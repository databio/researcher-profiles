"""The sized record shapes: one profile, one paper, one paper row.

Built in one place so every surface that serves them (the REST routes, and the
MCP server that calls those routes) gets the same trim. The default
``record`` view holds what an agent or a list view commonly needs and stays
under 8 KB: long lists are cut to their top entries with a total, and the
JSON-LD plumbing and the file manifest are left out. ``view="full"`` keeps
every field, untrimmed.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import Request

from ..models.api import PaperRecordView, PaperRow, ProfileParts, ProfileRecord
from ..privacy import SECTION_FIELDS, ViewerTier, section_tiers, tier_allows
from ..profile.edit import paper_version
from ..profile.payloads import metadata_payload_dict
from ..schema import PaperRecord
from ..store import ProfileStore
from ._projection import _content_hash, registry_proofs
from ._sizes import SizeIndex

View = Literal["record", "full"]

#: Research interests (and the plain interest lists) shown in the record.
RECORD_TOP_INTERESTS = 8
#: Career and training entries shown in the record, most recent first.
RECORD_TOP_HISTORY = 3
#: Authors and topics shown in a paper record.
RECORD_TOP_AUTHORS = 10
RECORD_TOP_TOPICS = 5
#: The most of an abstract a paper record carries.
RECORD_ABSTRACT_CHARS = 1500
#: The length of a row's ``summary_short``.
SUMMARY_SHORT_CHARS = 160

#: Fields the record view carries as they are.
_RECORD_PLAIN = (
    "name",
    "job_title",
    "affiliation",
    "field",
    "subfields",
    "summary",
    "expertise",
    "methodological_commitments",
    "level",
    "visibility",
    "date_modified",
    "same_as",
    "openalex_id",
    "scholar_url",
    "collaborators",
    "paper_stats",
)
#: The optional clinical extension: carried only when non-empty.
_RECORD_CLINICAL = ("therapeutic_areas", "site_capabilities", "regulatory_experience")
#: Never in any view: the file manifest has its own route (``/files``).
_MANIFEST_KEYS = ("has_part", "subject_of")


def _trim(items: list, n: int) -> dict:
    return {"top": list(items[:n]), "total": len(items)}


def _interest_label(entry: dict) -> Optional[str]:
    concept = entry.get("concept") or {}
    return concept.get("label") or concept.get("display") or concept.get("code")


def _top_interests(entries: list[dict]) -> dict:
    """Top research interests by weight, as ``{"label", "weight"}`` only."""
    ranked = sorted(
        (e for e in entries if isinstance(e, dict)),
        key=lambda e: -(e.get("weight") if e.get("weight") is not None else 0.0),
    )
    top = []
    for e in ranked[:RECORD_TOP_INTERESTS]:
        row: dict[str, Any] = {"label": _interest_label(e)}
        if e.get("weight") is not None:
            row["weight"] = e["weight"]
        top.append(row)
    return {"top": top, "total": len(entries)}


def _recent(entries: list[dict], *keys: str) -> list[dict]:
    """Most recent first: an open-ended entry (no end year) counts as now."""

    def when(e: dict) -> tuple:
        out = []
        for k in keys:
            v = e.get(k)
            out.append(9999 if (v is None and k.startswith(("end", "year_end"))) else (v or 0))
        return tuple(out)

    return sorted((e for e in entries if isinstance(e, dict)), key=when, reverse=True)


def _scalars(obj: Any) -> Optional[dict]:
    if not isinstance(obj, dict):
        return None
    return {
        k: v for k, v in obj.items() if v is not None and not isinstance(v, (list, dict))
    } or None


def _withheld_fields(prof, viewer: ViewerTier) -> list[str]:
    """Inline document fields this viewer's tier does not reach."""
    tiers = section_tiers(prof.metadata)
    out: list[str] = []
    for section, names in SECTION_FIELDS.items():
        if not tier_allows(viewer, tiers[section]):
            out.extend(names)
    return out


def profile_record(
    prof,
    viewer: ViewerTier,
    *,
    view: View,
    request: Request,
    store: ProfileStore,
    sizes: Optional[SizeIndex] = None,
) -> ProfileRecord:
    """One profile as ``view`` shows it to ``viewer``. The one trim."""
    resolved = store.resolve_slug(prof.slug)
    sizes = sizes or SizeIndex(prof, viewer, store, resolved)
    data = metadata_payload_dict(prof, viewer, proofs=registry_proofs(request, prof.metadata.rid))
    for key in _MANIFEST_KEYS:
        data.pop(key, None)
    hidden = [f for f in _withheld_fields(prof, viewer) if f in data or f in _RECORD_PLAIN]
    papers = prof.papers
    summaries = prof.summaries
    counts = {
        "paper_count": len(papers),
        "summary_count": sum(1 for p in papers if p.paper_id and p.paper_id in summaries),
    }

    if view == "full":
        fields = {**data, **counts}
    else:
        fields = {k: data.get(k) for k in _RECORD_PLAIN}
        fields["interests"] = _trim(list(data.get("interests") or []), RECORD_TOP_INTERESTS)
        fields["not_interests"] = _trim(list(data.get("not_interests") or []), RECORD_TOP_INTERESTS)
        fields["research_interests"] = _top_interests(list(data.get("research_interests") or []))
        fields["career"] = _trim(
            _recent(list(data.get("career") or []), "end_year", "start_year"), RECORD_TOP_HISTORY
        )
        fields["training"] = _trim(
            _recent(list(data.get("training") or []), "year_end", "year_start"),
            RECORD_TOP_HISTORY,
        )
        fields["career_stage"] = _scalars(data.get("career_stage"))
        for key in _RECORD_CLINICAL:
            if data.get(key):
                fields[key] = data[key]
        fields.update(counts)
    # Withheld is ``null`` and named, never an empty value that reads as a fact.
    for name in hidden:
        if name in fields:
            fields[name] = None

    soul_size, expertise_size = sizes.soul(), sizes.expertise()
    withheld = sorted(set(hidden))
    if soul_size.reason == "not_permitted":
        withheld.append("soul")
    record = ProfileRecord(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        content_hash=_content_hash(store, resolved),
        view=view,
        fields=fields,
        parts=ProfileParts(
            soul=soul_size,
            expertise=expertise_size,
            papers=sizes.papers(),
            files_withheld=sizes.files_withheld(),
        ),
        withheld=withheld,
    )
    if view == "full":
        record.soul = prof.soul if soul_size.available else None
        record.expertise = prof.expertise if expertise_size.available else None
    return record


# ---------------------------------------------------------------------------
# Papers
# ---------------------------------------------------------------------------


def summary_text(
    prof, sizes: SizeIndex, record: PaperRecord
) -> tuple[Optional[str], Optional[str]]:
    """``(text, source)``: the generated summary this viewer may read, else the record's own."""
    pid = record.paper_id
    if pid and sizes.summary(pid).available:
        text = prof.summaries.get(pid) if pid in prof.summaries else None
        if text:
            return text, "generated"
    if record.summary:
        return record.summary, "record"
    return None, None


def _short(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    flat = " ".join(text.split())
    if len(flat) <= SUMMARY_SHORT_CHARS:
        return flat
    cut = flat[:SUMMARY_SHORT_CHARS]
    space = cut.rfind(" ")
    return (cut[:space] if space > SUMMARY_SHORT_CHARS // 2 else cut).rstrip() + "..."


def paper_row(
    prof, sizes: SizeIndex, record: PaperRecord, *, short_summary: bool = True
) -> PaperRow:
    """One paper as a list row: identity, sizes, the version, a short summary."""
    pid = record.paper_id or ""
    short = None
    if short_summary:
        text, _ = summary_text(prof, sizes, record)
        short = _short(text or record.abstract)
    return PaperRow(
        paper_id=pid,
        title=record.title,
        year=record.year,
        journal=record.journal,
        first_author=record.first_author,
        doi=record.doi,
        openalex_id=record.openalex_id,
        summary_short=short,
        summary=sizes.summary(pid),
        text=sizes.text(pid),
        version=paper_version(record),
    )


def paper_record_view(
    prof, sizes: SizeIndex, record: PaperRecord, *, view: View, sections: Optional[list[str]]
) -> PaperRecordView:
    """One paper as ``view`` shows it: fields, inline summary, sizes, sections."""
    pid = record.paper_id or ""
    # On-disk names (``datePublished``, ``isPartOf``, ``author``): the same
    # vocabulary the work patch takes, so a caller edits what it read.
    fields = record.model_dump(mode="json", by_alias=True, exclude_none=True)
    for key in ("@id", "@type", "@context"):
        fields.pop(key, None)
    if record.authors:
        fields["author"] = list(record.authors)  # names, not Person nodes
    if view == "record":
        fields.pop("summary", None)  # served once, as ``summary`` below
        if record.authors:
            fields["author"] = _trim(list(record.authors), RECORD_TOP_AUTHORS)
        if record.topics:
            fields["topics"] = _trim(list(record.topics), RECORD_TOP_TOPICS)
        if record.abstract and len(record.abstract) > RECORD_ABSTRACT_CHARS:
            fields["abstract"] = record.abstract[:RECORD_ABSTRACT_CHARS]
            fields["abstract_truncated"] = True
    text, source = summary_text(prof, sizes, record)
    summary_size, text_size = sizes.summary(pid), sizes.text(pid)
    withheld = [
        name
        for name, size in (("summary", summary_size), ("text", text_size))
        if size.reason == "not_permitted"
    ]
    return PaperRecordView(
        paper_id=pid,
        title=record.title,
        version=paper_version(record),
        view=view,
        fields=fields,
        summary=text,
        summary_source=source,
        parts={"summary": summary_size, "text": text_size},
        sections=sections,
        withheld=withheld,
    )


__all__ = [
    "RECORD_TOP_HISTORY",
    "RECORD_TOP_INTERESTS",
    "paper_record_view",
    "paper_row",
    "profile_record",
    "summary_text",
]
