"""The HTTP client side of researcher profiles: two read-only storage
backends, the registry verbs, and the local profile cache.

Requires the ``client`` extra (``httpx``), which is imported inside the
functions that open a connection, so this package imports on a core-only
install.

Both backends are read-only: every writer is refused by
:class:`~researcher_profiles.profile.storage.ReadOnlyArtifactStorage` with a
message naming :func:`install_profile`. Otherwise a profile on either behaves
like one from ``ResearcherProfile.from_files``.

Every public name is re-exported here, plus the private helpers
``ResearcherProfile`` and the tests use (``_split_profile_url``,
``_fetch_profiles``, ``_s3_to_https``).
"""

from ._http import (
    _split_profile_url,  # noqa: F401  (re-exported)
    auth_headers,
)
from ._identity import resolve_rid
from ._push import (
    PLAN_GROUPS,
    PushInsufficientAccess,
    PushPlan,
    PushRefused,
    PushResult,
    PushWouldRemove,
    push_profile,
)
from ._registry import (
    CACHE_ENV_VAR,  # noqa: F401  (re-exported)
    REGISTRY_ENV_VAR,  # noqa: F401  (re-exported)
    RegistryListing,
    _fetch_profiles,  # noqa: F401  (re-exported)
    install_profile,
    list_installed,
    list_registry,
    rank_against,
    resolve_registries,
    seek_profile,
)
from ._storage import (  # noqa: F401  (re-exported)
    ApiArtifactStorage,
    StaticArtifactStorage,
    _s3_to_https,
)

__all__ = [
    "ApiArtifactStorage",
    "auth_headers",
    "StaticArtifactStorage",
    "PLAN_GROUPS",
    "PushPlan",
    "PushInsufficientAccess",
    "PushRefused",
    "PushResult",
    "PushWouldRemove",
    "push_profile",
    "resolve_rid",
    "install_profile",
    "seek_profile",
    "list_installed",
    "list_registry",
    "RegistryListing",
    "resolve_registries",
    "rank_against",
]
