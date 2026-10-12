"""FastAPI dependencies: auth gates, the caller, the viewer tier, and the store."""

import logging
import os
from dataclasses import dataclass, field, replace
from typing import Optional

from fastapi import Depends, Header, HTTPException, Query, Request

from ..privacy import ViewerTier
from ..schema import Visibility
from ..store import ProfileStore
from .caller import Caller

logger = logging.getLogger(__name__)

#: Methods that never get a per-caller view: they write, and a view is read-only.
_WRITE_METHODS = frozenset({"PUT", "PATCH", "DELETE"})


def get_service(request: Request):
    """The app's :class:`~researcher_profiles.api.service.Service`."""
    return request.app.state.service


def get_caller(request: Request) -> Caller:
    """This request's :class:`Caller`, built by ``hooks.caller_resolver`` and memoized."""
    cached = getattr(request.state, "caller", None)
    if cached is not None:
        return cached
    caller = request.app.state.hooks.caller_resolver(request)
    request.state.caller = caller
    return caller


def _is_edit_route(request: Request) -> bool:
    from .routers._routers import edit_router

    route = request.scope.get("route")
    endpoint = getattr(route, "endpoint", None)
    return endpoint is not None and any(
        getattr(r, "endpoint", None) is endpoint for r in edit_router.routes
    )


def get_store(request: Request) -> ProfileStore:
    """The store this request reads: the caller's view on a read, the store on a write.

    Writes and every ``edit_router`` route get the store itself, since a view
    is read-only.
    """
    service = request.app.state.service
    if request.method in _WRITE_METHODS or _is_edit_route(request):
        return service.store
    return service.store_for(get_caller(request))


def require_token(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> None:
    """Bearer-token gate against ``app.state.token``; no token set means open mode."""
    expected = getattr(request.app.state, "token", None)
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(
            status_code=401,
            detail="invalid or missing bearer token",
        )


@dataclass(frozen=True)
class ConsumerIdentity:
    """The identity a ``consumer_verifier`` returns for a calling application.

    ``is_operator`` holds every scope; a real consumer key has
    ``is_operator=False`` and the scopes minted for it.
    """

    id: str
    name: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    is_operator: bool = False
    #: The widest tier this consumer may be shown, minted per key. Defaults to
    #: the narrowest, ``public``.
    tier: Visibility = "public"


def require_scope(scope: str):
    """Build a dependency enforcing that the caller may use capability ``scope``.

    A host installs ``app.state.consumer_verifier``, a callable
    ``(request, scope) -> ConsumerIdentity`` that raises ``HTTPException(401)``
    for a bad credential and ``HTTPException(403)`` for a consumer lacking
    ``scope``. With no verifier this is :func:`require_token`.
    """

    def dep(
        request: Request,
        authorization: Optional[str] = Header(None),
    ) -> None:
        verifier = getattr(request.app.state, "consumer_verifier", None)
        if verifier is None:
            require_token(request, authorization)
            return
        identity = verifier(request, scope)
        request.state.consumer = identity

    return dep


#: ``?as=`` viewer names to tiers. A cap, never a widening.
PREVIEW_VIEWERS: dict[str, ViewerTier] = {
    "anonymous": "public",
    "lab": "limited",
    "owner": "private",
}


def _client_ip(request: Request) -> Optional[str]:
    client = getattr(request, "client", None)
    return getattr(client, "host", None)


def default_caller_resolver(request: Request) -> Caller:
    """The default caller: a consumer identity, else the operator token, else anonymous.

    Open dev mode (no token configured) resolves to anonymous, not
    ``private``: a missing credential is not a permissive credential.
    """
    consumer = getattr(request.state, "consumer", None)
    if consumer is None:
        verifier = getattr(request.app.state, "consumer_verifier", None)
        if verifier is not None and (request.headers.get("authorization") or ""):
            # Boundary: a verifier that broke is logged, then treated as anonymous.
            try:
                consumer = verifier(request, "read")
            except Exception:
                logger.warning(
                    "consumer_verifier raised; treating the caller as anonymous", exc_info=True
                )
                consumer = None
    ip = _client_ip(request)
    if consumer is not None:
        return Caller(
            is_operator=bool(getattr(consumer, "is_operator", False)),
            tier=getattr(consumer, "tier", "public") or "public",
            consumer=consumer,
            client_ip=ip,
        )
    expected = getattr(request.app.state, "token", None)
    if expected and request.headers.get("authorization") == f"Bearer {expected}":
        return Caller(is_operator=True, tier="private", client_ip=ip)
    # A fresh caller, never the shared ANONYMOUS: its memo is per request.
    return Caller(client_ip=ip)


def resolve_viewer_tier(caller: Caller, slug: str | None) -> ViewerTier:
    """The default viewer tier: the widest tier this caller may be shown.

    ``slug`` is unused: the bare SDK has no per-profile grants.
    """
    del slug
    if caller.is_operator:
        return "private"
    if caller.consumer is not None:
        return getattr(caller.consumer, "tier", "public") or "public"
    return caller.tier or "public"


def get_viewer_tier(
    request: Request,
    as_: Optional[str] = Query(
        None,
        alias="as",
        description="Preview as another viewer: anonymous | lab | owner. a cap, never a widening.",
    ),
) -> ViewerTier:
    """This request's viewer tier, with the ``?as=`` preview cap applied.

    ``slug`` comes from path params, so routes without one get no phantom
    ``?slug=`` query parameter.

    ``?as=`` can only narrow, so it needs no authorization, and a preview runs
    the same handler and projection as the real view. An unknown ``?as=`` is a
    400: ignoring it would show an owner their own view while they believed
    they saw a stranger's.
    """
    request.state.viewer_cap = None
    if as_ is not None:
        requested = PREVIEW_VIEWERS.get(as_)
        if requested is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown viewer {as_!r} (anonymous | lab | owner)",
            )
        request.state.viewer_cap = requested
    caller = get_caller(request)
    if request.state.viewer_cap is not None:
        caller = replace(caller, viewer_cap=request.state.viewer_cap)
    resolved = request.app.state.service.viewer(caller, request.path_params.get("slug"))
    # Read by the response stamp. On multi-profile routes this is the caller's
    # baseline tier; listings are filtered per profile.
    request.state.viewer_tier = resolved
    return resolved


def get_read_caller(request: Request, viewer: ViewerTier = Depends(get_viewer_tier)) -> Caller:
    """This request's caller with its ``?as=`` preview cap, for read routes."""
    del viewer
    caller = get_caller(request)
    cap = getattr(request.state, "viewer_cap", None)
    return caller if cap is None else replace(caller, viewer_cap=cap)


@dataclass(frozen=True)
class TierFloor:
    """A host-imposed ceiling on one profile, and the sentence explaining it.

    They travel together so a shown reason always matches the rule that ran.
    ``tier=None`` means no opinion; ``reason`` is meaningful only with a tier.
    """

    tier: Visibility | None = None
    reason: str | None = None


def get_match_store(request: Request):
    """The app's store, proved able to rank, for ``/api/v1/match``.

    Raises a 503, not a 500, when the extras are missing, the store cannot
    serve vectors, or profiles exist but none is indexed (an empty ranking
    would read as "nobody matched"). Also refreshes the ``rid <-> slug``
    lookup index for shell callers.
    """
    try:
        from ..store._analytics import require_vector_store
    except ImportError as e:  # numpy / embeddings missing (core-only install)
        raise HTTPException(
            status_code=503,
            detail=(
                "matching unavailable: this is a core-only install missing the "
                f"vectors/embeddings extra ({e}). Install the 'vectors' and 'st' extras."
            ),
        ) from e

    store = request.app.state.store

    try:
        from ..errors import CapabilityUnavailableError

        vstore = require_vector_store(store)
        # Touching the accessor imports the lazy analytics.
        _ = vstore.match
    except CapabilityUnavailableError as e:
        raise HTTPException(status_code=503, detail=f"matching unavailable: {e}") from e
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "matching unavailable: this is a core-only install missing the "
                f"vectors/embeddings extra ({e}). Install the 'vectors' and 'st' extras."
            ),
        ) from e

    try:
        slugs = store.list_slugs()
        indexed = any(store.has_vector_index(slug) for slug in slugs)
    # Boundary: enumerating the store and probing every profile's index.
    except Exception as e:
        logger.exception("failed to survey the store for matching")
        raise HTTPException(
            status_code=503,
            detail=f"matching unavailable: the profiles store could not be read: {e}",
        ) from e

    if slugs and not indexed:
        raise HTTPException(
            status_code=503,
            detail=(
                f"matching unavailable: {len(slugs)} profile(s) exist in "
                f"{store.location} but none has a usable embedding index"
            ),
        )

    store.write_lookup_index()
    return vstore


def materialize_store_to_tempdir(request: Request, store: ProfileStore):
    """Export every profile from a rootless store to a temp directory.

    The graph needs a directory. The temp dir is cached on
    ``service.registry_tempdir`` until ``Service.invalidate`` drops it.
    Export failures are per profile, so one bad row does not cost the graph.
    """
    import tempfile
    from pathlib import Path

    service = request.app.state.service
    existing = service.registry_tempdir
    if existing is not None:
        return Path(existing.name)

    tmpdir = tempfile.TemporaryDirectory(prefix="rp-registry-")
    service.registry_tempdir = tmpdir
    root = Path(tmpdir.name)

    slugs = store.list_slugs()
    for slug in slugs:
        try:
            store.export_directory(slug, root / slug)
        # Boundary: one profile's export; the rest of the corpus still materializes.
        except Exception:
            logger.warning("failed to export profile %s for the graph", slug, exc_info=True)

    logger.info("materialized %d profiles to %s for the graph", len(slugs), root)
    return root


def get_graph(request: Request):
    """Lazily build and cache the profile graph on ``service.graph``.

    Raises a 503 only if the store is unreadable.
    """
    service = request.app.state.service
    graph = service.graph
    if graph is not None:
        return graph

    try:
        from ..graph import ProfileGraph
    except ImportError as e:  # pragma: no cover - graph is core-only, should import
        raise HTTPException(
            status_code=503,
            detail="graph unavailable: could not import the graph subpackage",
        ) from e

    store = request.app.state.store
    root = store.root

    if root is None:
        root = materialize_store_to_tempdir(request, store)

    try:
        graph = ProfileGraph.from_store(root)
    # Boundary: building the graph reads every profile in the store.
    except Exception as e:
        logger.exception("failed to build ProfileGraph")
        raise HTTPException(
            status_code=503,
            detail="graph unavailable: the profiles store could not be read",
        ) from e
    service.graph = graph
    return graph


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def get_token_from_env() -> Optional[str]:
    return os.environ.get("RESEARCHER_PROFILES_TOKEN") or None
