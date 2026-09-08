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
    ``list_profiles``, ``get_profile_detail``, ``list_papers``,
    ``get_paper_summary``, ``search_profile``, ``ask_profile``,
    ``review_profile``, ``innovate_profile``, ``riff_profile``. Each takes the
    store and the viewer tier as arguments, and a host supplies them through
    its own ``Depends``. ``list_profiles`` returns a ``Response`` carrying the
    ``rp:profileList`` envelope, not a list of models.

Read projection
    ``artifact_visible``, ``invalidate_after_write``, ``metadata_payload`` and
    ``withheld`` are the functions the route modules project through. A host
    that composes its own surface uses the same four, so it and the SDK cannot
    disagree about what a viewer may see or about what a write invalidates.

``api.deps``
    The dependency callables a host reuses or wraps: ``get_store``,
    ``get_viewer_tier``, ``get_registry``, ``require_scope``,
    ``ConsumerIdentity`` (what a ``consumer_verifier`` returns), ``TierFloor``
    (what a ``profile_tier_floor`` returns) and
    ``materialize_store_to_tempdir`` (how a store without a filesystem root is
    exported for the registry).

``api.upload``
    The archive codec and its constants: ``build_profile_archive``,
    ``extract_profile_archive``, ``ingest_archive``, ``PROFILE_TOP_LEVEL``,
    ``SOURCES_MEMBERS``, ``CACHE_MEMBERS`` and ``DEFAULT_MAX_UPLOAD_BYTES``.

``app.state`` hooks
    Every route reads its store from ``app.state.store``; a host that mounts
    the routers or re-declares the handlers sets it to a ``ProfileStore``.
    ``create_app`` sets the auth and policy hooks to ``None`` and a host
    replaces them with callables: ``owner_verifier``, ``consumer_verifier``,
    ``write_scope_verifier``, ``push_gate``, ``viewer_resolver`` and
    ``profile_tier_floor``. A host that runs an embedding preflight reports it
    through ``embedding_healthy`` and ``embedding_health_detail``. Each is
    described where ``create_app`` sets it and in the root ``AGENTS.md``.
"""

from ._projection import (
    artifact_visible,
    invalidate_after_write,
    metadata_payload,
    withheld,
)
from .app import create_app
from .routers._routers import edit_router, public_router, router
from .routers.generative import ask_profile, innovate_profile, review_profile, riff_profile
from .routers.read import get_paper_summary, get_profile_detail, list_papers, list_profiles
from .routers.search import search_profile

__all__ = [
    "artifact_visible",
    "ask_profile",
    "create_app",
    "edit_router",
    "get_paper_summary",
    "get_profile_detail",
    "innovate_profile",
    "invalidate_after_write",
    "list_papers",
    "list_profiles",
    "metadata_payload",
    "public_router",
    "review_profile",
    "riff_profile",
    "router",
    "search_profile",
    "withheld",
]
