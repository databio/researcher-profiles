"""The public read surface: the profile listing and one profile's artifacts.

Every response is projected through the viewer tier; anonymous is the
``public`` tier, not a separate code path.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import Depends, Query, Request, Response

from ... import __version__
from ...errors import Invalid
from ...models.api import (
    FileList,
    PaperPage,
    PaperRecordView,
    ProfileListEntry,
    ProfileRecord,
    SummaryBatch,
    TextPage,
)
from ...models.published import ProfileCard, ProfileCollection
from ...privacy import ViewerTier
from ...schema import validate_ref, validate_rid
from ...schema.jsonld import CONTEXT_URL
from .. import service as svc
from .._projection import PUBLIC_DOCUMENT_MAX_AGE, VARY_ON_CREDENTIALS, _http_date
from ..caller import Caller
from ..deps import get_read_caller, get_service
from ..paging import (
    PAGE_PARAMS,
    TOKEN_PARAMS,
    PageRequest,
    page_params,
    paginate,
    reject_unknown_params,
)
from ..service import Service
from ._routers import public_router

logger = logging.getLogger(__name__)


def _cache_headers(
    viewer: ViewerTier,
    *,
    etag: str | None = None,
    last_modified_iso: str | None = None,
) -> dict[str, str]:
    """Cache directives: only the anonymous rendering is shareable, and only briefly."""
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


#: Query parameters every read route takes through ``get_read_caller``.
READ_CALLER_PARAMS = frozenset({"as"})
#: ``GET /profiles``: (default, max) rows per page. Rows are slim.
PROFILES_PAGE = (100, 1000)
#: ``GET /profiles/{slug}/seed-papers``: (default, max) rows per page.
SEED_PAPERS_PAGE = (100, 500)


def _rid_filter(values: list[str]) -> Optional[list[str]]:
    """The ``rid`` filter values, canonical; a malformed one is a 400."""
    if not values:
        return None
    out = []
    for raw in values:
        try:
            out.append(validate_rid(raw.strip()))
        except ValueError as e:
            raise Invalid(str(e), code="invalid_argument") from None
    return out


@public_router.get("/profiles")
def list_profiles(
    request: Request,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
    paging: PageRequest = Depends(page_params(*PROFILES_PAGE)),
    rid: list[str] = Query([]),
    clinical: Optional[bool] = None,
    affiliation: Optional[str] = None,
) -> Response:
    """The profiles this caller may see, in slug order, page-based.

    Returns ``{results: [ProfileListEntry], pagination, note}``. Filters are
    exact and AND together: ``rid`` (repeatable; any of these identities),
    ``clinical`` and ``affiliation`` (trimmed, ignoring case). ``total``
    counts the matches this caller may see. An unknown query parameter is a
    400. To read one profile by ORCID, ``GET /profiles/{orcid}``. Never
    shared-cached, since membership depends on who asked.
    """
    reject_unknown_params(
        request, READ_CALLER_PARAMS | PAGE_PARAMS | {"rid", "clinical", "affiliation"}
    )
    summaries = svc.list_profiles(
        service,
        caller,
        rids=_rid_filter(rid),
        clinical=clinical,
        affiliation=affiliation,
    )
    origin = _public_origin(request)
    page = paginate(
        [
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
                clinical=s.clinical,
            )
            for s in summaries
        ],
        paging,
    )
    return Response(
        content=page.model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "private, no-store", "Vary": VARY_ON_CREDENTIALS},
    )


def _public_origin(request: Request) -> str:
    """The scheme + host a client reached this server on, no trailing slash.

    Forwarded headers win over ``request.base_url``, which is ``http://``
    behind a TLS-terminating proxy and blocked as mixed content.
    """
    base = request.base_url
    scheme = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    return f"{scheme or base.scheme}://{host or base.netloc}"


@public_router.get("/collection.json")
def get_collection(
    request: Request,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> Response:
    """The visible profiles as a collection bundle document, for a browser home view.

    Unlike a static ``collection.jsonld`` it carries no centroids or
    ``artifacts``: their source index is a server-only floor, so clients rank
    with ``/match``. Each card's ``base`` is the absolute ``content/`` URL,
    since a client resolves it with a bare URL parse. Never shared-cached.
    """
    summaries = svc.list_profiles(service, caller)
    origin = _public_origin(request)
    bundle = ProfileCollection(
        **{
            "@context": CONTEXT_URL,
            "@id": str(request.url.replace(query="")),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "generator": f"researcher-profiles/{__version__}",
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
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
    view: Literal["record", "full"] = "record",
) -> Response:
    """One profile, sized for this caller.

    ``view="record"`` (default) trims long lists, adds ``content_hash`` and the
    readable ``parts`` with sizes, and stays under 8 KB. ``view="full"`` adds
    every field plus the ``soul`` and ``expertise`` bodies. The file manifest
    is ``GET /profiles/{slug}/files``.

    A withheld field is ``null`` and named in ``withheld``, never ``""``. The
    weak ``ETag`` covers ``content_hash``, tier and view.
    """
    record = svc.get_profile(service, caller, slug, view=view)
    headers = {"Cache-Control": "private, no-store", "Vary": VARY_ON_CREDENTIALS}
    current = record.content_hash
    if current:
        viewer = request.state.viewer_tier
        etag = f'W/"{current.split(":", 1)[-1][:32]}-{viewer}-{view}"'
        headers["ETag"] = etag
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
    return Response(
        content=record.model_dump_json(exclude_none=True),
        media_type="application/json",
        headers=headers,
    )


@public_router.get("/profiles/{slug}/files", response_model=FileList)
def get_profile_files(
    slug: str,
    response: Response,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> FileList:
    """The profile's manifest, each entry labeled with its effective tier.

    Served whole at every tier (spec section 6), with each entry's ``slot`` and
    ``effective_visibility``, and ``withheld`` naming what this viewer may not
    read.
    """
    files = svc.list_files(service, caller, slug)
    _no_store(response)
    return files


@public_router.get(
    "/profiles/{slug}/profile.jsonld",
)
def get_profile_jsonld(
    slug: str,
    request: Request,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> Response:
    """Serve the profile's ``profile.jsonld``: the stored record plus registry proofs.

    Registry proofs (``orcid_login``) are computed per request, never stored.
    With nothing to project, the stored bytes are served as written, so
    ``conformsTo`` refers to a file anyone can retrieve byte for byte.
    """
    try:
        validate_ref(slug)
    except ValueError as e:
        raise Invalid(str(e)) from e
    data, prof, viewer = svc.get_document(service, caller, slug)
    return _document_response(request, data, prof, viewer)


def _document_response(request: Request, data: bytes, prof, viewer: ViewerTier) -> Response:
    """The one ``profile.jsonld`` response, shared by both document URLs."""
    # Strong etag over the served bytes, so replicas agree and a proof change shows.
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
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> Response:
    """Serve one manifest artifact, projected through the caller's viewer tier.

    ``content/`` is the base URL every relative ``contentUrl`` resolves against.

    Refusals, in order:

    * the profile gate: 404, identical to a nonexistent slug;
    * the hard floors (``.cache/``, ``.keys/``; ``docs/rp-spec/privacy.md``):
      403, since they are withheld from everyone and their existence is no
      secret;
    * the artifact gate: 404, identical to an artifact not in the manifest, so
      errors cannot enumerate the private half of a profile.

    A permitted entry whose body the store lacks is a 404 that says so. Only
    paths the manifest declares are servable. ``X-RP-Effective-Tier`` reports
    the derived tier.
    """
    data, media_type, prof, viewer, tier = svc.get_artifact(service, caller, slug, artifact)
    if tier is None:  # profile.jsonld: the served document
        return _document_response(request, data, prof, viewer)
    return Response(
        content=data,
        media_type=media_type or "application/octet-stream",
        headers={
            "X-RP-Effective-Tier": tier,
            "Cache-Control": "private, no-store",
            "Vary": VARY_ON_CREDENTIALS,
        },
    )


@public_router.get("/profiles/{slug}/seed-papers")
def list_seed_papers(
    slug: str,
    request: Request,
    response: Response,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
    paging: PageRequest = Depends(page_params(*SEED_PAPERS_PAGE)),
) -> dict:
    """The interest seed papers: papers that define the person's interests.

    Never works they wrote. Gated on the tier of
    ``sources/seed_papers.jsonld``, ``private`` by default: a caller below it
    gets the profile's 404. Page-based ``{results, pagination}``, in
    ``seed_id`` order.
    """
    reject_unknown_params(request, READ_CALLER_PARAMS | PAGE_PARAMS)
    rows = svc.list_seed_papers(service, caller, slug)
    rows.sort(key=lambda r: str(r.get("seed_id") or ""))
    _no_store(response)
    return paginate(rows, paging).model_dump(mode="json")


@public_router.get(
    "/profiles/{slug}/papers",
    response_model=PaperPage,
    response_model_exclude_none=True,
)
def list_papers(
    slug: str,
    request: Request,
    response: Response,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
    page_size: Optional[int] = None,
    page_token: Optional[str] = None,
    q: Optional[str] = None,
    year_min: Optional[int] = None,
    missing_ids: bool = False,
    has_text: Optional[bool] = None,
    ids: Optional[str] = None,
) -> PaperPage:
    """The works list as rich rows, token-paged, gated on the tier of ``sources/papers.jsonld``.

    Returns ``{results, pagination, filters_applied, search_mode_used, note}``.
    Each row carries identity, a short summary, the ``summary`` and ``text``
    sizes this caller may fetch (``{available, bytes, approx_tokens, reason}``),
    and the paper's ``version`` for an edit.

    Without ``q``, rows are newest first (``year`` desc, then ``paper_id``);
    pass ``pagination.next_page_token`` back as ``page_token`` until it is
    ``null``. With ``q``, rows are ranked by hybrid search (meaning, over the
    stored summary and abstract vectors, plus BM25 keywords over title,
    summary, abstract and journal, merged by reciprocal rank) and returned as
    one top-N page with no ``total`` and no next token; ``search_mode_used``
    and ``note`` say what ran. ``year_min``, ``missing_ids`` (no DOI and no
    OpenAlex id) and ``has_text`` filter before ranking. ``page_size``
    defaults to 20 and is clamped to 100 (``pagination.page_size``). A token
    is bound to the filters it was made under; reusing it with others is a
    400 ``page_token_mismatch``.

    ``ids`` (a comma list, at most 20) reads those rows only, in that order,
    as one page.
    """
    reject_unknown_params(
        request,
        READ_CALLER_PARAMS | TOKEN_PARAMS | {"q", "year_min", "missing_ids", "has_text", "ids"},
    )
    page = svc.list_papers(
        service,
        caller,
        slug,
        q=q,
        year_min=year_min,
        missing_ids=missing_ids,
        has_text=has_text,
        page_size=page_size,
        page_token=page_token,
        ids=ids,
    )
    _no_store(response)
    return page


@public_router.get(
    "/profiles/{slug}/papers/{paper_id}",
    response_model=PaperRecordView,
    response_model_exclude_none=True,
)
def get_paper(
    slug: str,
    paper_id: str,
    response: Response,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
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
    record = svc.get_paper(service, caller, slug, paper_id, view=view)
    _no_store(response)
    return record


@public_router.get("/profiles/{slug}/summaries", response_model=SummaryBatch)
def get_summaries(
    slug: str,
    response: Response,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
    ids: str = Query(..., description="Comma-separated paper ids, at most 20."),
) -> SummaryBatch:
    """Several generated paper summaries in one read, each gated on its own tier.

    At most 20 ids (more are listed in ``not_processed``). A summary this
    caller may not read, or that does not exist, is in ``unavailable`` with its
    reason (``not_permitted``, ``none``, ``not_uploaded``), the same answer the
    rows' ``summary`` size gives.
    """
    batch = svc.get_summaries(service, caller, slug, ids)
    _no_store(response)
    return batch


@public_router.get(
    "/profiles/{slug}/papers/{paper_id}/text",
    response_model=TextPage,
    response_model_exclude_none=True,
)
def get_paper_text(
    slug: str,
    paper_id: str,
    response: Response,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
    section: Optional[str] = None,
    offset: int = 0,
    max_chars: Optional[int] = None,
) -> TextPage:
    """A paper's full text, bounded: whole up to ~80K chars, else paged.

    A text this caller may not read is the same 404 as a missing one. Above 80K chars the page is the first chunk, cut at a paragraph,
    with ``has_more`` and ``next_offset``. ``section=`` reads one heading's
    span (names are in ``sections``). Offsets are indices into the whole text.
    """
    page = svc.read_paper_text(
        service, caller, slug, paper_id, section=section, offset=offset, max_chars=max_chars
    )
    _no_store(response)
    return page


@public_router.get(
    "/profiles/{slug}/text",
    response_model=TextPage,
    response_model_exclude_none=True,
)
def get_profile_text(
    slug: str,
    response: Response,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
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
    page = svc.read_profile_text(
        service, caller, slug, section=section, offset=offset, max_chars=max_chars
    )
    _no_store(response)
    return page


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = VARY_ON_CREDENTIALS
