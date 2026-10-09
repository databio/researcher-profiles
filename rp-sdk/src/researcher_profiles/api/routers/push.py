"""Whole-profile upload (``push`` scope) and tier-filtered archive download."""

import hashlib
import json
import logging
import tempfile
from pathlib import Path

from fastapi import Depends, HTTPException, Request, Response
from pydantic import ValidationError

from ...errors import Invalid, NotFound, ProfileWriteError
from ...models.api import (
    CapabilitiesResponse,
    PushResponse,
)
from ...schema import (
    ProfileDocument,
    mint_local_rid,
    validate_ref,
)
from ...store import ProfileStore
from ..caller import Caller
from ..deps import get_read_caller, get_service, get_store, require_scope
from ..service import Service
from ..upload import (
    DEFAULT_MAX_UPLOAD_BYTES,
    PUSH_MODES,
    SLUG_RE,
    UploadError,
    build_viewer_archive,
    ingest_archive,
)
from ._routers import public_router, router

logger = logging.getLogger(__name__)


#: Push behaviours advertised at ``GET /api/v1/capabilities``. ``manifest_splice``:
#: a kept file keeps its manifest entry, so a manifest that drops entries cannot
#: delete artifacts the server holds.
PUSH_FEATURES = ["manifest_splice"]


@public_router.get("/capabilities", response_model=CapabilitiesResponse)
def capabilities() -> CapabilitiesResponse:
    """What this server accepts, for a client to check before it writes.

    Unauthenticated on purpose: it says nothing about any profile. A 404 means a
    server without this route.
    """
    return CapabilitiesResponse(
        version="v1",
        push_modes=list(PUSH_MODES),
        features=list(PUSH_FEATURES),
    )


@router.put(
    "/profiles/{slug}",
    response_model=PushResponse,
    dependencies=[Depends(require_scope("push"))],
)
async def put_profile(
    slug: str,
    request: Request,
    store: ProfileStore = Depends(get_store),
) -> PushResponse:
    """Upload (create or replace) a profile.

    * ``application/json``: a ``profile.jsonld`` body, giving a document-only
      profile.
    * any other content type: a gzipped tarball of the profile directory with
      ``profile.jsonld`` at the root.

    The pushed profile is visible immediately.
    """
    if not SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail=f"invalid slug {slug!r} (expected ^[a-z0-9][a-z0-9-]*$)",
        )

    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype == "application/json":
        return await _put_profile_json(slug, request, store)
    return await _put_profile_tarball(slug, request, store)


def _push_gate(request: Request, slug: str, rid: str) -> None:
    """Ask the host whether this credential may write ``rid`` (see app.state.push_gate)."""
    gate = getattr(request.app.state, "push_gate", None)
    if gate is not None:
        gate(request, slug, rid)


async def _put_profile_json(
    slug: str,
    request: Request,
    store: ProfileStore,
) -> PushResponse:
    """JSON upsert. Mints a ``local:`` rid on ``mintLocalRid=true`` or ``?mint=local``.

    Supports optimistic concurrency via ``If-Match: <content_hash>``.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}") from e

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    mint_query = request.query_params.get("mint", "").lower()
    mint_body = body.pop("mintLocalRid", False)
    should_mint = mint_body is True or mint_query == "local"

    if "rid" not in body or not body.get("rid"):
        if should_mint:
            name = body.get("name", "").strip()
            if not name:
                raise HTTPException(
                    status_code=400,
                    detail="mintLocalRid requires a non-empty name",
                )
            body["rid"] = mint_local_rid(name)
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    "missing rid: supply an ORCID rid in the document, or pass "
                    "mintLocalRid=true (or ?mint=local) to mint a local: identity"
                ),
            )
    elif should_mint:
        raise HTTPException(
            status_code=400,
            detail="cannot mint: document already carries a rid",
        )

    try:
        document = ProfileDocument.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    _push_gate(request, slug, document.rid)

    if_match = request.headers.get("if-match")
    if if_match:
        if store.exists(slug):
            current_hash = store.content_hash(slug)
            if if_match != current_hash:
                raise HTTPException(
                    status_code=409,
                    detail="content hash mismatch (concurrent edit)",
                    headers={"X-RP-Content-Hash": current_hash},
                )

    try:
        prof = store.put_document(slug, document)
    except ProfileWriteError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    get_service(request).invalidate(slug)

    logger.info("profile %s JSON upsert", slug)
    return PushResponse(
        slug=slug,
        rid=prof.rid,
        name=prof.metadata.name,
        level=str(prof.level),
        indexed=False,  # JSON upsert never carries an embedding index
    )


async def _put_profile_tarball(
    slug: str,
    request: Request,
    store: ProfileStore,
) -> PushResponse:
    """Tarball upload of a profile directory."""
    max_bytes = getattr(request.app.state, "max_upload_bytes", DEFAULT_MAX_UPLOAD_BYTES)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(status_code=413, detail="archive exceeds size cap")
    data = await request.body()
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail="archive exceeds size cap")
    if not data:
        raise HTTPException(status_code=400, detail="empty request body")

    # ``?mode=``: what happens to the live files the archive does not carry.
    mode = request.query_params.get("mode", "replace")
    if mode not in PUSH_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid mode {mode!r} (expected one of {', '.join(PUSH_MODES)})",
        )
    try:
        result = ingest_archive(
            store,
            slug,
            data,
            include_fulltext=bool(getattr(request.app.state, "accept_fulltext", False)),
            gate=lambda rid: _push_gate(request, slug, rid),
            mode=mode,
        )
    except UploadError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ProfileWriteError as e:
        # Includes RetiredRidError: an archive whose rid or slug a merge retired.
        raise HTTPException(status_code=409, detail=str(e)) from e

    # Explicit: extracted mtimes can predate centroids.npz, so mtime checks miss it.
    get_service(request).invalidate(slug)

    logger.info("profile %s pushed (%d bytes)", slug, len(data))
    return PushResponse(
        slug=slug,
        rid=result.rid,
        name=result.name,
        level=result.level,
        indexed=result.indexed,
        kept=result.kept,
        spliced=result.spliced,
        manifest_counts=result.manifest_counts,
        mode=result.mode,
    )


# Gated on `read`, not `push`: nothing here writes, and the viewer tier decides
# what a caller gets.
@router.get(
    "/profiles/{slug}/archive",
    dependencies=[Depends(require_scope("read"))],
)
def get_profile_archive(
    slug: str,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> Response:
    """Download a profile as a gzipped tarball: the inverse of ``PUT``.

    Holds exactly what the read surface would serve this caller, never the
    hard floors (``.cache/``, ``.keys/``), and no registry-issued proofs.
    ``X-RP-Archive-Digest`` is the body's md5; ``X-RP-Archive-Tier`` names the
    tier it was built for.
    """
    # A read path accepts either form. Never gate a rid on SLUG_RE: it
    # forbids uppercase and would reject every X-suffixed ORCID.
    try:
        validate_ref(slug)
    except ValueError as e:
        raise Invalid(str(e)) from e
    # The path below uses the resolved slug, never the caller's reference.
    store, prof, viewer = service.read(caller, slug)
    slug = store.resolve_slug(slug)
    root = store.root
    try:
        if root is not None:
            data = build_viewer_archive(root / slug, viewer=viewer)
        else:
            # No directory store: stage one in scratch space.
            with tempfile.TemporaryDirectory(prefix="rp-archive-") as tmp:
                staged = store.export_directory(slug, Path(tmp) / slug)
                data = build_viewer_archive(staged, viewer=viewer)
    except FileNotFoundError as e:
        raise NotFound(str(e)) from e
    # Transfer-integrity digest only, not a security control; TLS and signing cover tampering.
    digest = hashlib.md5(data).hexdigest()
    return Response(
        content=data,
        media_type="application/gzip",
        headers={
            "X-RP-Archive-Digest": digest,
            "X-RP-Profile-Level": str(prof.level),
            "X-RP-Archive-Tier": viewer,
            "Content-Disposition": f'attachment; filename="{slug}.tar.gz"',
        },
    )
