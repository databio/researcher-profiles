"""FastAPI server exposing ``ResearcherProfile`` over HTTP.

Requires the ``api`` extra; see the install instructions in the README.

Run with:

    python -m researcher_profiles.api --profiles-dir <DIR> --port 8109

The host surface
================

The names below are the stable surface a host builds against. Anything not
listed here is internal.

Application factory
    ``create_app`` builds the bare server: every router mounted, every
    ``app.state`` hook left at its default.

Routers
    ``public_router`` carries the tier-projected read surface (listing,
    detail, JSON-LD, artifacts, papers, per-paper summary). ``router`` carries
    the scope-gated write, search, match, graph and generative endpoints.
    ``edit_router`` carries the owner-gated interactive edit endpoints. All
    three are declared with the ``/api/v1`` prefix; a host that wants a
    different prefix walks ``router.routes`` and re-adds each endpoint.

Route handlers
    The read, search and generative handlers are exported so a host can
    re-declare them on its own ``APIRouter`` with its own dependencies:
    see ``__all__``. The profile, paper, text and passage handlers take the
    ``Service`` and a ``Caller`` (``deps.get_service``,
    ``deps.get_read_caller``); the rest take the store and the viewer tier.
    ``list_profiles`` returns a ``Response`` carrying the ``rp:profileList``
    envelope, not a list of models.

Service layer
    ``Service`` and its functions (``api.service``) hold every permission
    check; ``create_app`` builds one on ``app.state.service``.
    ``service_error_response`` maps the typed errors to HTTP; a host that
    re-declares the handlers on its own app calls
    ``install_service_errors(app)``.

Read projection
    ``artifact_visible``, ``metadata_payload`` and ``withheld``. A host that
    composes its own surface uses the same three, so it and the SDK cannot
    disagree about what a viewer may see. ``served_document`` and
    ``served_document_bytes`` build the served ``profile.jsonld``.

``api.deps``
    The dependency callables a host reuses or wraps: ``get_store``,
    ``get_service``, ``get_caller``, ``get_viewer_tier``, ``require_scope``,
    ``ConsumerIdentity`` (what a ``consumer_verifier`` returns), ``TierFloor``
    (what a ``profile_tier_floor`` returns) and
    ``materialize_store_to_tempdir`` (how a store without a filesystem root is
    exported for the registry).

``api.upload``
    The archive codec and its constants: ``build_profile_archive`` (which
    returns a ``ProfileArchive``: the bytes, its members, and what on disk did
    not ship), ``extract_profile_archive``, ``ingest_archive``,
    ``PROFILE_TOP_LEVEL``, ``SOURCES_MEMBERS``, ``CACHE_MEMBERS`` and
    ``DEFAULT_MAX_UPLOAD_BYTES``.

``app.state.hooks``
    A ``Hooks`` (see ``api.hooks``). Two request-shaped hooks stay on
    ``app.state`` because they gate the push/search/match plane:
    ``consumer_verifier`` and ``push_gate``. An embedding preflight reports
    through ``app.state.embedding_healthy`` and ``embedding_health_detail``.
"""

from ._errors import install_service_errors, service_error_response
from ._projection import (
    artifact_visible,
    metadata_payload,
    served_document,
    served_document_bytes,
    withheld,
)
from .app import create_app
from .caller import ANONYMOUS, Caller
from .hooks import Hooks
from .routers._routers import edit_router, public_router, router
from .routers.generative import ask_profile, innovate_profile, review_profile, riff_profile
from .routers.read import (
    get_paper,
    get_profile_detail,
    get_summaries,
    list_papers,
    list_profiles,
)
from .routers.search import search_profile
from .service import Service

__all__ = [
    "ANONYMOUS",
    "Caller",
    "Hooks",
    "Service",
    "artifact_visible",
    "ask_profile",
    "create_app",
    "edit_router",
    "get_paper",
    "get_profile_detail",
    "get_summaries",
    "install_service_errors",
    "innovate_profile",
    "list_papers",
    "list_profiles",
    "metadata_payload",
    "public_router",
    "review_profile",
    "riff_profile",
    "router",
    "search_profile",
    "served_document",
    "served_document_bytes",
    "service_error_response",
    "withheld",
]
