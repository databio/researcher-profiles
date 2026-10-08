"""The public read surface: the profile listing and one profile's artifacts.

Every route here hangs on ``public_router`` and resolves a viewer tier, then
projects its response against that tier. An anonymous request is not a
different code path; it is the viewer whose tier is ``public``.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import Depends, HTTPException, Query, Request, Response

from ... import __version__
from ...models.api import (
    FileList,
    PaperPage,
    PaperRecordView,
    ProfileListEntry,
    ProfileListResponse,
    ProfileRecord,
    ProfileSummary,
    SummaryBatch,
    TextPage,
    TextSection,
)
from ...models.published import ProfileCard, ProfileCollection
from ...privacy import (
    ALWAYS_PRIVATE_PREFIXES,
    ViewerTier,
    effective_tiers,
    explain_tiers,
    profile_visible,
    tier_allows,
)
from ...schema import (
    PaperRecord,
    validate_ref,
)
from ...schema.jsonld import CONTEXT_URL
from ...store import ProfileNotFoundError, ProfileStore
from .._cursor import decode_cursor, encode_cursor
from .._limits import BATCH_IDS_CAP, PAPERS_LIMIT, clamp
from .._projection import (
    PUBLIC_DOCUMENT_MAX_AGE,
    VARY_ON_CREDENTIALS,
    _content_hash,
    _gate_profile,
    _http_date,
    _profile_missing,
    _profile_summary,
    artifact_visible,
    served_document_bytes,
    withheld,
)
from .._records import paper_record_view, paper_row, profile_record, summary_text
from .._sizes import (
    EXPERTISE_URL,
    PAPERS_URL,
    SOUL_URL,
    SizeIndex,
    summary_url,
    text_url,
)
from .._text import page_text, sections_of
from ..deps import (
    get_caller,
    get_profile,
    get_profile_tier_floor,
    get_service,
    get_store,
    get_viewer_tier,
    viewer_tier_for,
)
from ._routers import public_router

logger = logging.getLogger(__name__)


def _cache_headers(
    viewer: ViewerTier,
    *,
    etag: str | None = None,
    last_modified_iso: str | None = None,
) -> dict[str, str]:
    """Cache directives for a tier-projected response.

    Only the anonymous rendering is shareable, and only briefly: every other
    tier is somebody's private view of a profile and is never stored.
    """
    headers = {
        "Cache-Control": (
            f"public, max-age={PUBLIC_DOCUMENT_MAX_AGE}"
            if viewer == "public"
            else "private, no-store"
        ),
        "Vary": VARY_ON_CREDENTIALS,
    }
    if etag is not None:
        headers["ETag"] = etag
    lm = _http_date(last_modified_iso)
    if lm is not None:
        headers["Last-Modified"] = lm
    return headers


def _visible_summaries(request: Request, store: ProfileStore) -> list[ProfileSummary]:
    """Every profile this caller may see, summarized. The one listing walk.

    ``GET /profiles`` returns the profile list; ``GET /collection.json`` returns
    the ranking bundle. They differ in shape and in what they carry, but both
    must answer for the same set of visible profiles, so they share this one
    walk rather than each deciding for itself who is in it. A second walk is a
    second privacy implementation; the projection exists so there is only one.
    """
    out: list[ProfileSummary] = []
    failed = 0
    for slug in store.list_slugs():
        try:
            prof = store.get(slug)
        # Boundary: one profile's load; the listing still answers for the rest.
        except Exception:
            # A load failure is an outage, not a privacy decision, and the two
            # must never look alike from here: a profile that vanishes because
            # its bytes are unreadable has to be countable, or a corrupted
            # store reads as a store full of private profiles.
            failed += 1
            logger.exception("could not load profile %s", slug)
            continue
        try:
            # Per profile, not per request: the caller's tier depends on which
            # profile, so an owner's own held-back profile belongs in their list.
            per_profile = viewer_tier_for(request, slug)
            floor = get_profile_tier_floor(request, prof, slug)
            if not profile_visible(prof.metadata, per_profile, floor=floor.tier):
                continue
            out.append(_profile_summary(prof, per_profile))
        # Boundary: one profile's summary projection; the listing still answers.
        except Exception:
            failed += 1
            logger.exception("could not summarize profile %s", slug)
    if failed:
        logger.warning("listing omitted %d profile(s) that failed to load", failed)
    return out


@public_router.get("/profiles")
def list_profiles(
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),  # noqa: ARG001
) -> Response:
    """The profile list, in ``rp:profileList`` format.

    Returns the same envelope a static server publishes as ``profiles.json``,
    with enriched entries that include summary fields. Projected through the
    caller's viewer tier: a profile this viewer may not see is absent.

    Never stored by a shared cache: the membership of this list is a function of
    who asked, so one caller's copy is nobody else's answer.
    """
    summaries = _visible_summaries(request, store)
    origin = _public_origin(request)
    envelope = ProfileListResponse(
        name=store.name if hasattr(store, "name") else None,
        url=str(request.url.replace(query="")),
        updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        profiles=[
            ProfileListEntry(
                url=f"{origin}/api/v1/profiles/{s.slug}/content/",
                slug=s.slug,
                rid=s.rid,
                name=s.name,
                level=s.level,
                affiliation=s.affiliation,
                field=s.field,
                paper_count=s.paper_count,
                summary_count=s.summary_count,
                fulltext_pct=s.fulltext_pct,
                contaminated_count=s.contaminated_count,
            )
            for s in summaries
        ],
    )
    return Response(
        content=envelope.model_dump_json(by_alias=True),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store", "Vary": VARY_ON_CREDENTIALS},
    )


def _public_origin(request: Request) -> str:
    """The scheme + host a client reached this server on, no trailing slash.

    ``request.base_url`` is what the ASGI server saw, which behind a TLS-
    terminating reverse proxy is ``http://``, a URL the browser then refuses to
    load from an ``https://`` page. The forwarded headers are the proxy's
    statement of what the client actually asked for, so they win when present.

    Only used to make the collection bundle's ``base`` URLs absolute. They have to
    be: a client resolves a manifest against them with ``new URL(base)``, which
    has no document to resolve a root-relative path against.
    """
    base = request.base_url
    scheme = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    return f"{scheme or base.scheme}://{host or base.netloc}"


@public_router.get("/collection.json")
def get_collection(
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),  # noqa: ARG001
) -> Response:
    """The collection as a **bundle document**, for a browser client's home view.

    This exists because the SPA's home source is a document URL, not an API
    call, and on a hosted registry there was no document at that URL to fetch.
    It answers for the same set of visible profiles as ``GET /profiles``, under
    the same projection, so one explorer build reads a hosted registry and a
    rendered directory of static files with the same code.

    It is not identical to the static ``collection.jsonld`` a published site
    writes. This dynamic bundle carries ``artifacts: []`` and no centroids: the
    sqlite index the centroids come from is a server-only floor (``.cache/``),
    so a client ranks against a hosted registry by calling ``/match`` on the
    server. The static ``collection.jsonld`` carries the stacked centroids
    inline, so a client ranks that collection itself, offline.

    Each card's ``base`` is the absolute
    ``.../api/v1/profiles/{slug}/content/``: the base URL
    ``get_profile_artifact`` serves, from which ``profile.jsonld`` and every
    relative ``contentUrl`` inside it resolve. Absolute rather than
    root-relative because a client resolves the manifest against it with a bare
    URL parse, which has no document to resolve a relative path against.

    ``Cache-Control: private, no-store``: the membership of this list is a
    function of who asked, so a shared cache holding one caller's copy would
    hand a stranger an owner's collection.
    """
    summaries = _visible_summaries(request, store)
    origin = _public_origin(request)
    bundle = ProfileCollection(
        **{
            "@context": CONTEXT_URL,
            "@id": str(request.url.replace(query="")),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "generator": f"researcher-profiles/{__version__}",
            # No centroid blob is served over HTTP: the sqlite index it is
            # derived from is a hard floor (``.cache/``), so the bundle carries
            # no ``artifacts`` and clients fall back to server-side /match.
            "backend_spec": None,
            "dim": None,
            "count": len(summaries),
            "cards": [
                ProfileCard(
                    slug=s.slug,
                    rid=s.rid or "",
                    name=s.name,
                    level=s.level,
                    affiliation=s.affiliation,
                    field=s.field,
                    paper_count=s.paper_count,
                    summary_count=s.summary_count,
                    fulltext_pct=s.fulltext_pct,
                    base=f"{origin}/api/v1/profiles/{s.slug}/content/",
                )
                for s in summaries
            ],
            "artifacts": [],
        }
    )
    return Response(
        content=bundle.model_dump_json(by_alias=True),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store", "Vary": VARY_ON_CREDENTIALS},
    )


@public_router.get(
    "/profiles/{slug}",
    response_model=ProfileRecord,
    response_model_exclude_none=True,
)
def get_profile_detail(
    slug: str,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
    view: Literal["record", "full"] = "record",
) -> Response:
    """One profile, sized for this caller.

    ``view="record"`` (the default) is the trimmed record: the common fields,
    long lists cut to their top entries with a total, the edit version
    (``content_hash``) and, under ``parts``, what this caller may fetch of the
    narrative and the works file and how big each is. It stays under 8 KB.
    ``view="full"`` carries every metadata field untrimmed plus the ``soul``
    and ``expertise`` bodies, for an edit form or a client that mirrors the
    profile. Neither view carries the file manifest: that is
    ``GET /profiles/{slug}/files``.

    A field this caller may not see is ``null`` and named in ``withheld``,
    never ``""``: a client has to be able to tell withheld from empty.

    ``ETag`` is weak and derived from ``content_hash`` (plus the viewer tier and
    the view), so ``If-None-Match`` answers 304 for one stored-column read.
    """
    prof = get_profile(slug, store)
    _gate_profile(get_service(request), get_caller(request), prof, viewer, slug)
    resolved = store.resolve_slug(slug)
    current = _content_hash(store, resolved)
    headers = {"Cache-Control": "private, no-store", "Vary": VARY_ON_CREDENTIALS}
    if current:
        etag = f'W/"{current.split(":", 1)[-1][:32]}-{viewer}-{view}"'
        headers["ETag"] = etag
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
    record = profile_record(
        prof, viewer, view=view, proofs=get_service(request).proofs(prof.metadata.rid), store=store
    )
    return Response(
        content=record.model_dump_json(exclude_none=True),
        media_type="application/json",
        headers=headers,
    )


@public_router.get("/profiles/{slug}/files", response_model=FileList)
def get_profile_files(
    slug: str,
    request: Request,
    response: Response,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> FileList:
    """The profile's manifest, each entry labeled with its effective tier.

    Served whole at every tier (spec section 6, tier-invariant), with each
    entry's ``effective_visibility`` so no client recomputes the derivation
    rule, its ``slot`` (``hasPart`` or ``subjectOf``), and ``withheld`` naming
    what this viewer may not read. A push client reads this to learn what the
    server holds.
    """
    prof = get_profile(slug, store)
    _gate_profile(get_service(request), get_caller(request), prof, viewer, slug)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = VARY_ON_CREDENTIALS
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


@public_router.get(
    "/profiles/{slug}/profile.jsonld",
)
def get_profile_jsonld(
    slug: str,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> Response:
    """Serve the profile's ``profile.jsonld``: the stored record plus registry proofs.

    The served document is the stored record plus any registry-issued proofs
    (``orcid_login``), which the registry computes on every request from its
    own live state and never stores (``app.state.registry_proofs``). A profile
    with no section projection and no registry proof is served as the exact
    bytes the store persisted (``store.document_bytes``), so the
    ``conformsTo`` claim is about a file anyone can retrieve byte for byte.
    ``/profiles/{slug}/content/profile.jsonld`` returns the same bytes.
    """
    try:
        validate_ref(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    prof = get_profile(slug, store)
    _gate_profile(get_service(request), get_caller(request), prof, viewer, slug)
    return _document_response(request, store, prof, viewer, slug)


def _document_response(
    request: Request, store: ProfileStore, prof, viewer: ViewerTier, slug: str
) -> Response:
    """The one ``profile.jsonld`` response, shared by both document URLs.

    Gated by the profile tier only (the caller has already run the gate);
    inline sections are projected when the profile declares section
    visibility, and registry-issued proofs are attached (see
    :func:`served_document_bytes`).
    """
    try:
        data = served_document_bytes(
            get_service(request), store, prof, viewer, store.resolve_slug(slug)
        )
    except (ProfileNotFoundError, KeyError) as e:
        raise _profile_missing(slug) from e
    # A strong etag over the served bytes, so the short revalidation above is a
    # 304 rather than a re-send. It is the document itself, not a timestamp: two
    # replicas serving the same profile agree on it, and a change in a
    # registry proof changes it.
    etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
    headers = _cache_headers(viewer, etag=etag, last_modified_iso=prof.metadata.date_modified)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(
        content=data,
        media_type="application/ld+json",
        headers=headers,
    )


@public_router.get("/profiles/{slug}/content/{artifact:path}")
def get_profile_artifact(
    slug: str,
    artifact: str,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> Response:
    """Serve one manifest artifact, projected through the caller's viewer tier.

    This is the one artifact-serving route, and it is the read plane a browser
    client actually needs: ``/api/v1/profiles/{slug}/content/`` is a base URL
    from which ``profile.jsonld`` and every relative ``contentUrl`` the manifest
    names resolve. Without it the SPA could fetch a profile document from a
    hosted registry and then fail on every artifact inside it.

    The gate is the viewer tier, not the router. A hosted registry keeps the
    whole profile, every tier, in one store, so an owner-or-nothing route
    would be the wrong shape: it would lock a signed-in owner out of the public
    view and give nobody a way to read the public tier without a credential.
    An owner reads their own ``private`` CV because a management host's
    resolver gives them the ``private`` viewer tier, not because this path
    is reserved for them.

    Three refusals, in order:

    * the profile gate: 404, byte-identical to a nonexistent slug;
    * the hard floors of ``docs/rp-spec/privacy.md``: **403**, and
      not a 404: these are withheld from *everyone*, the owner
      included, so there is no viewer for whom their existence is a secret and
      nothing is disclosed by saying why.

      - ``.cache/`` and ``.keys/``: build-local derived state (the sqlite
        embedding index) and key material, not servable profile artifacts.

    * the artifact gate: 404, byte-identical to an artifact that is not in the
      manifest at all. "Not for you" and "not there" must not be tellable apart,
      or the error message enumerates the private half of the profile.

    Past all three, a manifest entry whose body the store does not hold (a
    push withheld it) is a 404 that says so. Only a caller the tier gate
    admitted can reach it, so it discloses nothing they may not see.

    Only paths the manifest declares are servable; an unknown or traversing path
    is a 404. ``X-RP-Effective-Tier`` reports the tier the derivation rule
    resolved (``privacy.effective_tiers``), so a derivative never advertises a
    looser tier than its sources.
    """
    prof = get_profile(slug, store)
    _gate_profile(get_service(request), get_caller(request), prof, viewer, slug)
    resolved = store.resolve_slug(slug)

    # ``profile.jsonld`` is the manifest, not an entry in it, so it is resolved
    # here rather than through the manifest lookup below. This is what makes
    # ``/content/`` a usable base URL: a client points at one directory and
    # follows relative contentUrls out of the document it finds there, exactly
    # as it does against a static site. The same response as
    # ``/profiles/{slug}/profile.jsonld``: same bytes, same ETag.
    if artifact == "profile.jsonld":
        return _document_response(request, store, prof, viewer, resolved)

    # Only manifest artifacts are servable. Keying off effective_tiers (rather
    # than the raw path) both bounds the surface to declared artifacts and gives
    # us the derivation-correct tier in one lookup.
    effective = effective_tiers(prof.metadata)
    unknown = HTTPException(
        status_code=404,
        detail=f"artifact {artifact!r} is not a manifest artifact of {resolved!r}",
    )
    if artifact not in effective:
        raise unknown
    part = next((p for p in prof.manifest() if p.content_url == artifact), None)
    if part is None:  # pragma: no cover - effective_tiers is built from the manifest
        raise unknown

    # Hard floors: never served, to anybody, at any tier.
    if any(artifact.startswith(prefix) for prefix in ALWAYS_PRIVATE_PREFIXES):
        raise HTTPException(
            status_code=403,
            detail="build-local derived state is not a servable artifact",
        )

    if not tier_allows(viewer, effective[artifact]):
        raise unknown

    # The store owns retrieval, including the traversal refusal a directory
    # backend needs and a relational one structurally cannot need.
    #
    # A manifest row can outlive its body: a push that withholds a class of
    # files (``sources/papers/`` fulltext is withheld by default) still commits
    # a manifest listing them. The caller has already passed the tier gate
    # here, so they may know the artifact exists; telling them it is "not a
    # manifest artifact" would be false and sends them hunting for a
    # permission problem. Say what is actually wrong instead.
    try:
        data = store.artifact_bytes(resolved, artifact)
    except (ProfileNotFoundError, KeyError) as e:
        raise HTTPException(
            status_code=404,
            detail=(
                f"artifact {artifact!r} is listed in the manifest of {resolved!r} "
                "but its content was not uploaded to this registry"
            ),
        ) from e

    return Response(
        content=data,
        media_type=part.encoding_format or "application/octet-stream",
        headers={
            "X-RP-Effective-Tier": effective[artifact],
            "Cache-Control": "private, no-store",
            "Vary": VARY_ON_CREDENTIALS,
        },
    )


def _works_visible(prof, viewer: ViewerTier) -> bool:
    return artifact_visible(
        explain_tiers(prof.metadata), prof.metadata, PAPERS_URL, "works", viewer
    )


def _year_key(record: PaperRecord) -> int:
    return record.year if isinstance(record.year, int) else -1


def _split_ids(ids: str) -> list[str]:
    out: list[str] = []
    for raw in ids.split(","):
        pid = raw.strip()
        if pid and pid not in out:
            out.append(pid)
    return out


@public_router.get(
    "/profiles/{slug}/papers",
    response_model=PaperPage,
    response_model_exclude_none=True,
)
def list_papers(
    slug: str,
    request: Request,
    response: Response,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    q: Optional[str] = None,
    year_min: Optional[int] = None,
    missing_ids: bool = False,
    has_text: Optional[bool] = None,
    ids: Optional[str] = None,
) -> PaperPage:
    """The works list as rich rows, paged, gated on the tier of ``sources/papers.jsonld``.

    Each row carries what a caller needs to choose its next read: identity,
    a short summary, the ``summary`` and full-text ``text`` sizes *this caller*
    may fetch (``{available, bytes, approx_tokens, reason}``), and the paper's
    ``version`` for an edit. Advertising a part the caller cannot retrieve
    would send them into a 404 and teach them the profile is broken rather
    than private.

    Without ``q``, rows are newest first (``year`` desc, then ``paper_id``),
    paged with a keyset cursor. With ``q``, rows are ranked by hybrid search
    (meaning, over the stored summary and abstract vectors, plus BM25 keywords
    over title, summary, abstract and journal, merged by reciprocal rank),
    paged with an offset cursor; ``search_mode_used`` and ``note`` say what
    ran. ``year_min``, ``missing_ids`` (no DOI and no OpenAlex id) and
    ``has_text`` filter before ranking. ``limit`` defaults to 20 and is
    clamped to 100 (``limit_applied``). A cursor is bound to the filters it was
    made under; reusing it with others is a 400 ``cursor_mismatch``.

    ``ids`` (a comma list, at most 20) reads those rows only, in that order,
    with no paging.
    """
    prof = get_profile(slug, store)
    _gate_profile(get_service(request), get_caller(request), prof, viewer, slug)
    if not _works_visible(prof, viewer):
        raise _profile_missing(slug)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = VARY_ON_CREDENTIALS
    sizes = SizeIndex(prof, viewer, store, store.resolve_slug(slug))
    papers = [p for p in prof.papers if p.paper_id]

    if ids is not None:
        wanted = _split_ids(ids)
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
                decode_cursor("", filters)  # raises the cursor_mismatch 400
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

    from .._semantic import hybrid_rank_papers

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


def _find_paper(prof, paper_id: str, slug: str) -> PaperRecord:
    for record in prof.papers:
        if record.paper_id == paper_id:
            return record
    raise HTTPException(status_code=404, detail=f"paper {paper_id!r} not found in profile {slug!r}")


def _text_of(store: ProfileStore, slug: str, content_url: str) -> str:
    """One artifact's stored text; a 404 that says so when its body was never pushed."""
    try:
        return store.artifact_bytes(store.resolve_slug(slug), content_url).decode(
            "utf-8", errors="replace"
        )
    except (ProfileNotFoundError, KeyError) as e:
        raise HTTPException(
            status_code=404,
            detail=f"{content_url!r} is listed for {slug!r} but its content was not uploaded",
        ) from e


@public_router.get(
    "/profiles/{slug}/papers/{paper_id}",
    response_model=PaperRecordView,
    response_model_exclude_none=True,
)
def get_paper(
    slug: str,
    paper_id: str,
    request: Request,
    response: Response,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
    view: Literal["record", "full"] = "record",
) -> PaperRecordView:
    """One paper: its fields, its summary inline, its sizes, and its version.

    Gated like the works list (``sources/papers.jsonld``). ``summary`` is the
    generated summary when this caller may read it, else the record's own
    ``summary`` field (``summary_source`` says which). ``sections`` lists the
    full text's headings when this caller may read the text, so a section read
    needs no offset arithmetic. ``view="record"`` trims long lists (first 10
    authors, 5 topics) and cuts the abstract at 1,500 chars; ``view="full"``
    returns the whole record.
    """
    prof = get_profile(slug, store)
    _gate_profile(get_service(request), get_caller(request), prof, viewer, slug)
    if not _works_visible(prof, viewer):
        raise _profile_missing(slug)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = VARY_ON_CREDENTIALS
    record = _find_paper(prof, paper_id, slug)
    sizes = SizeIndex(prof, viewer, store, store.resolve_slug(slug))
    sections = None
    if sizes.text(paper_id).available:
        try:
            body = _text_of(store, slug, text_url(paper_id))
        except HTTPException:
            body = None
        if body is not None:
            sections = [s.name for s in sections_of(body)]
    return paper_record_view(prof, sizes, record, view=view, sections=sections)


@public_router.get("/profiles/{slug}/summaries", response_model=SummaryBatch)
def get_summaries(
    slug: str,
    request: Request,
    response: Response,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
    ids: str = Query(..., description="Comma-separated paper ids, at most 20."),
) -> SummaryBatch:
    """Several generated paper summaries in one read, each gated on its own tier.

    At most 20 ids (more are listed in ``not_processed``). A summary this
    caller may not read, or that does not exist, is in ``unavailable`` with its
    reason (``not_permitted``, ``none``, ``not_uploaded``), the same answer the
    rows' ``summary`` size gives.
    """
    prof = get_profile(slug, store)
    _gate_profile(get_service(request), get_caller(request), prof, viewer, slug)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = VARY_ON_CREDENTIALS
    wanted = _split_ids(ids)
    taken, rest = wanted[:BATCH_IDS_CAP], wanted[BATCH_IDS_CAP:]
    sizes = SizeIndex(prof, viewer, store, store.resolve_slug(slug))
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


def _unknown_section(e: ValueError) -> HTTPException:
    valid = e.args[1] if len(e.args) > 1 else []
    return HTTPException(
        status_code=400,
        detail={
            "error": "unknown_section",
            "message": "that section does not exist in this text",
            "valid": valid,
        },
    )


@public_router.get(
    "/profiles/{slug}/papers/{paper_id}/text",
    response_model=TextPage,
    response_model_exclude_none=True,
)
def get_paper_text(
    slug: str,
    paper_id: str,
    request: Request,
    response: Response,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
    section: Optional[str] = None,
    offset: int = 0,
    max_chars: Optional[int] = None,
) -> TextPage:
    """A paper's full text, bounded: whole up to ~80K chars, else paged.

    Gated exactly like ``GET /profiles/{slug}/content/sources/papers/{id}.md``:
    a full text this caller may not read is the same 404 as one that does not
    exist. Above 80K chars the page is the first chunk, cut at a paragraph,
    with ``has_more`` and ``next_offset``. ``section=`` reads one heading's
    span (names are in ``sections``). Offsets are indices into the whole text.
    """
    prof = get_profile(slug, store)
    _gate_profile(get_service(request), get_caller(request), prof, viewer, slug)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = VARY_ON_CREDENTIALS
    url = text_url(paper_id)
    missing = HTTPException(
        status_code=404, detail=f"no full text {paper_id!r} for profile {slug!r}"
    )
    effective = effective_tiers(prof.metadata)
    if url not in effective or not tier_allows(viewer, effective[url]):
        raise missing
    body = _text_of(store, slug, url)
    try:
        return page_text(body, section=section, offset=offset, max_chars=max_chars)
    except ValueError as e:
        raise _unknown_section(e) from e


#: The narrative's two parts, in reading order: ``(section name, contentUrl, role)``.
_NARRATIVE = (("soul", SOUL_URL, "soul"), ("expertise", EXPERTISE_URL, "expertise"))


@public_router.get(
    "/profiles/{slug}/text",
    response_model=TextPage,
    response_model_exclude_none=True,
)
def get_profile_text(
    slug: str,
    request: Request,
    response: Response,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
    section: Optional[str] = None,
    offset: int = 0,
    max_chars: Optional[int] = None,
) -> TextPage:
    """The profile's narrative, bounded: ``soul`` (SOUL.md) and ``expertise``.

    With no section, both parts this caller may read, joined as ``# Soul`` and
    ``# Expertise`` sections. With ``section=soul`` or ``section=expertise``,
    that artifact's stored text alone, so an offset from a passage is an index
    into it. Each part is gated like its content route. The response carries
    ``content_hash``, so an edit to the narrative needs no second read.
    """
    prof = get_profile(slug, store)
    _gate_profile(get_service(request), get_caller(request), prof, viewer, slug)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = VARY_ON_CREDENTIALS
    explain = explain_tiers(prof.metadata)
    md = prof.metadata
    bodies = {
        name: (prof.soul if name == "soul" else prof.expertise)
        for name, url, role in _NARRATIVE
        if artifact_visible(explain, md, url, role, viewer)
    }
    names = [name for name, _url, _role in _NARRATIVE]
    if section is not None and section.lower() not in names:
        raise _unknown_section(ValueError("unknown section", names))
    if section is not None:
        section = section.lower()
        if section not in bodies:
            raise HTTPException(status_code=404, detail=f"no {section} text for profile {slug!r}")
        page = page_text(bodies[section] or "", section=None, offset=offset, max_chars=max_chars)
        page.section = section
        page.sections = [TextSection(name=section, offset=0, chars=len(bodies[section] or ""))]
    else:
        if not bodies:
            raise HTTPException(status_code=404, detail=f"no narrative for profile {slug!r}")
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
    page.content_hash = _content_hash(store, store.resolve_slug(slug))
    return page
