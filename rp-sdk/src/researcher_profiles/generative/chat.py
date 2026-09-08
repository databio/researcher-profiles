"""Multi-turn ``Chat`` wrapper around ``prof.persona.ask``.

``ResearcherProfile.persona.chat(...)`` is the factory; importing this module
has no side effect on the profile class.
"""

import time
from dataclasses import dataclass, field
from typing import Literal, Optional

from ..models.results import CitationRef, PersonaResponse

_MAX_HISTORY_CHARS_DEFAULT = 12000


@dataclass
class Turn:
    role: Literal["user", "assistant"]
    content: str
    citations: list[CitationRef] = field(default_factory=list)
    ts: float = field(default_factory=lambda: time.time())
    usage: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "citations": [c.to_dict() for c in self.citations],
            "ts": self.ts,
            "usage": self.usage,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Turn":
        cits = [
            CitationRef(
                paper_id=c["paper_id"],
                relevance=c.get("relevance"),
                span=c.get("span"),
            )
            for c in data.get("citations", []) or []
        ]
        return cls(
            role=data["role"],
            content=data["content"],
            citations=cits,
            ts=float(data.get("ts", time.time())),
            usage=data.get("usage"),
        )


class Chat:
    """A multi-turn conversation rooted at a :class:`ResearcherProfile`."""

    def __init__(
        self,
        profile,
        *,
        k: int = 5,
        strict_corpus: bool = False,
        model: Optional[str] = None,
        max_history_chars: int = _MAX_HISTORY_CHARS_DEFAULT,
    ):
        self.profile = profile
        self.k = k
        self.strict_corpus = strict_corpus
        self.model = model
        self.max_history_chars = max_history_chars
        self.history: list[Turn] = []

    # ------------------------------------------------------------------
    # Message rendering for the LLM
    # ------------------------------------------------------------------

    def _history_as_messages(self) -> list[dict]:
        msgs = [{"role": t.role, "content": t.content} for t in self.history]
        # Trim from the head until we fit within the char budget.
        total = sum(len(m["content"]) for m in msgs)
        while msgs and total > self.max_history_chars:
            dropped = msgs.pop(0)
            total -= len(dropped["content"])
        return msgs

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def send(self, message: str, k: Optional[int] = None) -> PersonaResponse:
        # Snapshot history before appending the new user turn. That snapshot
        # is what the LLM sees as prior context.
        prior_messages = self._history_as_messages()
        self.history.append(Turn(role="user", content=message, citations=[], usage=None))

        resp = self.profile.persona.ask(
            message,
            k=k if k is not None else self.k,
            model=self.model,
            strict_corpus=self.strict_corpus,
            history=prior_messages,
        )

        self.history.append(
            Turn(
                role="assistant",
                content=resp.text,
                citations=list(resp.citations),
                usage=dict(resp.usage) if resp.usage else None,
            )
        )
        return resp

    def reset(self) -> None:
        self.history = []

    def to_dict(self) -> dict:
        return {
            # `rid` is what a reloaded session is validated against; `slug` is
            # written for human readability only. Keying on the directory name
            # meant a rename hard-broke every saved session.
            "rid": getattr(self.profile, "rid", None),
            "slug": self.profile.slug,
            "k": self.k,
            "strict_corpus": self.strict_corpus,
            "model": self.model,
            "max_history_chars": self.max_history_chars,
            "history": [t.to_dict() for t in self.history],
        }

    @classmethod
    def from_dict(cls, data: dict, profile) -> "Chat":
        # Validate on identity when the session carries one; fall back to the
        # slug for sessions written before `rid` existed.
        saved_rid = data.get("rid")
        if saved_rid:
            current_rid = getattr(profile, "rid", None)
            if saved_rid != current_rid:
                raise ValueError(
                    f"rid mismatch: session rid {saved_rid!r} != profile rid {current_rid!r}"
                )
        else:
            slug = data.get("slug")
            if slug != getattr(profile, "slug", None):
                raise ValueError(
                    f"slug mismatch: data slug {slug!r} != profile slug {profile.slug!r}"
                )
        chat = cls(
            profile,
            k=int(data.get("k", 5)),
            strict_corpus=bool(data.get("strict_corpus", False)),
            model=data.get("model"),
            max_history_chars=int(data.get("max_history_chars", _MAX_HISTORY_CHARS_DEFAULT)),
        )
        chat.history = [Turn.from_dict(t) for t in data.get("history", []) or []]
        return chat


__all__ = ["Chat", "Turn"]
