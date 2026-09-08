"""The HTTP client side of researcher profiles: two read-only storage
backends, the registry verbs, and the local profile cache.

Requires the ``client`` extra (``httpx``). Every module here imports
``httpx`` inside the function that opens a connection, so importing this
package on a core-only install still works.

:class:`ApiArtifactStorage`
    One profile served by a live ``researcher_profiles.api`` server.
    ``ResearcherProfile.from_api(...)`` builds one. The server owns the model,
    the corpus and the embedding index, so ``prof.persona`` and ``prof.index``
    are HTTP-backed here.

:class:`StaticArtifactStorage`
    One profile published as a directory on a dumb static host (Cloudflare
    Pages, S3, Apache). ``ResearcherProfile.from_url(...)`` builds one. One
    lazy GET per artifact, and the manifest in ``profile.jsonld`` stands in for
    the directory listing a static host does not have.

Both are read-only: every writer is refused by
:class:`~researcher_profiles.profile.storage.ReadOnlyArtifactStorage` with one
message naming :func:`install_profile`. To callers, the profile in front of either behaves
the same as one from ``ResearcherProfile.from_files``, with the same
properties and the same return dataclasses.

The remote persona verbs (``ask``, ``review``, ``innovate``, ``riff``) accept
exactly the parameters the wire contract carries, which makes their signatures
a strict subset of the local ones. The local-only LLM transport knobs,
``thinking`` and ``max_tokens``, have no field on ``AskRequest``,
``ReviewRequest``, ``InnovateRequest``, or ``RiffRequest``, so they are not
accepted here. Forwarding them would only move the mismatch one hop:
``_APIModel`` sets ``extra="allow"``, so a server would accept and silently
ignore them.

How the client package is laid out
==================================

``_storage`` holds the two backends and the remote ``persona`` / ``index``
managers behind :class:`ApiArtifactStorage`. ``_registry`` holds everything
that talks to a registry as a whole or to the local cache: :func:`list_registry`,
:func:`rank_against`, :func:`install_profile`, :func:`seek_profile`,
:func:`list_installed`. :func:`push_profile` (``_push``) and
:func:`resolve_rid` (``_identity``) each get a module of their own. ``_http``
holds the URL and request-body helpers the rest share, including
:func:`auth_headers`, the bearer header a caller that speaks to the server with
its own HTTP client builds from a token. Every public name is re-exported here,
and so are the private helpers ``ResearcherProfile`` and the tests reach for
(``_split_profile_url``, ``_fetch_profiles``, ``_s3_to_https``).
"""

from ._http import (
    _split_profile_url,  # noqa: F401  (re-exported)
    auth_headers,
)
from ._identity import resolve_rid
from ._push import push_profile
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
