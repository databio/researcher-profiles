"""The public read surface: the profile listing and one profile's artifacts.

Every route here hangs on ``public_router`` and resolves a viewer tier, then
projects its response against that tier. An anonymous request is not a
different code path; it is the viewer whose tier is ``public``.
"""

import hashlib
import logging
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, Response

from ... import __version__
from ...models.api import (
    PaperEntry,
    PaperSummary,
    ProfileDetail,
    ProfileListEntry,
    ProfileListResponse,
    ProfileSummary,
)
from ...models.published import ProfileCard, ProfileCollection
from ...privacy import (
    ALWAYS_RESTRICTED_PREFIXES,
    ViewerTier,
    effective_tiers,
    explain_tiers,
    profile_visible,
    tier_allows,
)
from ...schema import (
    ALWAYS_RESTRICTED_ROLES,
    validate_ref,
)
from ...schema.jsonld import CONTEXT_URL
from ...schema.manifest import _SUMMARY_SUFFIX
from ...store import ProfileNotFoundError, ProfileStore
from .._projection import (
    PUBLIC_DOCUMENT_MAX_AGE,
    VARY_ON_CREDENTIALS,
    _content_hash,
    _gate_profile,
    _http_date,
    _profile_missing,
    _profile_summary,
    artifact_visible,
    metadata_payload,
    withheld,
)
from ..deps import (
    get_profile,
    get_profile_tier_floor,
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
            out.append(_profile_summary(prof))
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
    response_model=ProfileDetail,
)
def get_profile_detail(
    slug: str,
    request: Request,
    response: Response,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> ProfileDetail:
    """One profile, projected artifact by artifact through the viewer's tier.

    The profile gate runs first (404 when this viewer may not see it at all),
    then each body is served only if its backing artifact is visible: ``soul``
    from ``personality/SOUL.md``, ``expertise`` from
    ``personality/expertise.md``. A withheld body is ``None``, never ``""``: a
    client has to be able to tell "withheld" from "empty", and an owner looking
    at an empty box would reasonably conclude publication had worked.

    The manifest itself stays whole (spec section 6, tier-invariant), with each
    entry carrying its ``effective_visibility`` so no client ever recomputes the
    derivation rule, and ``withheld`` naming what this viewer did not get.
    """
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = VARY_ON_CREDENTIALS
    explain = explain_tiers(prof.metadata)
    md = prof.metadata

    manifest = []
    for part in prof.manifest():
        entry = part.model_dump(mode="json")
        detail = explain.get(part.content_url)
        entry["effective_visibility"] = detail.effective if detail is not None else part.visibility
        manifest.append(entry)

    return ProfileDetail(
        slug=prof.slug,
        rid=getattr(prof, "rid", None),
        metadata=metadata_payload(prof),
        expertise=(
            prof.expertise
            if artifact_visible(explain, md, "personality/expertise.md", "expertise", viewer)
            else None
        ),
        soul=(
            prof.soul
            if artifact_visible(explain, md, "personality/SOUL.md", "soul", viewer)
            else None
        ),
        manifest=manifest,
        withheld=withheld(explain, viewer),
        content_hash=_content_hash(store, prof.slug),
    )


@public_router.get(
    "/profiles/{slug}/profile.jsonld",
)
def get_profile_jsonld(
    slug: str,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> Response:
    """Serve the stored ``profile.jsonld`` **verbatim**.

    These are the bytes the store persisted, not a re-serialization from the
    loaded model: what a crawler or an agent fetches here has to be the
    published document, byte for byte, or the ``conformsTo`` claim is about a
    file nobody can retrieve. ``store.document_bytes`` is that guarantee on
    every backend.
    """
    try:
        validate_ref(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # The profile-level tier gates the document; what is inside it is served
    # verbatim (spec section 6/7). The bytes are the published record or the
    # ``conformsTo`` claim is about a file nobody can retrieve.
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    try:
        data = store.document_bytes(slug)
    except (ProfileNotFoundError, KeyError) as e:
        raise _profile_missing(slug) from e
    # A strong etag over the served bytes, so the short revalidation above is a
    # 304 rather than a re-send. It is the document itself, not a timestamp: two
    # replicas serving the same profile agree on it.
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
    An owner reads their own ``restricted`` CV because a management host's
    resolver gives them the ``restricted`` viewer tier, not because this path
    is reserved for them.

    Three refusals, in order:

    * the profile gate: 404, byte-identical to a nonexistent slug;
    * the hard floors of ``docs/rp-spec/privacy.md``: **403**, and
      not a 404: these are withheld from *everyone*, the owner
      included, so there is no viewer for whom their existence is a secret and
      nothing is disclosed by saying why.

      - ``paper_fulltext`` (``sources/papers/``): extracted full text of
        copyrighted papers. A legal floor, not a preference; serving it is the
        redistribution the whole tier exists to prevent.
      - ``.cache/`` and ``.keys/``: build-local derived state (the sqlite
        embedding index) and key material, not servable profile artifacts.

    * the artifact gate: 404, byte-identical to an artifact that is not in the
      manifest at all. "Not for you" and "not there" must not be tellable apart,
      or the error message enumerates the private half of the profile.

    Only paths the manifest declares are servable; an unknown or traversing path
    is a 404. ``X-RP-Effective-Tier`` reports the tier the derivation rule
    resolved (``privacy.effective_tiers``), so a derivative never advertises a
    looser tier than its sources.
    """
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    resolved = store.resolve_slug(slug)

    # ``profile.jsonld`` is the manifest, not an entry in it, so it is resolved
    # here rather than through the manifest lookup below. This is what makes
    # ``/content/`` a usable base URL: a client points at one directory and
    # follows relative contentUrls out of the document it finds there, exactly
    # as it does against a static site. Gated by the profile tier only: the
    # bytes are served verbatim (spec section 6/7).
    if artifact == "profile.jsonld":
        try:
            data = store.document_bytes(resolved)
        except (ProfileNotFoundError, KeyError) as e:
            raise _profile_missing(slug) from e
        headers = {"Cache-Control": "private, no-store", "Vary": VARY_ON_CREDENTIALS}
        lm = _http_date(prof.metadata.date_modified)
        if lm is not None:
            headers["Last-Modified"] = lm
        return Response(
            content=data,
            media_type="application/ld+json",
            headers=headers,
        )

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
    if part.role in ALWAYS_RESTRICTED_ROLES:
        raise HTTPException(
            status_code=403,
            detail="paper full text is a restricted hard floor and is never served",
        )
    if any(artifact.startswith(prefix) for prefix in ALWAYS_RESTRICTED_PREFIXES):
        raise HTTPException(
            status_code=403,
            detail="build-local derived state is not a servable artifact",
        )

    if not tier_allows(viewer, effective[artifact]):
        raise unknown

    # The store owns retrieval, including the traversal refusal a directory
    # backend needs and a relational one structurally cannot need.
    try:
        data = store.artifact_bytes(resolved, artifact)
    except (ProfileNotFoundError, KeyError) as e:
        raise unknown from e

    return Response(
        content=data,
        media_type=part.encoding_format or "application/octet-stream",
        headers={
            "X-RP-Effective-Tier": effective[artifact],
            "Cache-Control": "private, no-store",
            "Vary": VARY_ON_CREDENTIALS,
        },
    )


@public_router.get(
    "/profiles/{slug}/papers",
    response_model=list[PaperEntry],
)
def list_papers(
    slug: str,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> list[PaperEntry]:
    """The works list, gated on the tier of ``sources/papers.jsonld``.

    ``summary_available`` reports what this viewer may fetch, not what exists on
    disk. Advertising a summary the caller cannot retrieve would send them
    straight into a 404 and teach them the profile is broken rather than
    private.
    """
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    explain = explain_tiers(prof.metadata)
    md = prof.metadata
    if not artifact_visible(explain, md, "sources/papers.jsonld", "works", viewer):
        raise _profile_missing(slug)
    summaries = prof.summaries
    out: list[PaperEntry] = []
    for p in prof.papers:
        pid = p.paper_id
        has_summary = bool(pid and pid in summaries) and artifact_visible(
            explain, md, f"sources/summaries/{pid}{_SUMMARY_SUFFIX}", "paper_summary", viewer
        )
        out.append(
            PaperEntry(
                paper_id=pid,
                title=p.title,
                year=p.year,
                journal=p.journal,
                first_author=p.first_author,
                authors=p.authors,
                doi=p.doi,
                pmid=p.pmid,
                openalex_id=p.openalex_id,
                full_text_link=p.full_text_link,
                summary_available=has_summary,
            )
        )
    return out


@public_router.get(
    "/profiles/{slug}/summary/{paper_id}",
    response_model=PaperSummary,
)
def get_paper_summary(
    slug: str,
    paper_id: str,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> PaperSummary:
    """One paper summary, gated on that summary artifact's effective tier.

    A withheld summary 404s with the same body as a summary that was never
    written: "you may not have this" and "this does not exist" must be
    indistinguishable, or the error message is an existence oracle.
    """
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    summaries = prof.summaries
    missing = HTTPException(
        status_code=404,
        detail=f"summary {paper_id!r} not found for profile {slug!r}",
    )
    if paper_id not in summaries:
        raise missing
    if not artifact_visible(
        explain_tiers(prof.metadata),
        prof.metadata,
        f"sources/summaries/{paper_id}{_SUMMARY_SUFFIX}",
        "paper_summary",
        viewer,
    ):
        raise missing
    return PaperSummary(paper_id=paper_id, summary=summaries[paper_id])
