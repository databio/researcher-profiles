"""``ProfileStore``: a set of profiles you can enumerate, look up, create, and delete.

One level down, each profile's own artifacts are handled by
:class:`researcher_profiles.profile.storage.ArtifactStorage`. A ``ProfileStore``
is what an HTTP app, an ingest path, or a management host is handed.

Three backends ship in the SDK:

===============================  ===========================  ============
Store                            Backing                      Extra
===============================  ===========================  ============
:class:`FilesystemProfileStore`  a directory of directories   core
``store.sql.SqlProfileStore``    the ``rp_*`` SQL tables      ``[sql]``
``store.http.HttpProfileStore``  a published site, over HTTP  ``[client]``
===============================  ===========================  ============

:class:`~.protocol.VectorStore` is an optional capability protocol: a store that
can also hand out a profile's vectors (:meth:`vector_index`, :meth:`centroid`,
:meth:`centroids_matrix`). All three backends implement it and share one read
implementation (:class:`~researcher_profiles.embeddings.flat.FlatEmbeddingIndex`),
so cosine is computed the same way everywhere. It is kept separate from
``ProfileStore`` so a backend without vectors fails an ``isinstance`` check
rather than raising from half its contract, and so the base contract stays
importable without numpy.

The cross-profile analytics ``store.centroids``, ``store.match`` and
``store.indexes`` (:mod:`researcher_profiles.analytics`) hang off the store as
accessors, the way ``prof.cite`` hangs off a profile.

:attr:`ProfileStore.root` is the store's directory, or ``None`` when it has
none. Only a short sanctioned list may branch on it; that list lives on the
:attr:`~.protocol.ProfileStore.root` property in ``store/protocol.py``, in one
copy, and is not restated here.
"""

from importlib import import_module

from .factory import build_store
from .files import FilesystemProfileStore
from .protocol import (
    DuplicateIdentityError,
    IngestResult,
    ProfileNotFoundError,
    ProfileStore,
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
    """PEP 562: import the SQL backend on first use, not on package import.

    A ``try: from .sql import ... except ImportError: pass`` at module scope
    would be the obvious thing and is wrong, because it defers nothing on a
    machine that actually has the extra: it only guards absence. Every
    ``import researcher_profiles`` on a developer box or a container with
    ``[sql]`` installed would then pay SQLAlchemy's import cost, including the
    commands that must stay light (``rp list`` over a 300-member roster
    must never touch a database, and a downstream guardrail asserts exactly
    that). Optional means optional at runtime, not merely at install time.

    A resolved name is cached into ``globals()``, so the cost is paid once and
    later accesses are ordinary attribute lookups.
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
