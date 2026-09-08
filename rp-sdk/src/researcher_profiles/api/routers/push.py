"""Whole-profile upload and download, gated by the ``push`` consumer scope.

``PUT /profiles/{slug}`` accepts either a JSON document or a tarball, and
``GET /profiles/{slug}/archive`` hands back a tier-filtered copy of the
directory.
"""

import hashlib
import json
import logging
import tempfile
from pathlib import Path

from fastapi import Depends, HTTPException, Request, Response
from pydantic import ValidationError

from ...errors import ProfileWriteError
from ...models.api import (
    PushResponse,
)
from ...privacy import (
    ViewerTier,
)
from ...schema import (
    ProfileDocument,
    mint_local_rid,
    validate_ref,
)
from ...store import ProfileStore
from .._projection import (
    _gate_profile,
    invalidate_after_write,
)
from ..deps import (
    get_profile,
    get_store,
    get_viewer_tier,
    require_scope,
)
from ..upload import (
    DEFAULT_MAX_UPLOAD_BYTES,
    SLUG_RE,
    UploadError,
    build_viewer_archive,
    ingest_archive,
)
from ._routers import router

logger = logging.getLogger(__name__)


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

    Accepts two content types on the same canonical URL:

    * ``application/json``: a JSON body conforming to ``profile.jsonld``.
      Creates a document-only profile (identity + expertise); enrichment
      (papers, embeddings) is added later by a push or a build. This is the
      backend write path for services that own identity.

    * tarball (any other content type): a gzipped tar archive of one
      profile directory's contents with ``profile.jsonld`` at the tar root.
      The archive is validated, staged, and committed into the store.

    Both paths invalidate the profile cache entry and the registry snapshot
    so the pushed profile is immediately visible to ``/profiles`` and
    ``/match`` (when it carries a built embedding index).
    """
    if not SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail=f"invalid slug {slug!r} (expected ^[a-z0-9][a-z0-9-]*$)",
        )

    # Dispatch on content type: JSON or tarball
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype == "application/json":
        return await _put_profile_json(slug, request, store)

    # Tarball path (the original behavior)
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
    """Handle JSON upsert of a profile document.

    Parses the body as a ProfileDocument, optionally mints a local:rid if
    ``mintLocalRid=true`` is passed in the body or ``?mint=local`` in the
    query, then calls ``store.put_document``.

    Supports optimistic concurrency via ``If-Match: <content_hash>``.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}") from e

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    # Handle local:rid minting
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

    # Parse and validate the document
    try:
        document = ProfileDocument.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    _push_gate(request, slug, document.rid)

    # Optimistic concurrency: If-Match header
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
        # If the profile doesn't exist, If-Match is irrelevant (create)

    try:
        prof = store.put_document(slug, document)
    except ProfileWriteError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    invalidate_after_write(request, store, slug)

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
    """Handle tarball upload of a profile directory.

    The original PUT path: a gzipped tar archive of the profile directory's
    contents with ``profile.jsonld`` at the tar root.
    """
    max_bytes = getattr(request.app.state, "max_upload_bytes", DEFAULT_MAX_UPLOAD_BYTES)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(status_code=413, detail="archive exceeds size cap")
    data = await request.body()
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail="archive exceeds size cap")
    if not data:
        raise HTTPException(status_code=400, detail="empty request body")

    try:
        result = ingest_archive(
            store,
            slug,
            data,
            include_fulltext=bool(getattr(request.app.state, "accept_fulltext", False)),
            gate=lambda rid: _push_gate(request, slug, rid),
        )
    except UploadError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # Invalidate: cached profile object, in-memory registry snapshot, and the
    # on-disk registry caches. Extraction restores archive mtimes, which can
    # predate centroids.npz, so the mtime check alone cannot be trusted to
    # notice a replaced profile.
    invalidate_after_write(request, store, slug)

    logger.info("profile %s pushed (%d bytes)", slug, len(data))
    return PushResponse(
        slug=slug,
        rid=result.rid,
        name=result.name,
        level=result.level,
        indexed=result.indexed,
    )


@router.get(
    "/profiles/{slug}/archive",
    dependencies=[Depends(require_scope("push"))],
)
def get_profile_archive(
    slug: str,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> Response:
    """Download a profile as a gzipped tarball: the inverse of ``PUT``.

    Projected through the caller's viewer tier by
    :func:`~researcher_profiles.api.upload.build_viewer_archive`, so the tarball
    contains exactly what the JSON read surface would serve the same caller: no
    artifact above their tier, and never the hard floors (copyrighted full
    text, ``.cache/``, ``.keys/``). The former ``serve_fulltext`` operator flag
    is gone: the full-text floor is the schema's job now, in one place.

    The response carries ``X-RP-Archive-Digest`` (md5 of the body) so the
    client can verify the transfer before committing it to its cache, and
    ``X-RP-Archive-Tier`` naming the tier it was built for.
    """
    # A read path accepts either form. Never gate a rid on SLUG_RE: it
    # forbids uppercase and would reject every X-suffixed ORCID.
    try:
        validate_ref(slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Resolve through the cache so a missing profile 404s consistently, and so
    # the path below is built from a real directory name rather than the
    # caller-supplied reference.
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    slug = store.resolve_slug(slug)
    root = store.root
    try:
        if root is not None:
            data = build_viewer_archive(root / slug, viewer=viewer)
        else:
            # No directory to tar. Materialize one in scratch space, archive it,
            # and drop it: the tarball is an INTERCHANGE format, so a store that
            # is not a directory still has to be able to emit one.
            with tempfile.TemporaryDirectory(prefix="rp-archive-") as tmp:
                staged = store.export_directory(slug, Path(tmp) / slug)
                data = build_viewer_archive(staged, viewer=viewer)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
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
