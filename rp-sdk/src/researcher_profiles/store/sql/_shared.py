"""The artifact addresses and row queries both halves of the SQL backend read.

This is the leaf of the ``store.sql`` package: it imports nothing from its
siblings, so :mod:`._store` and :mod:`._storage` can both depend on it without
a cycle. What lives here is exactly what the two classes share: the manifest
addresses with a fixed meaning (the rendered collections, the persona
documents), the summary naming rule, and the two ``rp_artifacts`` reads that
a store-level export and a profile-level load answer identically.
"""

from typing import Any, Optional

from sqlmodel import Session, select

from ...schema.jsonld import canonical_dumps
from ...schema.manifest import is_text_artifact
from ..db import ArtifactRow, GrantRow, PaperRow

#: Manifest addresses whose bytes are regenerated from the relational tables
#: rather than stored as an artifact body. Their rows carry ``rendered=True``,
#: a NULL body, and the collection ``envelope``.
PAPERS_URL = "sources/papers.jsonld"
GRANTS_URL = "sources/grants.jsonld"
RENDERED_URLS = (PAPERS_URL, GRANTS_URL)

SOUL_URL = "personality/SOUL.md"
EXPERTISE_URL = "personality/expertise.md"
CITATIONS_URL = "sources/citations.json"
SUMMARY_SUFFIX = ".summary.md"

#: Manifest roles naming a paper summary.
SUMMARY_ROLES = frozenset({"paper_summary"})


def _summary_id(part_or_url: Any) -> Optional[str]:
    """The summary id a manifest entry names, or ``None``.

    Prefers the entry's declared ``paperId`` and falls back to the filename,
    because the published corpus contains entries carrying only one of the two.
    """
    paper_id = getattr(part_or_url, "paper_id", None)
    if paper_id:
        return str(paper_id)
    url = getattr(part_or_url, "content_url", part_or_url)
    if isinstance(url, str) and url.endswith(SUMMARY_SUFFIX):
        return url.rsplit("/", 1)[-1][: -len(SUMMARY_SUFFIX)]
    return None


def _is_text(encoding_format: Optional[str], content_url: str) -> bool:
    """Which body column an artifact lands in. Defined once, in ``manifest``."""
    return is_text_artifact(encoding_format, content_url)


def _is_memory_sqlite(url: str) -> bool:
    """Whether ``url`` names an in-memory SQLite database."""
    tail = url.split("://", 1)[-1]
    return tail in ("", "/", ":memory:", "/:memory:") or "mode=memory" in url


def artifact_rows(s: Session, rid: str) -> list[ArtifactRow]:
    """Every ``rp_artifacts`` row of one profile, in manifest order."""
    return list(
        s.exec(
            select(ArtifactRow)
            .where(ArtifactRow.profile_rid == rid)
            .order_by(ArtifactRow.manifest_slot, ArtifactRow.ordinal, ArtifactRow.content_url)
        ).all()
    )


def render_collection(s: Session, rid: str, art: ArtifactRow) -> Optional[str]:
    """Re-render ``papers.jsonld`` / ``grants.jsonld`` from the tables."""
    envelope = dict(art.envelope or {})
    if art.content_url == PAPERS_URL:
        records = [
            r.record
            for r in s.exec(
                select(PaperRow).where(PaperRow.profile_rid == rid).order_by(PaperRow.ordinal)
            ).all()
        ]
    elif art.content_url == GRANTS_URL:
        records = [
            r.record
            for r in s.exec(
                select(GrantRow).where(GrantRow.profile_rid == rid).order_by(GrantRow.ordinal)
            ).all()
        ]
    else:  # pragma: no cover - only two collections are rendered
        return None
    envelope["hasPart"] = records
    return canonical_dumps(envelope)
