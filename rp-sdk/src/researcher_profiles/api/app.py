"""FastAPI application factory for researcher-profiles.

Also exposes a module-level ``app`` so ``uvicorn researcher_profiles.api.app:app``
works (environment-driven configuration).
"""

import logging
import os
import re
from collections.abc import Sequence
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, Response
from starlette.routing import Match, Mount

# Import the package eagerly so a serving process pays the optional-extra
# import cost at startup rather than on the first request that needs it.
import researcher_profiles  # noqa: F401

try:
    import researcher_profiles.embeddings  # noqa: F401
except ImportError:  # pragma: no cover - the vectors extra is optional
    pass

from ..env import RETIRED_ENV_VARS
from ..models.api import HealthResponse
from ..store import ProfileStore, build_store
from ..store.config import DATABASE_URL_ENV_VAR, PROFILES_ROOT_ENV_VAR
from ._errors import install_service_errors
from .deps import configure_logging
from .hooks import Hooks

# The six route modules hang their handlers on the three routers declared in
# ``_projection`` at import time, so importing them here is what puts the routes
# on the routers. Nothing in this file calls into them by name.
from .routers import (  # noqa: F401
    edit,
    generative,
    identity,
    passages,
    push,
    read,
    search,
)
from .routers._routers import edit_router as v1_edit_router
from .routers._routers import public_router as v1_public_router
from .routers._routers import router as v1_router
from .service import Service

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def create_app(
    store: ProfileStore,
    token: Optional[str] = None,
    *,
    max_upload_bytes: Optional[int] = None,
    accept_fulltext: Optional[bool] = None,
    pre_commit_hooks: Optional[Sequence[Any]] = None,
) -> FastAPI:
    """Build a FastAPI app over a :class:`~researcher_profiles.store.ProfileStore`.

    Takes a store rather than a directory, so the same server can be backed by
    a directory or a database. There is no path-accepting form; wrap a
    directory in ``FilesystemProfileStore(dir)`` and pass that::

        from researcher_profiles.store import FilesystemProfileStore
        create_app(FilesystemProfileStore("~/researcher-profiles"))

        from researcher_profiles.store.sql import SqlProfileStore   # [sql]
        create_app(SqlProfileStore("postgresql://user@host/db"))

    Read privacy is a property of each profile and each caller, not a switch on
    the read surface: every read resolves a viewer tier through
    ``app.state.hooks.viewer_resolver`` and projects its response. The default
    is :func:`researcher_profiles.api.deps.resolve_viewer_tier`, which is the
    right answer for a directory you mean to serve whole. A host with sessions
    or grants installs its own. See ``docs-dev/rp-sdk/developer/read-seam.md``.

    Parameters
    ----------
    store:
        The profile store to serve. Any object satisfying the
        :class:`~researcher_profiles.store.ProfileStore` protocol. Its LRU
        capacity (if it has one) is the store's business, not this
        function's.
    token:
        If set, ``Authorization: Bearer <token>`` is required on every
        endpoint. If ``None`` or empty the server runs in open mode and
        logs a warning.
    max_upload_bytes:
        Size cap for profile pushes (``PUT /api/v1/profiles/{slug}``);
        defaults to 50 MB.
    accept_fulltext:
        Whether ``PUT /api/v1/profiles/{slug}`` *stores* extracted paper
        fulltext a client sends. Defaults to the
        ``RESEARCHER_PROFILES_ACCEPT_FULLTEXT`` env var, else False, in
        which case ``sources/papers/`` is stripped during ingest and never
        touches the profiles root. This is the copyright boundary on the way
        IN: without it the registry would be trusting every client to have
        excluded the text. Enable it only on a private registry entitled to
        hold the corpus.
    pre_commit_hooks:
        Callables a management host registers to run inside every profile
        write, before commit. See
        :meth:`researcher_profiles.profile.ResearcherProfile.write_unit`. Each
        takes one :class:`~researcher_profiles.profile.WriteContext`. A hook
        that raises aborts the write; nothing is swallowed. Registered once
        here at composition time, on the store rather than on ``app.state``, so
        writes that never touch an HTTP route still fire it. Equivalent to
        calling ``store.add_pre_commit_hook`` yourself before building the app.
    """
    if not isinstance(store, ProfileStore):
        raise TypeError(
            f"create_app() takes a ProfileStore, got {type(store).__name__}. "
            "For a directory of profiles, wrap it: "
            "create_app(FilesystemProfileStore(profiles_dir), ...)"
        )

    app = FastAPI(
        title="researcher-profiles",
        version="1",
        description=(
            "HTTP API exposing per-researcher ResearcherProfile capabilities: "
            "metadata, papers, summaries, semantic search, ask, review, "
            "innovate, riff."
        ),
    )
    # The store. ``app.state.profiles_dir`` is gone with it: a store that is a
    # directory answers ``store.root``, and one that is not answers ``None``
    # instead of handing out a path that does not exist.
    app.state.store = store
    app.state.token = token or None
    from .upload import DEFAULT_MAX_UPLOAD_BYTES

    app.state.max_upload_bytes = max_upload_bytes or DEFAULT_MAX_UPLOAD_BYTES
    if accept_fulltext is None:
        accept_fulltext = _env_flag("RESEARCHER_PROFILES_ACCEPT_FULLTEXT")
    app.state.accept_fulltext = accept_fulltext
    # The host seams (``api.hooks.Hooks``): caller-shaped callables a host
    # assigns, e.g. ``app.state.hooks.viewer_resolver = ...``. Bare rp-sdk
    # leaves them at their defaults: the operator token edits, the consumer
    # identity or the operator token sets the read tier, no floor, no proofs.
    # See ``docs-dev/rp-sdk/developer/read-seam.md``.
    app.state.hooks = Hooks()
    # The service every route and every host adapter calls: the store, the
    # hooks, and the caches (the lazily built graph, a rootless store's temp
    # export) that ``Service.invalidate`` drops after a write.
    app.state.service = Service(store, app.state.hooks, open_mode=not app.state.token)
    # Consumer-to-scope hook for the write/heavy/LLM router. Left None here: a bare
    # rp-sdk server has no consumer layer, so ``require_scope`` falls back to the
    # operator token. A management host sets a callable here to switch
    # those endpoints to scoped per-consumer keys. See deps.require_scope.
    app.state.consumer_verifier = None
    # Push-identity hook: ``(request, slug, rid) -> None``, called on
    # ``PUT /profiles/{slug}`` once the body's rid is known and before anything
    # is committed. ``require_scope("push")`` runs before the body is read, so
    # it can only ever answer "may this credential push at all"; a host whose
    # keys are bounded to particular people's profiles (a management host's
    # ``push_own``) answers "may it push this rid" here, raising
    # ``HTTPException(403)`` to refuse. Left None: bare rp-sdk has one operator
    # token and no notion of whose profile a rid is.
    app.state.push_gate = None
    # Health flag: a host that runs a startup query-embedding preflight
    # flips this to False so ``/health`` answers 503 and the
    # container's HEALTHCHECK stops routing traffic to a deployment whose
    # /match would otherwise silently return 200 {"matches": []} forever.
    # Bare rp-sdk never sets it, so /health stays "ok" here.
    app.state.embedding_healthy = True
    app.state.embedding_health_detail = None
    # Pre-commit hook for hosts. Registered on the store (here, the profile
    # cache), not on ``app.state``: a hook registered on an app only fires for
    # writes that went through a route that remembered to fire it, so a
    # background writer that bypasses the routes would skip it. On the store it
    # fires for every write: API routes, CLI, and out-of-process runs alike.
    # See ResearcherProfile.write_unit for the ordering and failure contracts.
    for hook in pre_commit_hooks or ():
        store.add_pre_commit_hook(hook)

    # The HTTP mapper: every typed service error becomes its status and body.
    install_service_errors(app)

    if not app.state.token:
        logger.warning(
            "RESEARCHER_PROFILES_TOKEN not set: server is in OPEN MODE. "
            "Do not expose this port publicly."
        )

    @app.middleware("http")
    async def _stamp_viewer_tier(request, call_next):
        """Report the tier every projected response was computed against.

        ``X-RP-Viewer-Tier`` makes a preview verifiable from outside the body: a
        client (or a test) can assert what it was shown as without parsing what
        it was shown. Stamped from ``request.state``, which
        ``deps.get_viewer_tier`` sets as it resolves, so refusals carry it too,
        and a route that never resolved a tier never claims one.
        """
        response = await call_next(request)
        tier = getattr(request.state, "viewer_tier", None)
        if tier:
            response.headers["X-RP-Viewer-Tier"] = str(tier)
        return response

    @app.get("/health", response_model=HealthResponse)
    def health(response: Response) -> HealthResponse:
        healthy = getattr(app.state, "embedding_healthy", True)
        if not healthy:
            response.status_code = 503
            return HealthResponse(
                status="degraded",
                store=store.location,
                profile_count=len(store.list_slugs()),
                detail=getattr(app.state, "embedding_health_detail", None),
            )
        return HealthResponse(
            status="ok",
            store=store.location,
            profile_count=len(store.list_slugs()),
        )

    # Always-gated write/heavy/LLM router.
    app.include_router(v1_router)
    # Owner-scoped interactive edit router (gated by hooks.edit_gate inside the
    # service functions: operator token on bare rp-sdk, user session under a
    # management host).
    app.include_router(v1_edit_router)
    # Registry-browser read surface. Mounted with no router-level dependency:
    # every route on it resolves a viewer tier and projects its own response, so
    # a refusal is a 404 on one profile rather than a 401 on the whole registry.
    # A router-level gate could only ever ask "is this caller a credential we
    # recognize", which is the wrong question. It locked a signed-in owner out
    # of their own profile while an `rpk_` key read everybody else's.
    # The product UI is the separately deployed rp-browser SPA; this app
    # serves JSON only.
    app.include_router(v1_public_router)
    _serve_artifacts_beside_the_document(app)
    return app


# ``/api/v1/profiles/{slug}/<artifact>``: a GET under a profile.
_SIBLING_ARTIFACT = re.compile(r"^(/api/v1/profiles/[^/]+/)(?!content/)(.+)$")


def _serve_artifacts_beside_the_document(app: FastAPI) -> None:
    """Make ``/api/v1/profiles/{slug}/`` a base URL, like ``.../content/``.

    ``GET /api/v1/profiles/{slug}/profile.jsonld`` serves the document, and a
    consumer resolves its relative ``contentUrl``s against the URL it fetched
    (spec 3.3, Base conformance rule 6). So ``personality/SOUL.md`` must answer
    at ``/api/v1/profiles/{slug}/personality/SOUL.md``, exactly as it does on a
    static site, where the document and its files are siblings.

    A middleware, not a route: a GET under a profile that no route claims is
    rewritten to ``.../content/<artifact>`` before routing. Mounts are ignored
    when deciding "no route claims it", because a host may mount a catch-all
    at ``/`` (Prosopia's SPA does), and that must not swallow these paths.
    Routes are read per request, so ones a host adds after this app is built
    (Prosopia's ``/overlay``) still win. Anything the content route refuses
    (not in the manifest, a withheld tier) is still that route's 404.
    """
    app.add_middleware(_SiblingArtifactMiddleware, router=app.router)


class _SiblingArtifactMiddleware:
    def __init__(self, app, router) -> None:
        self.app = app
        self.router = router

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("GET", "HEAD"):
            root = scope.get("root_path", "")
            prefix = root if root and scope["path"].startswith(root) else ""
            m = _SIBLING_ARTIFACT.match(scope["path"][len(prefix) :])
            if m and not self._routed(scope):
                path = f"{prefix}{m.group(1)}content/{m.group(2)}"
                scope = {**scope, "path": path, "raw_path": quote(path).encode()}
        await self.app(scope, receive, send)

    def _routed(self, scope) -> bool:
        """True when a real route (not a mount) matches this path."""
        return any(
            not isinstance(route, Mount) and route.matches(scope)[0] != Match.NONE
            for route in self.router.routes
        )


def _build_default_app() -> FastAPI:
    """The env-driven app for ``uvicorn researcher_profiles.api.app:app``.

    Reads nothing about which store to build itself: :func:`build_store`
    (called with no store arguments here) is the one place that env-to-store
    composition rule lives, so this and ``python -m researcher_profiles.api``
    can never disagree about it. ``build_store`` raises ``ValueError`` when
    neither ``$RESEARCHER_PROFILES_DATABASE_URL`` nor
    ``$RESEARCHER_PROFILES_ROOT`` is set.
    """
    configure_logging()
    max_mb = os.environ.get("RESEARCHER_PROFILES_MAX_UPLOAD_MB")
    return create_app(
        build_store(),
        token=os.environ.get("RESEARCHER_PROFILES_TOKEN") or None,
        max_upload_bytes=int(max_mb) * 1024 * 1024 if max_mb else None,
    )


# Module-level app for `uvicorn researcher_profiles.api.app:app`. Built
# lazily so importing this module without env vars set (e.g. for tests)
# doesn't crash.
app: Optional[FastAPI]
try:
    # A retired ``RP_*`` name counts as "something is configured" here too,
    # even though it does not satisfy build_store(): the point is to let
    # build_store() raise its fail-loud RetiredEnvVarError instead of quietly
    # skipping straight to `app = None`, which would import cleanly and 404
    # every request with no clue that a renamed variable is the reason.
    _configured = os.environ.get(PROFILES_ROOT_ENV_VAR) or os.environ.get(DATABASE_URL_ENV_VAR)
    _retired_set = any(os.environ.get(name) for name in RETIRED_ENV_VARS)
    app = _build_default_app() if (_configured or _retired_set) else None
# Boundary: process start-up against whatever the environment says. A
# misconfigured server must still import, so it starts with no app and 404s;
# without this log it would do so with no explanation anywhere.
except Exception:  # pragma: no cover
    logger.exception("could not build the default app from the environment")
    app = None
