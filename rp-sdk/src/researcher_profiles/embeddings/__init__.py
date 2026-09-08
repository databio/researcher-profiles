"""Utilities to create and use embeddings of researcher profile components.

An embedding turns a piece of a profile (an abstract, a summary, an expertise
statement) into a numeric vector, so the profile can be searched by meaning
rather than by keyword and any document can be ranked against it. The index
lives inside the profile directory at ``<profile>/.cache/embeddings.sqlite``,
so the profile stays self-contained and portable.

Public API
----------

- :class:`SqliteEmbeddingIndex`: per-profile sqlite-vec store
- :func:`build_index`: function-level entry point that the
  create/update pipeline can lazy-import without circular imports
- :class:`SearchHit`, :class:`IndexReport`: result dataclasses
- :class:`MissingEmbeddingBackendError`, :class:`IndexBackendMismatchError`,
  :class:`IndexNotBuiltError`

The manager ``ResearcherProfile.index`` hands back is
:class:`researcher_profiles.profile.index.IndexManager`; it lives in
``profile/`` alongside the other capability managers and imports
:func:`build_index` / :func:`_index_root` from here.

Importing this subpackage has no side effect on the profile class.
"""

from ._sqlite import IndexNotBuiltError
from .backends import (
    EmbeddingBackend,
    MissingEmbeddingBackendError,
    OpenAIBackend,
    SentenceTransformerBackend,
    VoyageBackend,
    get_backend,
)
from .build import _index_root as _index_root
from .build import build_index
from .cache import (
    IndexBackendMismatchError,
    IndexReport,
    IndexStats,
    SearchHit,
    SqliteEmbeddingIndex,
    index_backend_name,
)
from .chunking import (
    PAPER_CHUNK_TYPES,
    Chunk,
    chunk_abstract,
    chunk_cv,
    chunk_expertise,
    chunk_grant,
    chunk_soul,
    chunk_summary,
    chunk_web,
)
from .flat import (
    FlatEmbeddingIndex,
    FlatExportResult,
    FlatHit,
    public_chunk_keys,
    rebuild_sqlite_from_flat,
    slugify_backend,
    write_flat_export,
)
from .protocol import VectorHit, VectorIndex
from .rank import (
    mmr_indices,
    rank_works_against_profile,
)

__all__ = [
    "PAPER_CHUNK_TYPES",
    "Chunk",
    "EmbeddingBackend",
    "FlatEmbeddingIndex",
    "FlatExportResult",
    "FlatHit",
    "IndexBackendMismatchError",
    "IndexNotBuiltError",
    "IndexReport",
    "IndexStats",
    "MissingEmbeddingBackendError",
    "OpenAIBackend",
    "SqliteEmbeddingIndex",
    "SearchHit",
    "SentenceTransformerBackend",
    "VectorHit",
    "VectorIndex",
    "VoyageBackend",
    "build_index",
    "chunk_abstract",
    "chunk_cv",
    "chunk_expertise",
    "chunk_grant",
    "chunk_soul",
    "chunk_summary",
    "chunk_web",
    "get_backend",
    "index_backend_name",
    "mmr_indices",
    "public_chunk_keys",
    "rank_works_against_profile",
    "rebuild_sqlite_from_flat",
    "slugify_backend",
    "write_flat_export",
]
