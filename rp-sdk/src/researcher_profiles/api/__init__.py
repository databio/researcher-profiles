"""FastAPI server exposing ``ResearcherProfile`` over HTTP.

Requires the ``api`` extra; see the install instructions in the README.

Run with:

    python -m researcher_profiles.api --profiles-dir <DIR> --port 8109

The host surface
================

A host is a service that serves these routes, mounts them under its own
prefix and auth, or composes its own surface on top of the same projection.
The names below are the stable surface a host builds against. Anything not
listed here is internal to the route modules.

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
    ``list_profiles``, ``get_profile_detail``, ``list_papers``, ``get_paper``,
    ``get_summaries``, ``search_profile``, ``ask_profile``,
    ``review_profile``, ``innovate_profile``, ``riff_profile``. Each takes the
    store and the viewer tier as arguments, and a host supplies them through
    its own ``Depends``. ``list_profiles`` returns a ``Response`` carrying the
    ``rp:profileList`` envelope, not a list of models.

Service layer
    ``Service`` (``api.service``) holds the store, the hooks and the app's
    caches; ``create_app`` builds one on ``app.state.service``. The functions
    beside it (``get_profile``, ``list_papers``, the text and passage reads,
    ``edit_metadata``, ``add_work`` and the other edits) take the service, a
    ``Caller`` (``api.caller``) and their arguments, hold every permission
    check, and raise the typed errors in ``researcher_profiles.errors``. The
    routes are thin adapters over them, and a host's MCP server calls the same
    functions. ``api._errors.service_error_response`` maps the typed errors to
    HTTP; ``create_app`` installs it. After a write, ``Service.invalidate``
    drops every cache that could reflect the old profile.

Read projection
    ``artifact_visible``, ``metadata_payload`` and ``withheld`` are the
    functions the route modules project through. A host that composes its own
    surface uses the same three, so it and the SDK cannot disagree about what
    a viewer may see. ``served_document`` and ``served_document_bytes`` build
    the served ``profile.jsonld``: the stored record plus the registry-issued
    proofs ``Service.proofs`` computes per read.

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
    The host seams, one field per seam on a ``Hooks`` (``api.hooks``), every
    one but ``caller_resolver`` taking a ``Caller`` instead of a request:
    ``caller_resolver``, ``viewer_resolver``, ``profile_tier_floor``,
    ``edit_gate``, ``write_scope``, ``record_edit``, ``registry_proofs`` and
    ``store_for``. A host assigns the ones it fills. Two request-shaped hooks
    stay on ``app.state`` because they gate the push/search/match plane:
    ``consumer_verifier`` and ``push_gate``. A host that runs an embedding
    preflight reports it through ``app.state.embedding_healthy`` and
    ``embedding_health_detail``. Each is described in ``api.hooks``, where
    ``create_app`` sets it, and in the root ``AGENTS.md``.
"""

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
    "withheld",
]
