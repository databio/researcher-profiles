"""Result objects SDK methods hand back in-process: what ``prof.cite``,
``prof.ask``, ``prof.topics``, ``store.match``, ``rank_works``,
``prof.coverage`` and friends return. Plain dataclasses, not wire models: the
JSON shapes the HTTP API serves live in :mod:`.api` and are never these
classes.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from ..errors import ProfileError

# ---------------------------------------------------------------------------
# Citation dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Citation:
    """A fully-expanded citation record for a single paper."""

    paper_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    abstract: str | None = None

    def to_dict(self) -> dict:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "url": self.url,
            "pdf_url": self.pdf_url,
            "abstract": self.abstract,
        }

    def to_bibtex(self) -> str:
        from ..profile.cite import _format_bibtex

        return _format_bibtex(self)

    def to_csl(self) -> str:
        from ..profile.cite import _format_csl_json

        return _format_csl_json([self])


@dataclass
class CitationRef:
    """A lightweight reference to a paper used as evidence for a response."""

    paper_id: str
    relevance: float | None = None
    span: str | None = None

    def to_dict(self) -> dict:
        return {
            "paper_id": self.paper_id,
            "relevance": self.relevance,
            "span": self.span,
        }


@dataclass
class PersonaResponse:
    """What every persona method returns: ``.ask``, ``.review``, ``Chat.send``.

    One shape, because the three answer the same kind of question: a piece of
    text written in the researcher's voice, plus the papers it leaned on and
    the accounting for the call that produced it.

    ``grounded`` is ``False`` when the model cited a ``paper_id`` that is not in
    this profile's paper set: the answer may still be useful, but it left the
    corpus. ``refused`` is ``True`` when strict-corpus retrieval came back too
    weak to answer from and the persona declined rather than improvised;
    ``refusal_reason`` then carries the retrieval score and threshold. A refusal
    is a successful call, not an error.

    ``raw`` is the provider's own response object, for callers that need
    something this dataclass does not expose. It is local-only: the HTTP API
    never serializes it, so a response that arrived over the wire has
    ``raw=None``.
    """

    text: str
    citations: list[CitationRef]
    model: str
    usage: dict
    raw: Any
    request_id: Optional[str] = None
    refused: bool = False
    refusal_reason: Optional[str] = None
    grounded: bool = True


@dataclass
class Idea:
    """One proposed research direction from ``ResearcherProfile.innovate``."""

    hypothesis: str
    approach: str
    rationale: str
    related_works: list[str]

    def resolve_citations(self, profile) -> dict[str, Any]:
        """Map ``related_works`` citation keys to paper records (or ``None``).

        Returns a mapping ``{citation_key: PaperRecord | None}``. Unknown keys
        map to ``None`` rather than raising.
        """
        by_id: dict[str, Any] = {}
        for p in getattr(profile, "papers", []) or []:
            key = getattr(p, "paper_id", None) or getattr(p, "id", None)
            if key:
                by_id[key] = p
        return {k: by_id.get(k) for k in self.related_works}


@dataclass
class Riff:
    """One brainstorm fragment from ``ResearcherProfile.riff``."""

    angle: str
    text: str
    related_work: Optional[str] = None


class GenerativeParseError(ProfileError):
    """Raised when the LLM repeatedly fails to return valid JSON."""

    def __init__(self, message: str, raw_text: str = ""):
        super().__init__(message)
        self.raw_text = raw_text


class PersonaUnavailableError(ProfileError):
    """Raised when a persona method is called on a profile with no persona.

    The persona endpoints (``.ask``/``.review``/``.innovate``/``.riff``)
    require a fully-synthesized profile: both ``expertise.md`` and ``SOUL.md``
    must be present and non-empty (see ``ResearcherProfile.has_persona``). An
    incomplete / "lite" profile is a client-visible precondition failure, not a
    server error, and must not silently role-play an empty persona. Subclasses
    ``ProfileError`` so library callers can catch either this or the base.
    """

    def __init__(self, slug: str):
        self.slug = slug
        super().__init__(
            f"profile {slug!r} has no synthesized persona (SOUL.md / "
            "expertise.md); persona endpoints require a fully-synthesized "
            "profile"
        )


# ---------------------------------------------------------------------------
# Registry-related dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Topic:
    """A topic cluster extracted from a profile's chunk embeddings."""

    label: str
    weight: float
    paper_ids: list[str] = field(default_factory=list)
    chunk_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "weight": self.weight,
            "paper_ids": list(self.paper_ids),
            "chunk_ids": list(self.chunk_ids),
        }


@dataclass
class MatchEvidence:
    """Evidence supporting a profile match for a query."""

    top_chunks: list[Any]  # list[SearchHit] - avoid circular import
    top_papers: list[str]
    overlapping_topics: list[str]
    centroid_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "top_chunks": [c.to_dict() if hasattr(c, "to_dict") else c for c in self.top_chunks],
            "top_papers": list(self.top_papers),
            "overlapping_topics": list(self.overlapping_topics),
            "centroid_score": self.centroid_score,
        }


def _llm_call_errors() -> tuple[type[BaseException], ...]:
    """The ways one Anthropic completion can fail: no extra, no key, bad call.

    Called from the ``except`` clause itself so ``anthropic``, which is behind
    the ``llm`` extra, is imported only after the call has already failed. When
    it is not installed at all, that failure is the ImportError in the tuple.
    """
    try:
        from anthropic import AnthropicError
    except ImportError:
        return (ImportError, RuntimeError)
    return (ImportError, RuntimeError, AnthropicError)


@dataclass
class Match:
    """One profile match against a query, with score, evidence, and lazy explanation."""

    profile: Any  # ResearcherProfile - excluded from to_dict
    score: float
    evidence: MatchEvidence
    explanation: Optional[str] = None

    def explain(self, query: str = "", *, client: Any = None) -> str:
        """Produce a short LLM-backed explanation of why this profile matched.

        Cached on first call. Subsequent calls return the cached value.
        """
        if self.explanation is not None:
            return self.explanation
        # Build a brief prompt from evidence; call LLM via the package adapter.
        from ..generative.llm import LLMClient

        prof = self.profile
        slug = getattr(prof, "slug", "?")
        name = getattr(prof, "name", slug)
        chunk_blurbs = []
        for h in self.evidence.top_chunks[:3]:
            text = getattr(h, "text", "") or ""
            chunk_blurbs.append(text.strip()[:400])
        joined_chunks = "\n\n---\n\n".join(chunk_blurbs)
        prompt = (
            f"Researcher: {name} (slug={slug})\n"
            f"Query: {query!r}\n"
            f"Top matching chunks:\n{joined_chunks}\n\n"
            "In 2-3 sentences, explain why this researcher is a good match for the query."
        )
        try:
            c = client if client is not None else LLMClient()
            resp = c.complete(
                system=[{"type": "text", "text": "You write brief researcher-match explanations."}],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
            )
            self.explanation = resp.text.strip()
        except _llm_call_errors() as e:  # pragma: no cover - defensive
            self.explanation = f"(explanation unavailable: {e})"
        return self.explanation

    def to_dict(self) -> dict[str, Any]:
        prof = self.profile
        slug = getattr(prof, "slug", None)
        return {
            "slug": slug,
            "score": self.score,
            "evidence": self.evidence.to_dict(),
            "explanation": self.explanation,
        }


@dataclass
class RankedWork:
    """One candidate work ranked against a profile, the mirror of :class:`Match`.

    ``work`` is a ``PaperRecord``; ``evidence`` is the overlapping profile
    topic labels supporting the score.
    """

    work: Any  # PaperRecord - serialized via model_dump in to_dict
    score: float
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        w = self.work
        return {
            "title": getattr(w, "title", None),
            "year": getattr(w, "year", None),
            "journal": getattr(w, "journal", None),
            "first_author": getattr(w, "first_author", None),
            "doi": getattr(w, "doi", None),
            "openalex_id": getattr(w, "openalex_id", None),
            "score": self.score,
            "evidence": list(self.evidence),
        }


# ---------------------------------------------------------------------------
# Coverage dataclass
# ---------------------------------------------------------------------------


@dataclass
class Coverage:
    """Landing-page metadata: what a profile covers and how fresh it is."""

    name: str
    affiliation: str | None
    one_liner: str | None
    topics: list[str]
    expertise_summary: str
    year_range: Optional[tuple[int, int]]
    paper_count: int
    last_updated: datetime
    staleness_label: str
    suggested_questions: list[str]
    coverage_caveats: list[str]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "affiliation": self.affiliation,
            "one_liner": self.one_liner,
            "topics": list(self.topics),
            "expertise_summary": self.expertise_summary,
            "year_range": list(self.year_range) if self.year_range else None,
            "paper_count": self.paper_count,
            "last_updated": self.last_updated.isoformat(),
            "staleness_label": self.staleness_label,
            "suggested_questions": list(self.suggested_questions),
            "coverage_caveats": list(self.coverage_caveats),
        }
