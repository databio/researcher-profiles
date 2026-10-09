"""``ProfileStore``: a set of profiles you can enumerate, look up, create, and delete.

One level down, each profile's own artifacts are handled by
:class:`researcher_profiles.profile.storage.ArtifactStorage`. A ``ProfileStore``
is what an HTTP app or ingest path is handed.

Three backends ship in the SDK:

===============================  ===========================  ============
Store                            Backing                      Extra
===============================  ===========================  ============
:class:`FilesystemProfileStore`  a directory of directories   core
``store.sql.SqlProfileStore``    the ``rp_*`` SQL tables      ``[sql]``
``store.http.HttpProfileStore``  a published site, over HTTP  ``[client]``
===============================  ===========================  ============

:class:`~.protocol.VectorStore` is an optional capability protocol: a store that
can also hand out a profile's vectors. All three backends implement it and
share one read implementation
(:class:`~researcher_profiles.embeddings.flat.FlatEmbeddingIndex`), so cosine
is computed the same way everywhere.

Only a sanctioned list may branch on :attr:`~.protocol.ProfileStore.root`; see
that property.
"""

from importlib import import_module

from .factory import build_store
from .files import FilesystemProfileStore
from .protocol import (
    DuplicateIdentityError,
    IngestResult,
    ProfileNotFoundError,
    ProfileStore,
    RetiredRidError,
    UploadError,
    VectorStore,
)

__all__ = [
    "DuplicateIdentityError",
    "FilesystemProfileStore",
    "build_store",
    "HttpProfileStore",
    "IngestResult",
    "ProfileNotFoundError",
    "ProfileStore",
    "RetiredRidError",
    "SqlProfileStore",
    "SqlArtifactStorage",
    "UploadError",
    "VectorStore",
]


_LAZY_NAMES = {
    "HttpProfileStore": (".http", "client"),
    "SqlProfileStore": (".sql", "sql"),
    "SqlArtifactStorage": (".sql", "sql"),
}


def __getattr__(name: str):
    """PEP 562: import the SQL and HTTP backends on first use.

    A guarded module-level import would still load SQLAlchemy whenever the
    extra is installed; light commands such as ``rp list`` must not (a
    guardrail asserts it). The resolved name is cached in ``globals()``.
    """
    lazy = _LAZY_NAMES.get(name)
    if lazy is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    submodule, extra = lazy
    try:
        module = import_module(submodule, __name__)
    except ImportError as e:
        raise ImportError(
            f"{name!r} requires the {extra!r} extra; see the install instructions in the README"
        ) from e
    value = getattr(module, name)
    globals()[name] = value
    return value
