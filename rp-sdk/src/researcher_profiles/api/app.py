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

# Importing the route modules registers their handlers on the routers.
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

    Wrap a directory in ``FilesystemProfileStore(dir)``::

        from researcher_profiles.store import FilesystemProfileStore
        create_app(FilesystemProfileStore("~/researcher-profiles"))

        from researcher_profiles.store.sql import SqlProfileStore   # [sql]
        create_app(SqlProfileStore("postgresql://user@host/db"))

    Every read resolves a viewer tier through
    ``app.state.hooks.viewer_resolver`` and projects its response. See
    ``docs-dev/rp-sdk/developer/read-seam.md``.

    Parameters
    ----------
    store:
        Any :class:`~researcher_profiles.store.ProfileStore`.
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
        in, so the registry does not rely on clients excluding the text.
        Enable it only on a private registry entitled to hold the corpus.
    pre_commit_hooks:
        Callables run inside every profile write, before commit; each takes
        one :class:`~researcher_profiles.profile.WriteContext` and a raise
        aborts the write. Registered on the store, not ``app.state``, so
        writes that bypass the HTTP routes (CLI, background jobs) still fire
        them. See
        :meth:`researcher_profiles.profile.ResearcherProfile.write_unit`.
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
    app.state.store = store
    app.state.token = token or None
    from .upload import DEFAULT_MAX_UPLOAD_BYTES

    app.state.max_upload_bytes = max_upload_bytes or DEFAULT_MAX_UPLOAD_BYTES
    if accept_fulltext is None:
        accept_fulltext = _env_flag("RESEARCHER_PROFILES_ACCEPT_FULLTEXT")
    app.state.accept_fulltext = accept_fulltext
    # Host seams; see ``api.hooks``.
    app.state.hooks = Hooks()
    app.state.service = Service(store, app.state.hooks, open_mode=not app.state.token)
    # Consumer-to-scope hook. ``None``: ``require_scope`` falls back to the
    # operator token. See deps.require_scope.
    app.state.consumer_verifier = None
    # Push-identity hook: ``(request, slug, rid) -> None``, called on
    # ``PUT /profiles/{slug}`` once the body's rid is known and before commit.
    # ``require_scope("push")`` runs before the body is read, so it can only
    # answer "may this credential push at all"; this hook answers "may it push
    # this rid", raising ``HTTPException(403)`` to refuse.
    app.state.push_gate = None
    # A host's embedding preflight sets this False so ``/health`` answers 503;
    # otherwise /match would silently return no matches.
    app.state.embedding_healthy = True
    app.state.embedding_health_detail = None
    for hook in pre_commit_hooks or ():
        store.add_pre_commit_hook(hook)

    install_service_errors(app)

    if not app.state.token:
        logger.warning(
            "RESEARCHER_PROFILES_TOKEN not set: server is in OPEN MODE. "
            "Do not expose this port publicly."
        )

    @app.middleware("http")
    async def _stamp_viewer_tier(request, call_next):
        """Report the tier every projected response was computed against.

        ``X-RP-Viewer-Tier`` lets a client check what it was shown as without
        parsing the body. Set from ``request.state`` (``deps.get_viewer_tier``),
        so refusals carry it too and a route that resolved no tier claims none.
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

    app.include_router(v1_router)
    # Gated by hooks.edit_gate inside the service functions.
    app.include_router(v1_edit_router)
    # No router-level dependency: every read route resolves a viewer tier and
    # projects its own response, so a refusal is a 404 on one profile rather
    # than a 401 on the whole registry. A router-level gate can only ask "is
    # this a known credential", which is the wrong question for reads.
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

    A GET under a profile that no route claims is rewritten to
    ``.../content/<artifact>`` before routing. Mounts are ignored when
    deciding "no route claims it", so a host's catch-all mount at ``/`` does
    not swallow these paths. Routes are read per request, so routes a host
    adds later still win.
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

    The store comes from :func:`build_store`, which raises ``ValueError``
    when neither ``$RESEARCHER_PROFILES_DATABASE_URL`` nor
    ``$RESEARCHER_PROFILES_ROOT`` is set.
    """
    configure_logging()
    max_mb = os.environ.get("RESEARCHER_PROFILES_MAX_UPLOAD_MB")
    return create_app(
        build_store(),
        token=os.environ.get("RESEARCHER_PROFILES_TOKEN") or None,
        max_upload_bytes=int(max_mb) * 1024 * 1024 if max_mb else None,
    )


# ``None`` when no store is configured, so importing this module never fails.
app: Optional[FastAPI]
try:
    # A retired env var name also counts, so build_store() raises its
    # RetiredEnvVarError instead of silently leaving ``app = None``.
    _configured = os.environ.get(PROFILES_ROOT_ENV_VAR) or os.environ.get(DATABASE_URL_ENV_VAR)
    _retired_set = any(os.environ.get(name) for name in RETIRED_ENV_VARS)
    app = _build_default_app() if (_configured or _retired_set) else None
# Boundary: a misconfigured server must still import; log why it has no app.
except Exception:  # pragma: no cover
    logger.exception("could not build the default app from the environment")
    app = None
