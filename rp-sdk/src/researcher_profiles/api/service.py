"""The service layer: plain functions that hold the logic and every check.

Each function takes a :class:`~researcher_profiles.api.service.Service` (the
store, the host hooks, and the app's caches, bound once per app), an explicit
:class:`~researcher_profiles.api.caller.Caller`, and its arguments. It checks
permission itself (through the hooks), does the work, and raises only the
typed errors in :mod:`researcher_profiles.errors`. The HTTP routes and a
host's MCP tools are two thin adapters over these functions; neither holds a
check of its own.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from ..errors import Invalid, NotFound
from ..models.api import PaperPage, TextSection
from ..privacy import (
    ViewerTier,
    effective_tiers,
    explain_tiers,
    narrow_viewer,
    profile_visible,
    tier_allows,
)
from ..schema import PaperRecord
from ..store import ProfileNotFoundError, ProfileStore
from ..utils.paths import STORE_CACHE_DIRNAME
from ._cursor import decode_cursor, encode_cursor
from ._limits import BATCH_IDS_CAP, PAPERS_LIMIT, clamp
from ._passages import paper_passages, profile_passages
from ._projection import _content_hash, artifact_visible
from ._records import paper_record_view, paper_row, profile_record, summary_text
from ._sizes import EXPERTISE_URL, PAPERS_URL, SOUL_URL, SizeIndex, text_url
from ._text import page_text, sections_of
from .caller import Caller
from .deps import TierFloor
from .hooks import Hooks

logger = logging.getLogger(__name__)


def profile_missing(ref: str) -> NotFound:
    """The not-found for "no such profile" and for "not for you": the same sentence."""
    return NotFound(f"profile {ref!r} not found")


class Service:
    """The store, the hooks, and the two caches every request shares. One per app."""

    def __init__(self, store: ProfileStore, hooks: Optional[Hooks] = None):
        self.store = store
        self.hooks = hooks if hooks is not None else Hooks()
        #: The profile graph, built lazily by ``deps.get_graph``.
        self.graph: Any = None
        #: The temp directory a rootless store is exported to for the graph.
        self.registry_tempdir: Any = None

    # -- the hooks, with their defaults ------------------------------------

    def store_for(self, caller: Caller) -> ProfileStore:
        """The store as this caller reads it. Never used by an edit."""
        hook = self.hooks.store_for
        return self.store if hook is None else hook(caller, self.store)

    def viewer(self, caller: Caller, slug: Optional[str]) -> ViewerTier:
        """This caller's viewer tier for one profile, preview cap applied."""
        tier: ViewerTier = self.hooks.viewer_resolver(caller, slug)
        if caller.viewer_cap is not None:
            tier = narrow_viewer(tier, caller.viewer_cap)
        return tier

    def floor(self, caller: Caller, prof: Any, slug: Optional[str] = None) -> TierFloor:
        """The host's floor for this profile, and why: never ``None``."""
        return self.hooks.profile_tier_floor(caller, prof, slug) or TierFloor()

    def proofs(self, rid: str) -> list:
        """The registry-issued proofs for ``rid``; ``[]`` when none or the hook fails.

        A proof failure must never fail a public read, so a raising hook is
        logged and treated as ``[]``.
        """
        hook = self.hooks.registry_proofs
        if hook is None or not rid:
            return []
        try:
            return list(hook(rid) or [])
        # Boundary: whatever the host's hook raises, the read still succeeds.
        except Exception:
            logger.warning("registry_proofs hook failed for %r", rid, exc_info=True)
            return []

    def invalidate(self, slug: str) -> None:
        """Drop every cache that could still reflect the pre-edit profile.

        The cached profile object and the store's on-disk ``.cache`` memos
        (centroids/topics/graph): the exact set a tarball push invalidates.
        ``store.evict`` also bumps the store's write generation, which is the
        whole in-process invalidation for ranking.

        **Cache invalidation only.** Dependent-state maintenance belongs on
        the write hooks (``store.add_pre_commit_hook``), which run inside the
        write. Failures here stay swallowed: a stale-cache rebuild is cheap and
        re-eviction is idempotent.
        """
        store = self.store
        store.evict(slug)
        # The derived graph follows the same rule: drop the snapshot so the
        # next graph query rebuilds over the new corpus.
        self.graph = None
        # A rootless store materializes into a temp directory the graph build
        # reuses; drop it so the next build re-exports the live corpus.
        tempdir = self.registry_tempdir
        if tempdir is not None:
            cleanup = getattr(tempdir, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup()
                except OSError:
                    pass
            self.registry_tempdir = None
        root = store.root
        if root is None:
            return
        # Names defined by ``CentroidManager.cache_path``,
        # ``MatchManager.topics_cache_path`` and ``graph.cache.graph_db_path``;
        # literal here because this path must work without importing them.
        for cache_name in ("centroids.npz", "topics.json", "graph.sqlite"):
            fp = root / STORE_CACHE_DIRNAME / cache_name
            if fp.exists():
                try:
                    fp.unlink()
                except OSError:
                    pass

    # -- the one entry point every read starts from -------------------------

    def load_visible(self, caller: Caller, ref: str) -> tuple[Any, ViewerTier]:
        """``(profile, viewer tier)`` for a profile this caller may see, else ``NotFound``.

        The profile comes from the caller's view of the store; a profile the
        caller may not see is the same ``NotFound`` as one that does not exist.
        """
        _store, prof, viewer = self._read(caller, ref)
        return prof, viewer

    def _read(self, caller: Caller, ref: str) -> tuple[ProfileStore, Any, ViewerTier]:
        """:meth:`load_visible`, plus the store view the profile came from."""
        store = self.store_for(caller)
        try:
            prof = store.get(ref)
        except (ProfileNotFoundError, KeyError) as e:
            raise profile_missing(ref) from e
        viewer = self.viewer(caller, ref)
        floor = self.floor(caller, prof, ref)
        if not profile_visible(prof.metadata, viewer, floor=floor.tier):
            raise profile_missing(ref)
        return store, prof, viewer


# ---------------------------------------------------------------------------
# Reads
#
# Every read starts from ``Service._read`` (``load_visible``): the caller's
# view of the store, the profile gate, the viewer tier. A profile or a part
# this caller may not see is the same ``NotFound`` as one that does not exist.
# ---------------------------------------------------------------------------

View = Literal["record", "full"]

#: The narrative's two parts, in reading order: ``(section name, contentUrl, role)``.
_NARRATIVE = (("soul", SOUL_URL, "soul"), ("expertise", EXPERTISE_URL, "expertise"))


def _works_visible(prof, viewer: ViewerTier) -> bool:
    return artifact_visible(
        explain_tiers(prof.metadata), prof.metadata, PAPERS_URL, "works", viewer
    )


def _year_key(record: PaperRecord) -> int:
    return record.year if isinstance(record.year, int) else -1


def split_ids(ids: str) -> list[str]:
    """A comma list of ids, stripped, deduplicated, in order."""
    out: list[str] = []
    for raw in ids.split(","):
        pid = raw.strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def _find_paper(prof, paper_id: str, ref: str) -> PaperRecord:
    for record in prof.papers:
        if record.paper_id == paper_id:
            return record
    raise NotFound(f"paper {paper_id!r} not found in profile {ref!r}")


def _text_of(store: ProfileStore, ref: str, content_url: str) -> str:
    """One artifact's stored text; a ``NotFound`` that says so when its body was never pushed."""
    try:
        return store.artifact_bytes(store.resolve_slug(ref), content_url).decode(
            "utf-8", errors="replace"
        )
    except (ProfileNotFoundError, KeyError) as e:
        raise NotFound(
            f"{content_url!r} is listed for {ref!r} but its content was not uploaded"
        ) from e


def _unknown_section(valid: list[str]) -> Invalid:
    return Invalid(
        "that section does not exist in this text", code="unknown_section", valid=list(valid)
    )


def get_profile(service: Service, caller: Caller, ref: str, *, view: View = "record"):
    """One profile, sized for this caller (a :class:`ProfileRecord`).

    ``view="record"`` is the trimmed record (under 8 KB); ``view="full"``
    carries every metadata field untrimmed plus the ``soul`` and ``expertise``
    bodies. A field this caller may not see is ``null`` and named in
    ``withheld``.
    """
    store, prof, viewer = service._read(caller, ref)
    return profile_record(
        prof, viewer, view=view, proofs=service.proofs(prof.metadata.rid), store=store
    )


def works_visible(service: Service, caller: Caller, ref: str) -> tuple[Any, ViewerTier]:
    """``(profile, viewer tier)`` when this caller may read the profile's works list.

    The works artifact (``sources/papers.jsonld``) has its own tier; a caller
    who may see the profile but not its works gets the profile's ``NotFound``.
    """
    prof, viewer = service.load_visible(caller, ref)
    if not _works_visible(prof, viewer):
        raise profile_missing(ref)
    return prof, viewer


def list_papers(
    service: Service,
    caller: Caller,
    ref: str,
    *,
    q: Optional[str] = None,
    year_min: Optional[int] = None,
    missing_ids: bool = False,
    has_text: Optional[bool] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    ids: Optional[str] = None,
):
    """The works list as rich rows, paged (a :class:`PaperPage`).

    Without ``q``, newest first with a keyset cursor; with ``q``, ranked by
    hybrid search with an offset cursor. ``ids`` (a comma list, at most 20)
    reads those rows only, in that order. A cursor made under other filters is
    ``Invalid(code="cursor_mismatch")``.
    """
    store, prof, viewer = service._read(caller, ref)
    if not _works_visible(prof, viewer):
        raise profile_missing(ref)
    sizes = SizeIndex(prof, viewer, store, store.resolve_slug(ref))
    papers = [p for p in prof.papers if p.paper_id]

    if ids is not None:
        wanted = split_ids(ids)
        taken = wanted[:BATCH_IDS_CAP]
        by_id = {p.paper_id: p for p in papers}
        rows = [paper_row(prof, sizes, by_id[pid]) for pid in taken if pid in by_id]
        note = None
        if len(wanted) > BATCH_IDS_CAP:
            note = f"Only the first {BATCH_IDS_CAP} ids were read."
        return PaperPage(
            items=rows,
            total=len(rows),
            limit_applied=len(taken),
            filters_applied={"ids": taken},
            note=note,
        )

    filters = {"q": q, "year_min": year_min, "missing_ids": missing_ids, "has_text": has_text}
    applied = {k: v for k, v in filters.items() if v not in (None, False, "")}
    lim = clamp(limit, *PAPERS_LIMIT)
    if year_min is not None:
        papers = [p for p in papers if isinstance(p.year, int) and p.year >= year_min]
    if missing_ids:
        papers = [p for p in papers if not p.doi and not p.openalex_id]
    if has_text is not None:
        papers = [p for p in papers if sizes.text(p.paper_id).available == has_text]

    if not q:
        papers.sort(key=lambda p: (-_year_key(p), p.paper_id))
        full_total = len(papers)
        if cursor:
            key = decode_cursor(cursor, filters).get("k")
            if not (isinstance(key, list) and len(key) == 2):
                decode_cursor("", filters)  # raises the cursor_mismatch Invalid
            after = (-int(key[0]), str(key[1]))
            papers = [p for p in papers if (-_year_key(p), p.paper_id) > after]
        page = papers[:lim]
        more = len(papers) > lim
        next_cursor = (
            encode_cursor({"k": [_year_key(page[-1]), page[-1].paper_id]}, filters)
            if more and page
            else None
        )
        return PaperPage(
            items=[paper_row(prof, sizes, p) for p in page],
            total=full_total,
            limit_applied=lim,
            next_cursor=next_cursor,
            has_more=more,
            filters_applied=applied,
        )

    from ._semantic import hybrid_rank_papers

    candidates = []
    for p in papers:
        text, _ = summary_text(prof, sizes, p)
        candidates.append(
            {
                "paper_id": p.paper_id,
                "title": p.title,
                "journal": p.journal,
                "summary": text,
                "abstract": p.abstract,
            }
        )
    ranked, mode, note = hybrid_rank_papers(store, prof, viewer, q, candidates)
    start = 0
    if cursor:
        start = decode_cursor(cursor, filters).get("o")
        if not isinstance(start, int) or start < 0:
            decode_cursor("", filters)
    by_id = {p.paper_id: p for p in papers}
    window = ranked[start : start + lim]
    rows = []
    for pid, score, matched_by in window:
        row = paper_row(prof, sizes, by_id[pid])
        row.score = round(score, 6)
        row.matched_by = list(matched_by)
        rows.append(row)
    more = start + lim < len(ranked)
    return PaperPage(
        items=rows,
        total=len(ranked),
        limit_applied=lim,
        next_cursor=encode_cursor({"o": start + lim}, filters) if more else None,
        has_more=more,
        filters_applied=applied,
        search_mode_used=mode,
        note=note,
    )


def get_paper(service: Service, caller: Caller, ref: str, paper_id: str, *, view: View = "record"):
    """One paper: its fields, its summary inline, its sizes, its version (a :class:`PaperRecordView`).

    Gated like the works list. A paper that is not there, or that this caller
    may not see, is ``NotFound``.
    """
    store, prof, viewer = service._read(caller, ref)
    if not _works_visible(prof, viewer):
        raise profile_missing(ref)
    record = _find_paper(prof, paper_id, ref)
    sizes = SizeIndex(prof, viewer, store, store.resolve_slug(ref))
    sections = None
    if sizes.text(paper_id).available:
        try:
            body = _text_of(store, ref, text_url(paper_id))
        except NotFound:
            body = None
        if body is not None:
            sections = [s.name for s in sections_of(body)]
    return paper_record_view(prof, sizes, record, view=view, sections=sections)


def read_paper_text(
    service: Service,
    caller: Caller,
    ref: str,
    paper_id: str,
    *,
    section: Optional[str] = None,
    offset: int = 0,
    max_chars: Optional[int] = None,
):
    """A paper's full text, bounded (a :class:`TextPage`).

    Gated like its content route: a full text this caller may not read is the
    same ``NotFound`` as one that does not exist. An unknown ``section`` is
    ``Invalid(code="unknown_section")`` with the valid names.
    """
    store, prof, viewer = service._read(caller, ref)
    url = text_url(paper_id)
    effective = effective_tiers(prof.metadata)
    if url not in effective or not tier_allows(viewer, effective[url]):
        raise NotFound(f"no full text {paper_id!r} for profile {ref!r}")
    body = _text_of(store, ref, url)
    try:
        return page_text(body, section=section, offset=offset, max_chars=max_chars)
    except ValueError as e:
        raise _unknown_section(e.args[1] if len(e.args) > 1 else []) from e


def read_profile_text(
    service: Service,
    caller: Caller,
    ref: str,
    *,
    section: Optional[str] = None,
    offset: int = 0,
    max_chars: Optional[int] = None,
):
    """The profile's narrative, bounded: ``soul`` and ``expertise`` (a :class:`TextPage`).

    With no section, both parts this caller may read, joined as ``# Soul`` and
    ``# Expertise`` sections; with one, that artifact's stored text alone. The
    page carries ``content_hash``, so an edit needs no second read.
    """
    store, prof, viewer = service._read(caller, ref)
    explain = explain_tiers(prof.metadata)
    md = prof.metadata
    bodies = {
        name: (prof.soul if name == "soul" else prof.expertise)
        for name, url, role in _NARRATIVE
        if artifact_visible(explain, md, url, role, viewer)
    }
    names = [name for name, _url, _role in _NARRATIVE]
    if section is not None and section.lower() not in names:
        raise _unknown_section(names)
    if section is not None:
        section = section.lower()
        if section not in bodies:
            raise NotFound(f"no {section} text for profile {ref!r}")
        page = page_text(bodies[section] or "", section=None, offset=offset, max_chars=max_chars)
        page.section = section
        page.sections = [TextSection(name=section, offset=0, chars=len(bodies[section] or ""))]
    else:
        if not bodies:
            raise NotFound(f"no narrative for profile {ref!r}")
        joined, spans = "", []
        for name in names:
            if name not in bodies:
                continue
            head = f"# {name.capitalize()}\n\n"
            if joined:
                joined += "\n\n"
            spans.append(
                TextSection(
                    name=name, offset=len(joined) + len(head), chars=len(bodies[name] or "")
                )
            )
            joined += head + (bodies[name] or "")
        page = page_text(joined, section=None, offset=offset, max_chars=max_chars)
        page.sections = spans
    page.content_hash = _content_hash(store, store.resolve_slug(ref))
    return page


def find_paper_passages(
    service: Service,
    caller: Caller,
    ref: str,
    paper_id: str,
    query: str,
    k: Optional[int] = None,
):
    """The passages of one paper that best answer ``query`` (a :class:`PassageList`).

    Gated like the works list; a paper this caller cannot see is the same
    ``NotFound`` as one that does not exist.
    """
    store, prof, viewer = service._read(caller, ref)
    if not _works_visible(prof, viewer):
        raise profile_missing(ref)
    record = next((p for p in prof.papers if p.paper_id == paper_id), None)
    if record is None:
        raise NotFound(f"no paper {paper_id!r} in profile {ref!r}")
    return paper_passages(store, prof, viewer, record, query, k)


def find_profile_passages(
    service: Service, caller: Caller, ref: str, query: str, k: Optional[int] = None
):
    """The passages of one profile that answer ``query`` (a :class:`PassageList`).

    Only sources this caller may read are searched.
    """
    store, prof, viewer = service._read(caller, ref)
    return profile_passages(store, prof, viewer, query, k)


__all__ = [
    "Service",
    "find_paper_passages",
    "find_profile_passages",
    "get_paper",
    "get_profile",
    "list_papers",
    "profile_missing",
    "read_paper_text",
    "read_profile_text",
    "split_ids",
    "works_visible",
]
