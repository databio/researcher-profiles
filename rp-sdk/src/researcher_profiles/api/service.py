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
from contextlib import nullcontext
from typing import Any, Literal, Optional

from ..errors import Conflict, Forbidden, Invalid, NotFound, Unauthenticated
from ..models.api import (
    ArtifactTier,
    EditResult,
    FileList,
    PaperPage,
    ProfileSummary,
    SectionTierReport,
    SummaryBatch,
    TextSection,
    VisibilityReport,
)
from ..privacy import (
    SECTION_FIELDS,
    ViewerTier,
    effective_tiers,
    explain_tiers,
    narrow_viewer,
    profile_visible,
    section_tiers,
    tier_allows,
)
from ..profile.edit import EditError, WorkNotFoundError, paper_version
from ..schema import PaperRecord, most_restrictive
from ..store import ProfileNotFoundError, ProfileStore
from ..utils.paths import STORE_CACHE_DIRNAME
from ._cursor import decode_cursor, encode_cursor
from ._limits import BATCH_IDS_CAP, PAPERS_LIMIT, clamp
from ._passages import paper_passages, profile_passages
from ._projection import (
    _content_hash,
    _is_hard_floor,
    _profile_summary,
    artifact_visible,
    served_document_bytes,
    withheld,
)
from ._records import paper_record_view, paper_row, profile_record, summary_text
from ._sizes import EXPERTISE_URL, PAPERS_URL, SOUL_URL, SizeIndex, summary_url, text_url
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

    def __init__(
        self, store: ProfileStore, hooks: Optional[Hooks] = None, *, open_mode: bool = False
    ):
        self.store = store
        self.hooks = hooks if hooks is not None else Hooks()
        #: No operator token is configured (dev mode): with no ``edit_gate``
        #: installed, anyone may edit. ``create_app`` sets it from its token.
        self.open_mode = open_mode
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

        Covers the cached profile, the graph, and the on-disk ``.cache`` memos
        (the same set a tarball push drops). ``store.evict`` also bumps the
        store's write generation, which invalidates ranking. Cache only:
        dependent state belongs on the write hooks. Failures are swallowed
        because a rebuild is cheap and re-eviction is idempotent.
        """
        store = self.store
        store.evict(slug)
        self.graph = None
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
        # Literal names so this works without importing the analytics modules.
        for cache_name in ("centroids.npz", "topics.json", "graph.sqlite"):
            fp = root / STORE_CACHE_DIRNAME / cache_name
            if fp.exists():
                try:
                    fp.unlink()
                except OSError:
                    pass

    # -- the one entry point every read starts from -------------------------

    def load_visible(self, caller: Caller, ref: str) -> tuple[Any, ViewerTier]:
        """``(profile, viewer tier)`` for a profile this caller may see, else ``NotFound``."""
        _store, prof, viewer = self.read(caller, ref)
        return prof, viewer

    def read(self, caller: Caller, ref: str) -> tuple[ProfileStore, Any, ViewerTier]:
        """:meth:`load_visible`, plus the store view the profile came from."""
        store = self.store_for(caller)
        try:
            prof = store.get(ref)
        except (ProfileNotFoundError, KeyError) as e:
            raise profile_missing(ref) from e
        ok, viewer = self.visible(caller, prof, ref)
        if not ok:
            raise profile_missing(ref)
        return store, prof, viewer

    def visible(
        self, caller: Caller, prof: Any, slug: Optional[str] = None
    ) -> tuple[bool, ViewerTier]:
        """``(may this caller be told about prof, their tier for it)``: the profile gate.

        The tier is resolved per profile (a grant is held on one profile, not
        on the rest), so a walk over many profiles asks this for each one.
        Fail-closed: an object that cannot answer for its own tier is hidden.
        """
        md = getattr(prof, "metadata", None)
        if md is None:
            return False, "public"
        slug = slug if slug is not None else getattr(prof, "slug", None)
        viewer = self.viewer(caller, slug)
        return profile_visible(md, viewer, floor=self.floor(caller, prof, slug).tier), viewer


# ---------------------------------------------------------------------------
# Reads
#
# Every read starts from ``Service.read``. A profile or a part this caller may
# not see is the same ``NotFound`` as one that does not exist, so a refusal
# never confirms that something hidden exists.
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
    store, prof, viewer = service.read(caller, ref)
    return profile_record(
        prof, viewer, view=view, proofs=service.proofs(prof.metadata.rid), store=store
    )


def works_visible(service: Service, caller: Caller, ref: str) -> tuple[Any, ViewerTier]:
    """``(profile, viewer tier)`` when this caller may read the profile's works list.

    The works artifact has its own tier; a caller below it gets the profile's
    ``NotFound``.
    """
    prof, viewer = service.load_visible(caller, ref)
    if not _works_visible(prof, viewer):
        raise profile_missing(ref)
    return prof, viewer


def list_profiles(service: Service, caller: Caller) -> list[ProfileSummary]:
    """Every profile this caller may see, summarized. The one listing walk.

    A profile that fails to load or summarize is logged and counted, never
    mistaken for a private one; the listing still answers for the rest.
    """
    store = service.store_for(caller)
    out: list[ProfileSummary] = []
    failed = 0
    for slug in store.list_slugs():
        try:
            prof = store.get(slug)
        # Boundary: one profile's load; the listing still answers for the rest.
        except Exception:
            failed += 1
            logger.exception("could not load profile %s", slug)
            continue
        try:
            ok, viewer = service.visible(caller, prof, slug)
            if ok:
                out.append(_profile_summary(prof, viewer))
        # Boundary: one profile's summary projection; the listing still answers.
        except Exception:
            failed += 1
            logger.exception("could not summarize profile %s", slug)
    if failed:
        logger.warning("listing omitted %d profile(s) that failed to load", failed)
    return out


def list_files(service: Service, caller: Caller, ref: str) -> FileList:
    """The manifest, each entry labeled with its effective tier and slot, plus ``withheld``."""
    _store, prof, viewer = service.read(caller, ref)
    explain = explain_tiers(prof.metadata)
    files = []
    for slot, parts in (
        ("hasPart", prof.metadata.has_part),
        ("subjectOf", prof.metadata.subject_of),
    ):
        for part in parts:
            entry = part.model_dump(mode="json")
            detail = explain.get(part.content_url)
            entry["effective_visibility"] = (
                detail.effective if detail is not None else part.visibility
            )
            entry["slot"] = slot
            files.append(entry)
    return FileList(files=files, withheld=withheld(explain, viewer))


def get_summaries(service: Service, caller: Caller, ref: str, ids: str) -> SummaryBatch:
    """Up to 20 generated summaries, each gated on its own tier; the rest say why not."""
    store, prof, viewer = service.read(caller, ref)
    wanted = split_ids(ids)
    taken, rest = wanted[:BATCH_IDS_CAP], wanted[BATCH_IDS_CAP:]
    sizes = SizeIndex(prof, viewer, store, store.resolve_slug(ref))
    summaries = prof.summaries
    out: dict[str, str] = {}
    unavailable: dict[str, str] = {}
    for pid in taken:
        size = sizes.of(summary_url(pid))
        if size.available and pid in summaries:
            out[pid] = summaries[pid]
        else:
            unavailable[pid] = size.reason or "none"
    return SummaryBatch(
        summaries=out, unavailable=unavailable, limit_applied=len(taken), not_processed=rest
    )


def _document(service: Service, store: ProfileStore, prof: Any, viewer: ViewerTier, ref: str):
    try:
        return served_document_bytes(service, store, prof, viewer, store.resolve_slug(ref))
    except (ProfileNotFoundError, KeyError) as e:
        raise profile_missing(ref) from e


def get_document(service: Service, caller: Caller, ref: str) -> tuple[bytes, Any, ViewerTier]:
    """``(profile.jsonld bytes, profile, viewer tier)``: the served document (see ``served_document_bytes``)."""
    store, prof, viewer = service.read(caller, ref)
    return _document(service, store, prof, viewer, ref), prof, viewer


def get_artifact(
    service: Service, caller: Caller, ref: str, artifact: str
) -> tuple[bytes, Optional[str], Any, ViewerTier, Optional[str]]:
    """One manifest artifact: ``(bytes, media type, profile, viewer tier, effective tier)``.

    ``profile.jsonld`` is the served document (effective tier ``None``). The
    hard floors (``.cache/``, ``.keys/``) are ``Forbidden`` for everyone.
    """
    store, prof, viewer = service.read(caller, ref)
    resolved = store.resolve_slug(ref)
    if artifact == "profile.jsonld":
        return _document(service, store, prof, viewer, resolved), None, prof, viewer, None
    effective = effective_tiers(prof.metadata)
    unknown = NotFound(f"artifact {artifact!r} is not a manifest artifact of {resolved!r}")
    part = next((p for p in prof.manifest() if p.content_url == artifact), None)
    if artifact not in effective or part is None:
        raise unknown
    if _is_hard_floor(artifact):
        raise Forbidden("build-local derived state is not a servable artifact")
    if not tier_allows(viewer, effective[artifact]):
        raise unknown
    try:
        data = store.artifact_bytes(resolved, artifact)
    except (ProfileNotFoundError, KeyError) as e:
        raise NotFound(
            f"artifact {artifact!r} is listed in the manifest of {resolved!r} "
            "but its content was not uploaded to this registry"
        ) from e
    return data, part.encoding_format, prof, viewer, effective[artifact]


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
    store, prof, viewer = service.read(caller, ref)
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
    """One paper: fields, inline summary, sizes, version (a :class:`PaperRecordView`).

    Gated like the works list.
    """
    store, prof, viewer = service.read(caller, ref)
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

    Gated on the text artifact's own tier. An unknown ``section`` is
    ``Invalid(code="unknown_section")`` with the valid names.
    """
    store, prof, viewer = service.read(caller, ref)
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
    store, prof, viewer = service.read(caller, ref)
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

    Gated like the works list.
    """
    store, prof, viewer = service.read(caller, ref)
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
    store, prof, viewer = service.read(caller, ref)
    return profile_passages(store, prof, viewer, query, k)


# ---------------------------------------------------------------------------
# Edits
#
# Every edit loads the stored profile (never a caller's view of it), runs the
# host's edit gate, checks the caller's version token, runs the host's
# write-scope check for each action, writes in one write unit, records the
# edit through ``hooks.record_edit``, and drops the caches.
# ---------------------------------------------------------------------------


def _load_for_edit(service: Service, caller: Caller, ref: str, *, read_ok: bool = False):
    """The stored profile, once the edit gate (``require_edit``) has admitted this caller.

    With no host gate the credential is checked before the lookup, so a caller
    who may not edit gets the same ``Unauthenticated`` whether or not ``ref``
    exists: a 404 for one and a 401 for the other would disclose a held-back
    profile.
    """
    if service.hooks.edit_gate is None and not (caller.is_operator or service.open_mode):
        raise Unauthenticated("invalid or missing bearer token")
    try:
        prof = service.store.get(ref)
    except (ProfileNotFoundError, KeyError) as e:
        raise profile_missing(ref) from e
    require_edit(service, caller, prof, read_ok=read_ok, ref=ref)
    return prof


def require_edit(
    service: Service,
    caller: Caller,
    prof: Any,
    *,
    read_ok: bool = False,
    ref: Optional[str] = None,
) -> None:
    """May this caller edit ``prof`` (or, with ``read_ok``, read its owner tooling)?

    ``hooks.edit_gate(caller, prof, read_ok=..., ref=...)`` when a host
    installed one; it raises ``Unauthenticated``, ``Forbidden`` or
    ``NotFound``. ``ref`` is the caller's own input, so a refusal (a gate's
    ``NotFound`` included) never names the resolved slug. Without a gate, the
    operator credential edits (and anyone in open mode); everyone else is
    ``Unauthenticated``.
    """
    gate = service.hooks.edit_gate
    if gate is not None:
        ref = ref if ref is not None else prof.slug
        try:
            gate(caller, prof, read_ok=read_ok, ref=ref)
        except NotFound as e:
            raise profile_missing(ref) from e
        return
    if caller.is_operator or service.open_mode:
        return
    raise Unauthenticated("invalid or missing bearer token")


def _write_scope(service: Service, caller: Caller, prof: Any, action: str, detail: dict) -> None:
    hook = service.hooks.write_scope
    if hook is not None:
        hook(caller, prof, action, detail)


def _record(
    service: Service, caller: Caller, prof: Any, action: str, fields: list, content_hash
) -> None:
    hook = service.hooks.record_edit
    if hook is not None:
        hook(caller, prof, action, list(fields), content_hash)


def _check_base_hash(store: ProfileStore, ref: str, base_hash: Optional[str]) -> None:
    """Refuse an edit composed against a version other than the current one.

    Opt-in: a caller that sends no ``base_hash`` gets last-writer-wins. The
    digest spans the document and the SOUL together, so metadata and soul
    writes conflict with each other rather than each keeping a private clock.
    """
    if base_hash is None:
        return
    current = _content_hash(store, ref)
    if current is None or current == base_hash:
        return
    raise Conflict(
        "this profile changed since you loaded it; reload it and re-apply your edit",
        current=current,
        kind="profile",
    )


def _check_base_version(record: PaperRecord, base_version: Optional[str]) -> None:
    """Refuse a work edit composed against another version of that work. Opt-in."""
    if base_version is None:
        return
    current = paper_version(record)
    if current == base_version:
        return
    raise Conflict(
        "this paper changed since you loaded it; reload it and re-apply your edit",
        current=current,
        kind="paper",
    )


def _work(prof, paper_id: str) -> PaperRecord:
    for record in prof.papers:
        if record.paper_id == paper_id:
            return record
    raise NotFound(f"no work with paper_id {paper_id!r} in this profile")


def _work_edit_error(e: EditError):
    """``NotFound`` when the corpus has no such ``paper_id``, else ``Invalid``."""
    if isinstance(e, WorkNotFoundError):
        return NotFound(str(e))
    return Invalid(str(e))


def edit_metadata(
    service: Service,
    caller: Caller,
    ref: str,
    patch: dict,
    *,
    base_hash: Optional[str] = None,
) -> EditResult:
    """Patch owner-editable metadata, the narrative (``soul``) included.

    Only the fields in ``patch`` change. ``soul`` replaces the narrative in the
    same write unit, so the edit is atomic and ``content_hash`` moves once. A
    stale ``base_hash`` is ``Conflict(kind="profile")``; a patch that would
    produce an invalid document is ``Invalid`` and changes nothing.
    """
    store = service.store
    prof = _load_for_edit(service, caller, ref)
    patch = dict(patch)
    # Neither is a metadata field: ``slug`` is the address and renaming is not
    # an edit; ``base_hash`` is the concurrency token.
    patch.pop("slug", None)
    patch.pop("base_hash", None)
    has_soul = "soul" in patch
    soul = patch.pop("soul", None)
    _check_base_hash(store, store.resolve_slug(ref), base_hash)
    actions: list[tuple[str, list[str]]] = []
    if patch or not has_soul:
        actions.append(("metadata", sorted(patch)))
    if has_soul:
        actions.append(("soul", []))
    for action, fields in actions:
        _write_scope(
            service, caller, prof, action, {"fields": fields} if action == "metadata" else {}
        )
    try:
        # One write unit when both halves change, so the edit is atomic and the
        # hooks run once. The unit is named for the narrative, the half a hook
        # that cares about embedded or overridable text must not miss.
        with prof.write_unit("soul") if (patch and has_soul) else nullcontext():
            if patch:
                prof.edit.patch_metadata(patch)
            if has_soul:
                prof.edit.set_soul(soul if soul is not None else "")
    except EditError as e:
        raise Invalid(str(e)) from e
    resolved = store.resolve_slug(ref)
    new_hash = _content_hash(store, resolved)
    for action, fields in actions:
        _record(service, caller, prof, action, fields, new_hash)
    service.invalidate(resolved)
    return EditResult(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        updated=sorted([*patch.keys(), *(["soul"] if has_soul else [])]),
        content_hash=new_hash,
    )


def edit_work(
    service: Service,
    caller: Caller,
    ref: str,
    paper_id: str,
    patch: dict,
    *,
    base_version: Optional[str] = None,
) -> EditResult:
    """Patch owner-editable fields of one work. A stale ``base_version`` is a ``Conflict``."""
    store = service.store
    prof = _load_for_edit(service, caller, ref)
    patch = dict(patch)
    patch.pop("base_version", None)
    _check_base_version(_work(prof, paper_id), base_version)
    fields = sorted(patch)
    _write_scope(service, caller, prof, "works", {"paper_id": paper_id, "fields": fields})
    try:
        updated = prof.edit.patch_work(paper_id, patch)
    except EditError as e:
        raise _work_edit_error(e) from e
    resolved = store.resolve_slug(ref)
    new_hash = _content_hash(store, resolved)
    _record(service, caller, prof, "works", fields, new_hash)
    service.invalidate(resolved)
    return EditResult(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        updated=fields,
        content_hash=new_hash,
        version=paper_version(updated),
    )


def add_work(service: Service, caller: Caller, ref: str, record: dict) -> EditResult:
    """Add one new work. Never overwrites: an existing ``paper_id`` is ``Conflict(kind="exists")``.

    ``EditManager.add_work`` keeps its replace-or-append semantics for the
    CLI; the never-overwrite rule is this function's.
    """
    store = service.store
    prof = _load_for_edit(service, caller, ref)
    paper_id = str(record.get("paper_id") or "").strip() if isinstance(record, dict) else ""
    if not paper_id:
        raise Invalid("a work record needs a paper_id")
    if any(r.paper_id == paper_id for r in prof.papers):
        raise Conflict("paper_id already exists; use PATCH", kind="exists")
    _write_scope(service, caller, prof, "works", {"paper_id": paper_id, "fields": ["*"]})
    try:
        added = prof.edit.add_work({**record, "paper_id": paper_id})
    except EditError as e:
        raise _work_edit_error(e) from e
    resolved = store.resolve_slug(ref)
    new_hash = _content_hash(store, resolved)
    _record(service, caller, prof, "works", ["*"], new_hash)
    service.invalidate(resolved)
    return EditResult(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        updated=[paper_id],
        content_hash=new_hash,
        version=paper_version(added),
    )


def remove_work(
    service: Service,
    caller: Caller,
    ref: str,
    paper_id: str,
    *,
    base_version: Optional[str] = None,
) -> EditResult:
    """Remove one work. A stale ``base_version`` is a ``Conflict`` and the work stays."""
    store = service.store
    prof = _load_for_edit(service, caller, ref)
    _check_base_version(_work(prof, paper_id), base_version)
    _write_scope(service, caller, prof, "works", {"paper_id": paper_id, "fields": []})
    try:
        prof.edit.remove_work(paper_id)
    except EditError as e:
        raise _work_edit_error(e) from e
    resolved = store.resolve_slug(ref)
    new_hash = _content_hash(store, resolved)
    _record(service, caller, prof, "works", [], new_hash)
    service.invalidate(resolved)
    return EditResult(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        updated=[paper_id],
        content_hash=new_hash,
    )


def set_visibility(
    service: Service,
    caller: Caller,
    ref: str,
    *,
    profile_visibility=None,
    artifacts: Optional[list[dict]] = None,
    sections: Optional[list[dict]] = None,
    base_hash: Optional[str] = None,
) -> EditResult:
    """Set the profile-level default tier and/or per-artifact and per-section tiers."""
    store = service.store
    prof = _load_for_edit(service, caller, ref)
    artifacts = list(artifacts or [])
    sections = list(sections or [])
    _check_base_hash(store, store.resolve_slug(ref), base_hash)
    _write_scope(
        service,
        caller,
        prof,
        "visibility",
        {
            "slug": ref,
            "profile_visibility": profile_visibility,
            "artifacts": artifacts,
            "sections": sections,
        },
    )
    try:
        _doc, changed = prof.edit.set_visibility(
            profile_visibility=profile_visibility,
            artifacts=artifacts or None,
            sections=sections or None,
        )
    except EditError as e:
        raise Invalid(str(e)) from e
    resolved = store.resolve_slug(ref)
    new_hash = _content_hash(store, resolved)
    _record(service, caller, prof, "visibility", [], new_hash)
    service.invalidate(resolved)
    updated = []
    if profile_visibility is not None:
        updated.append("visibility")
    if artifacts:
        updated.append("artifacts")
    if sections:
        updated.append("sections")
    return EditResult(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        updated=updated,
        artifacts_changed=changed,
        content_hash=new_hash,
    )


def get_visibility(service: Service, caller: Caller, ref: str) -> VisibilityReport:
    """What is published, to whom, and why: the read side of :func:`set_visibility`.

    Owner tooling: the edit gate with ``read_ok`` (a caller who may read the
    profile whole is admitted too). Every row is a projection of
    ``privacy.explain_tiers`` and ``privacy.tier_allows``.
    """
    store = service.store
    prof = _load_for_edit(service, caller, ref, read_ok=True)
    resolved = store.resolve_slug(ref)
    explain = explain_tiers(prof.metadata)
    floor = service.floor(caller, prof, ref)

    viewers: dict[str, ViewerTier] = {"anonymous": "public", "lab": "limited", "you": "private"}
    counts = dict.fromkeys(viewers, 0)
    artifacts: list[ArtifactTier] = []
    for entry in explain.values():
        floored = _is_hard_floor(entry.content_url)
        visible_to = []
        for label, tier in viewers.items():
            if not floored and tier_allows(tier, entry.effective):
                visible_to.append(label)
                counts[label] += 1
        artifacts.append(
            ArtifactTier(
                content_url=entry.content_url,
                role=entry.role,
                name=entry.name,
                paper_id=entry.paper_id,
                declared=entry.declared,
                effective=entry.effective,
                raised_by=list(entry.raised_by),
                visible_to=visible_to,
            )
        )
    artifacts.sort(key=lambda a: a.content_url)

    # One row per inline section, in SECTION_FIELDS order, with the declared
    # and the governing tier. ``soul`` governs no inline field: it reaches
    # personality/SOUL.md through the manifest, so its ``declared`` folds in
    # the soul artifact's own declared tier.
    section_effective = section_tiers(prof.metadata)
    declared_map = {x.section: x.visibility for x in prof.metadata.section_visibility}
    soul_declared = [e.declared for e in explain.values() if e.role == "soul"]
    sections: list[SectionTierReport] = []
    for section in SECTION_FIELDS:
        declared = declared_map.get(section, "public")
        if section == "soul":
            declared = most_restrictive(declared, *soul_declared)
        effective = section_effective[section]
        sections.append(
            SectionTierReport(
                section=section,
                declared=declared,
                effective=effective,
                visible_to=[
                    label for label, tier in viewers.items() if tier_allows(tier, effective)
                ],
            )
        )

    return VisibilityReport(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        profile_visibility=prof.metadata.visibility,
        profile_floor=floor.tier,
        profile_floor_reason=floor.reason if floor.tier else None,
        artifacts=artifacts,
        sections=sections,
        counts=counts,
    )


__all__ = [
    "Service",
    "add_work",
    "edit_metadata",
    "edit_work",
    "find_paper_passages",
    "find_profile_passages",
    "get_artifact",
    "get_document",
    "get_paper",
    "get_profile",
    "get_summaries",
    "get_visibility",
    "list_files",
    "list_papers",
    "list_profiles",
    "profile_missing",
    "read_paper_text",
    "read_profile_text",
    "remove_work",
    "require_edit",
    "set_visibility",
    "split_ids",
    "works_visible",
]
