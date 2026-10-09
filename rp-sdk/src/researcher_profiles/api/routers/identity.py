"""Person -> rid resolution, gated by the ``resolve`` consumer scope.

A true miss mints a stub, so this is a write route; behind ``match`` every
read-tier key could mint people.
"""

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ...models.api import ResolveCandidate, ResolveRequest, ResolveResponse
from ...resolve import ResolveError, resolve_person
from ...store import ProfileStore
from ..deps import (
    get_service,
    get_store,
    require_scope,
)
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

    The stub is ``lite``, ``limited`` and ``third_party``. See ``resolve_person``
    for the pipeline.
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
        get_service(request).invalidate(store.resolve_slug(result.rid))
    payload = ResolveResponse(
        rid=result.rid,
        created=result.created,
        confidence=result.confidence,
        candidates=[
            ResolveCandidate(rid=c.rid, name=c.name, affiliation=c.affiliation)
            for c in result.candidates
        ],
    )
    # A null ``rid`` means "deferred" and must stay on the wire, so only
    # ``candidates`` is dropped when empty.
    exclude = {"candidates"} if not result.candidates else set()
    return JSONResponse(
        payload.model_dump(exclude=exclude),
        status_code=201 if result.created else 200,
    )
