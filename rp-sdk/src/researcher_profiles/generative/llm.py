"""Thin LLM client adapter and the bodies of ``prof.persona.ask`` / ``.review``.

The rest of the package never imports :mod:`anthropic` directly, and it is
imported lazily, so this module imports without it installed.
"""

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096
REFUSAL_THRESHOLD = 0.4
_MAX_HISTORY_CHARS = 12000

# Upper bound for one Anthropic call, so /ask and /review cannot hang on a
# stalled connection. Override with ``RESEARCHER_PROFILES_LLM_TIMEOUT`` (seconds).
DEFAULT_LLM_TIMEOUT_S = 90.0


def _llm_timeout() -> float:
    raw = os.environ.get("RESEARCHER_PROFILES_LLM_TIMEOUT")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_LLM_TIMEOUT_S


# paper_id citations in model text, e.g. [foo2024bar].
_CITATION_PATTERN = re.compile(r"\[([a-z][a-z0-9_-]{2,})\]")


def _refusal_threshold() -> float:
    """Return the configured refusal threshold (env override allowed)."""
    raw = os.environ.get("RESEARCHER_PROFILES_REFUSAL_THRESHOLD")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return REFUSAL_THRESHOLD


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: dict
    stop_reason: str
    raw: Any
    request_id: Optional[str] = None


class LLMClient:
    """Minimal Anthropic adapter.

    ``system`` is a list of content blocks so callers place ``cache_control``
    themselves, which caching the persona prefix requires.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = DEFAULT_MODEL,
        timeout: Optional[float] = None,
    ):
        import anthropic

        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout if timeout is not None else _llm_timeout(),
        )
        self.default_model = default_model

    def complete(
        self,
        system: list[dict],
        messages: list[dict],
        *,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: Optional[float] = None,
        thinking: Optional[dict] = None,
    ) -> LLMResponse:
        model = model or self.default_model
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        if temperature is not None:
            # anthropic>=1 rejects ``temperature`` in create(), but the API
            # still honours it on models before Opus 4.7, so send it raw.
            kwargs["extra_body"] = {"temperature": temperature}
        if thinking is not None:
            kwargs["thinking"] = thinking
        msg = self._client.messages.create(**kwargs)
        text = next(
            (b.text for b in msg.content if getattr(b, "type", None) == "text"),
            "",
        )
        usage = {
            "input_tokens": getattr(msg.usage, "input_tokens", 0),
            "output_tokens": getattr(msg.usage, "output_tokens", 0),
            "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", 0)
            or 0,
            "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", 0) or 0,
        }
        return LLMResponse(
            text=text,
            model=msg.model,
            usage=usage,
            stop_reason=msg.stop_reason,
            raw=msg,
            request_id=getattr(msg, "_request_id", None),
        )


# ----------------------------------------------------------------------
# Helpers + ResearcherProfile attachments
# ----------------------------------------------------------------------


def _first_lines(s: str, max_chars: int = 500) -> str:
    return s.strip()[:max_chars]


def _render_evidence(chunks) -> tuple[str, list]:
    """Render search hits into a text block and a list of ``CitationRef``.

    Expertise and SOUL chunks go into the text but emit no ``CitationRef``.
    """
    from ..models.results import CitationRef

    if not chunks:
        return "", []
    lines = ["=== RETRIEVED EVIDENCE FROM YOUR PAPERS ==="]
    # paper_id -> (best_score, span)
    by_id: dict[str, tuple[float, str]] = {}
    for i, ch in enumerate(chunks, 1):
        sid = getattr(ch, "source_id", "?")
        stype = getattr(ch, "source_type", "")
        meta = getattr(ch, "meta", {}) or {}
        year = meta.get("year", "?")
        text = getattr(ch, "text", "").strip()
        lines.append(f"[{i}] {sid} ({year}):")
        lines.append(text)
        lines.append("")
        if stype == "paper_summary":
            score = float(getattr(ch, "score", 0.0) or 0.0)
            span = text[:280]
            prev = by_id.get(sid)
            if prev is None or score > prev[0]:
                by_id[sid] = (score, span)
    lines.append("=== END EVIDENCE ===")

    citation_refs = [
        CitationRef(paper_id=pid, relevance=score, span=span)
        for pid, (score, span) in by_id.items()
    ]
    citation_refs.sort(key=lambda c: (-(c.relevance or 0.0), c.paper_id))
    return "\n".join(lines), citation_refs


def _log_usage(profile, *, method: str, resp: LLMResponse, **extra) -> None:
    from ..utils.paths import build_logs_dir

    directory = profile.directory
    if directory is None:
        # No directory, nowhere to log; usage logging must never fail a call.
        return
    log_path = build_logs_dir(directory) / "llm-usage.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": profile.slug,
        "method": method,
        "model": resp.model,
        "request_id": resp.request_id,
        "stop_reason": resp.stop_reason,
        "usage": resp.usage,
    }
    # Truncate long free-text fields; never log full materials.
    for k, v in extra.items():
        if v is None:
            continue
        if k == "question" and isinstance(v, str):
            record[k] = v[:500]
        else:
            record[k] = v
    with log_path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _get_llm(profile) -> LLMClient:
    """Lazily attach a single ``LLMClient`` to the profile."""
    cached = getattr(profile, "_llm_client", None)
    if cached is None:
        cached = LLMClient()
        object.__setattr__(profile, "_llm_client", cached)
    return cached


def _trim_history(history: list[dict], max_chars: int) -> list[dict]:
    """Drop oldest turns until the total ``content`` chars fit within budget."""
    if not history:
        return []
    out = [dict(t) for t in history]
    total = sum(len(t.get("content", "") or "") for t in out)
    while out and total > max_chars:
        dropped = out.pop(0)
        total -= len(dropped.get("content", "") or "")
    return out


def _zero_usage() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


class _RefusalResp:
    """Minimal stand-in for an LLMResponse when refusing (no LLM call)."""

    def __init__(self):
        self.text = ""
        self.model = "<none>"
        self.usage = _zero_usage()
        self.stop_reason = "refused"
        self.raw = None
        self.request_id = None


def _maybe_refuse(
    self,
    *,
    chunks,
    strict_corpus: bool,
    refusal_threshold: Optional[float],
    refusal_text: str,
    log_method: str,
    log_extra: dict,
):
    """Return a refusal response if strict-corpus retrieval is too weak, else ``None``."""
    from ..models.results import PersonaResponse

    threshold = refusal_threshold if refusal_threshold is not None else _refusal_threshold()
    top_score = max(
        (float(getattr(c, "score", 0.0) or 0.0) for c in chunks),
        default=0.0,
    )
    if not (strict_corpus and top_score < threshold):
        return None
    reason = f"top retrieval score {top_score:.2f} below threshold {threshold:.2f}"
    fake = _RefusalResp()
    fake.text = refusal_text
    _log_usage(
        self,
        method=log_method,
        resp=fake,
        source_ids=[],
        top_score=top_score,
        refusal_threshold=threshold,
        **log_extra,
    )
    return PersonaResponse(
        text=refusal_text,
        citations=[],
        model="<none>",
        usage=_zero_usage(),
        raw=None,
        request_id=None,
        refused=True,
        refusal_reason=reason,
        grounded=True,
    )


def _grounded(self, resp: LLMResponse) -> bool:
    """True unless the model cited a paper_id outside the profile's paper set."""
    known_ids = {getattr(p, "paper_id", None) for p in getattr(self, "papers", []) or []}
    known_ids.discard(None)
    all_cited = _CITATION_PATTERN.findall(resp.text or "")
    return all(pid in known_ids for pid in all_cited) if all_cited else True


def _source_type_kwargs(source_types: Optional[list[str]]) -> dict:
    """Retrieval kwargs for a caller limited to ``source_types``.

    Empty for ``None``. The filter is applied at the query, not after, so
    hidden chunks cannot crowd out the ones the caller may see.
    """
    if source_types is None:
        return {}
    return {"filter": {"source_type": list(source_types)}}


def _retrieval_errors() -> tuple[type[BaseException], ...]:
    """The index failures a persona verb answers ungrounded on.

    Imported only when a search has failed: ``researcher_profiles.embeddings``
    pulls numpy, and this module must import with the ``llm`` extra alone.
    """
    from ..embeddings import IndexNotBuiltError, MissingEmbeddingBackendError
    from ..errors import CapabilityUnavailableError

    return (CapabilityUnavailableError, IndexNotBuiltError, MissingEmbeddingBackendError)


def _ask(
    self,
    question: str,
    k: int = 5,
    *,
    model: Optional[str] = None,
    thinking: Optional[dict] = None,
    strict_corpus: bool = False,
    refusal_threshold: Optional[float] = None,
    history: Optional[list] = None,
    source_types: Optional[list[str]] = None,
    safe_mode: Any = None,  # noqa: ARG001
):
    from ..models.results import PersonaResponse, PersonaUnavailableError
    from .persona import build_persona_system_blocks

    if not self.has_persona:
        raise PersonaUnavailableError(self.slug)

    try:
        chunks = self.index.search(question, k=k, **_source_type_kwargs(source_types))
    except _retrieval_errors():
        # No usable index: answer ungrounded rather than refusing.
        chunks = []
    evidence_text, citation_refs = _render_evidence(chunks)

    refusal = _maybe_refuse(
        self,
        chunks=chunks,
        strict_corpus=strict_corpus,
        refusal_threshold=refusal_threshold,
        refusal_text=(
            "I can't answer that from my own work. The question falls outside "
            "the papers and materials this profile covers."
        ),
        log_method="ask:refused",
        log_extra={"question": question, "k": k},
    )
    if refusal is not None:
        return refusal

    system_blocks = build_persona_system_blocks(
        expertise_md=self.expertise,
        soul_md=self.soul,
        mode="ask",
    )

    messages: list[dict] = []
    if history:
        messages.extend(_trim_history(list(history), _MAX_HISTORY_CHARS))

    user_text = (f"{evidence_text}\n\n" if evidence_text else "") + f"QUESTION: {question}"
    messages.append({"role": "user", "content": user_text})

    resp = _get_llm(self).complete(
        system=system_blocks,
        messages=messages,
        model=model,
        thinking=thinking,
    )

    grounded = _grounded(self, resp)

    _log_usage(
        self,
        method="ask",
        resp=resp,
        question=question,
        k=k,
        source_ids=[c.paper_id for c in citation_refs],
    )

    return PersonaResponse(
        text=resp.text,
        citations=citation_refs,
        model=resp.model,
        usage=resp.usage,
        raw=resp.raw,
        request_id=resp.request_id,
        refused=False,
        refusal_reason=None,
        grounded=grounded,
    )


def _review(
    self,
    material: str,
    focus: Optional[str] = None,
    k: int = 5,
    *,
    model: Optional[str] = None,
    thinking: Optional[dict] = None,
    strict_corpus: bool = False,
    refusal_threshold: Optional[float] = None,
    source_types: Optional[list[str]] = None,
    safe_mode: Any = None,  # noqa: ARG001
):
    from ..models.results import PersonaResponse, PersonaUnavailableError
    from .persona import build_persona_system_blocks

    if not self.has_persona:
        raise PersonaUnavailableError(self.slug)

    query = focus if focus else _first_lines(material, max_chars=500)
    try:
        chunks = self.index.search(query, k=k, **_source_type_kwargs(source_types)) if query else []
    except _retrieval_errors():
        # See ``_ask``.
        chunks = []
    evidence_text, citation_refs = _render_evidence(chunks)

    refusal = _maybe_refuse(
        self,
        chunks=chunks,
        strict_corpus=strict_corpus,
        refusal_threshold=refusal_threshold,
        refusal_text=(
            "I can't review this from my own work. The material falls outside "
            "the papers and materials this profile covers."
        ),
        log_method="review:refused",
        log_extra={"focus": focus, "k": k},
    )
    if refusal is not None:
        return refusal

    system_blocks = build_persona_system_blocks(
        expertise_md=self.expertise,
        soul_md=self.soul,
        mode="review",
    )

    parts: list[str] = []
    if evidence_text:
        parts.append(evidence_text)
    parts.append("=== MATERIAL TO REVIEW ===")
    parts.append(material.strip())
    parts.append("=== END MATERIAL ===")
    if focus:
        parts.append(f"FOCUS: Pay particular attention to {focus}.")
    user_text = "\n\n".join(parts)

    resp = _get_llm(self).complete(
        system=system_blocks,
        messages=[{"role": "user", "content": user_text}],
        model=model,
        thinking=thinking,
    )

    grounded = _grounded(self, resp)

    _log_usage(
        self,
        method="review",
        resp=resp,
        focus=focus,
        k=k,
        source_ids=[c.paper_id for c in citation_refs],
    )

    return PersonaResponse(
        text=resp.text,
        citations=citation_refs,
        model=resp.model,
        usage=resp.usage,
        raw=resp.raw,
        request_id=resp.request_id,
        refused=False,
        refusal_reason=None,
        grounded=grounded,
    )


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_MAX_TOKENS",
    "LLMClient",
    "LLMResponse",
    "REFUSAL_THRESHOLD",
]
