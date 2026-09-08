"""Authoritative person -> rid resolution, gated by the ``resolve`` consumer scope.

``POST /identity/resolve`` is the identity function: a rid or name goes in, a
rid comes out, and a true miss creates the identity (a ``lite``,
``internal``, ``third_party`` stub). It is a write route and is gated like
one; putting it behind ``match`` would let every read-tier key mint people.
See ``researcher_profiles.resolve`` for the pipeline.
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ...models.api import ResolveCandidate, ResolveRequest, ResolveResponse
from ...resolve import ResolveError, resolve_person
from ...store import ProfileStore
from .._projection import invalidate_after_write
from ..deps import get_store, require_scope
from ._routers import router


@router.post(
    "/identity/resolve",
    response_model=ResolveResponse,
    dependencies=[Depends(require_scope("resolve"))],
)
def resolve_identity(
    body: ResolveRequest,
    request: Request,
    store: ProfileStore = Depends(get_store),
) -> JSONResponse:
    """Resolve a person descriptor to a ``rid``, minting a stub on a true miss.

    A sibling of the read-only ``GET /profiles/{slug}`` surface: that answers
    "what profile is this rid", 404 on a miss, and never writes; this is the
    identity function, and a true miss creates the identity. Every consumer
    service that turns its own person data (a free-text name, a bare ORCID)
    into a canonical ``rid`` calls this rather than hand-rolling matching or
    minting its own local id. See ``resolve_person`` for the pipeline.
    """
    try:
        result = resolve_person(
            store,
            rid=body.rid,
            name=body.name,
            affiliation=body.affiliation,
            create_new=body.create_new,
        )
    except ResolveError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if result.created:
        invalidate_after_write(request, store, store.resolve_slug(result.rid))
    payload = ResolveResponse(
        rid=result.rid,
        created=result.created,
        confidence=result.confidence,
        candidates=[
            ResolveCandidate(rid=c.rid, name=c.name, affiliation=c.affiliation)
            for c in result.candidates
        ],
    )
    # ``rid`` is significant even when null (it is what "deferred" looks
    # like), so only ``candidates`` is dropped when empty. A blind
    # ``exclude_none`` would also swallow a deferred response's ``rid``.
    # Hosts that consume this route depend on that exact wire shape.
    exclude = {"candidates"} if not result.candidates else set()
    return JSONResponse(
        payload.model_dump(exclude=exclude),
        status_code=201 if result.created else 200,
    )
