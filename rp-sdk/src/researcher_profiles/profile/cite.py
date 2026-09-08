"""``prof.cite``: the profile's papers as formatted citations.

Defines :class:`CitationManager`, the object ``ResearcherProfile.cite`` hands
back (``prof.cite.get(...)`` / ``.many(...)`` / ``.verify(...)`` /
``.export(...)``), together with the functions behind it: converting a
:class:`~researcher_profiles.schema.PaperRecord` into a
:class:`~researcher_profiles.models.results.Citation`, and the bibtex / CSL-JSON /
plain / RIS formatters. Importing this module has no side effect on the
profile class.
"""

import json
import re
from typing import Iterable, Literal

from ..models.results import Citation
from ..schema import PaperRecord

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s/]+", re.IGNORECASE)


def _looks_like_pdf(url: str | None) -> bool:
    if not url:
        return False
    u = url.lower()
    return u.endswith(".pdf") or "/full.pdf" in u or ".pdf?" in u


def _extract_doi(url: str | None) -> str | None:
    if not url:
        return None
    m = _DOI_RE.search(url)
    return m.group(0) if m else None


def _paper_to_citation(paper: PaperRecord) -> Citation:
    """Convert a :class:`PaperRecord` into a :class:`Citation`.

    Best-effort: missing fields stay ``None`` and the formatters degrade
    gracefully. Author lists are derived from ``paper.authors`` when
    present, else ``[first_author]``, else ``[]``.
    """
    paper_id = getattr(paper, "paper_id", None) or ""
    title = getattr(paper, "title", "") or ""

    authors_field = getattr(paper, "authors", None)
    if authors_field:
        authors = list(authors_field)
    else:
        fa = getattr(paper, "first_author", None)
        authors = [fa] if fa else []

    year = getattr(paper, "year", None)
    venue = getattr(paper, "venue", None) or getattr(paper, "journal", None)

    explicit_doi = getattr(paper, "doi", None)
    full_text_link = getattr(paper, "full_text_link", None)
    doi = explicit_doi or _extract_doi(full_text_link)

    explicit_url = getattr(paper, "url", None)
    if doi:
        url = f"https://doi.org/{doi}"
    elif explicit_url:
        url = explicit_url
    elif full_text_link and not _looks_like_pdf(full_text_link):
        url = full_text_link
    else:
        url = None

    explicit_pdf = getattr(paper, "pdf_url", None)
    if explicit_pdf:
        pdf_url = explicit_pdf
    elif _looks_like_pdf(full_text_link):
        pdf_url = full_text_link
    else:
        pdf_url = None

    abstract = getattr(paper, "abstract", None) or getattr(paper, "summary", None)

    return Citation(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        doi=doi,
        url=url,
        pdf_url=pdf_url,
        abstract=abstract,
    )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _format_bibtex(c: Citation) -> str:
    fields: list[str] = []
    if c.title:
        fields.append(f"  title={{{c.title}}}")
    if c.authors:
        fields.append(f"  author={{{' and '.join(c.authors)}}}")
    if c.year is not None:
        fields.append(f"  year={{{c.year}}}")
    if c.venue:
        fields.append(f"  journal={{{c.venue}}}")
    if c.doi:
        fields.append(f"  doi={{{c.doi}}}")
    if c.url:
        fields.append(f"  url={{{c.url}}}")
    key = c.paper_id or "ref"
    body = ",\n".join(fields)
    return f"@article{{{key},\n{body}\n}}"


def _format_csl_json(citations: list[Citation]) -> str:
    out: list[dict] = []
    for c in citations:
        record: dict = {
            "id": c.paper_id,
            "type": "article-journal",
            "title": c.title,
        }
        if c.authors:
            record["author"] = [{"family": a} for a in c.authors]
        if c.year is not None:
            record["issued"] = {"date-parts": [[c.year]]}
        if c.venue:
            record["container-title"] = c.venue
        if c.doi:
            record["DOI"] = c.doi
        if c.url:
            record["URL"] = c.url
        out.append(record)
    return json.dumps(out, indent=2)


def _format_plain(c: Citation, *, fallback: str | None = None) -> str:
    if fallback:
        return fallback
    primary = c.authors[0] if c.authors else "Unknown"
    venue = c.venue or "n.d."
    year = c.year if c.year is not None else "n.d."
    return f"{primary} et al., {venue} ({year})"


def _format_ris(c: Citation) -> str:
    lines = ["TY  - JOUR"]
    if c.title:
        lines.append(f"TI  - {c.title}")
    for a in c.authors:
        lines.append(f"AU  - {a}")
    if c.year is not None:
        lines.append(f"PY  - {c.year}")
    if c.venue:
        lines.append(f"JO  - {c.venue}")
    if c.doi:
        lines.append(f"DO  - {c.doi}")
    if c.url:
        lines.append(f"UR  - {c.url}")
    lines.append("ER  - ")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The implementation functions behind the manager
# ---------------------------------------------------------------------------


def _cite(self, paper_id: str) -> Citation:
    cache = getattr(self, "_citation_cache", None)
    if cache is None:
        cache = {}
        object.__setattr__(self, "_citation_cache", cache)
    if paper_id in cache:
        return cache[paper_id]
    for p in self.papers:
        pid = getattr(p, "paper_id", None)
        if pid == paper_id:
            c = _paper_to_citation(p)
            cache[paper_id] = c
            return c
    raise KeyError(paper_id)


def _cite_many(self, paper_ids: Iterable[str], *, strict: bool = True) -> list[Citation]:
    out: list[Citation] = []
    for pid in paper_ids:
        try:
            out.append(_cite(self, pid))
        except KeyError:
            if strict:
                raise
    return out


def _verify_citations(self, paper_ids: Iterable[str]) -> list[bool]:
    known = {getattr(p, "paper_id", None) for p in self.papers}
    return [pid in known for pid in paper_ids]


def _export_citations(
    self,
    paper_ids: Iterable[str],
    format: Literal["bibtex", "csl-json", "plain", "ris"],
) -> str:
    citations = _cite_many(self, paper_ids, strict=False)
    if format == "bibtex":
        return "\n\n".join(_format_bibtex(c) for c in citations)
    if format == "csl-json":
        return _format_csl_json(citations)
    if format == "plain":
        # Build a lookup from on-disk citation strings (paper.citation) so
        # plain formatter prefers the pre-rendered human-readable string.
        fallback_map: dict[str, str | None] = {}
        for p in self.papers:
            pid = getattr(p, "paper_id", None)
            if pid:
                fallback_map[pid] = getattr(p, "citation", None)
        return "\n".join(_format_plain(c, fallback=fallback_map.get(c.paper_id)) for c in citations)
    if format == "ris":
        return "\n".join(_format_ris(c) for c in citations)
    raise ValueError(f"unknown citation format: {format!r}")


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class CitationManager:
    """``prof.cite``: the profile's papers as formatted citations."""

    def __init__(self, profile):
        self._profile = profile

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CitationManager(slug={self._profile.slug!r})"

    def get(self, paper_id: str) -> Citation:
        """One :class:`Citation`. Raises ``KeyError`` for an unknown id."""
        return _cite(self._profile, paper_id)

    def many(self, paper_ids: Iterable[str], *, strict: bool = True) -> list[Citation]:
        """Several citations; ``strict=False`` drops unknown ids silently."""
        return _cite_many(self._profile, paper_ids, strict=strict)

    def verify(self, paper_ids: Iterable[str]) -> list[bool]:
        """Whether each id names a paper this profile actually holds."""
        return _verify_citations(self._profile, paper_ids)

    def export(
        self,
        paper_ids: Iterable[str],
        format: Literal["bibtex", "csl-json", "plain", "ris"],
    ) -> str:
        """Render citations in ``format``. Unknown ids are dropped."""
        return _export_citations(self._profile, paper_ids, format)


__all__ = ["CitationManager", "_paper_to_citation"]
