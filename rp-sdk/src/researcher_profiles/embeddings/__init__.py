"""Utilities to create and use embeddings of researcher profile components.

The index lives at ``<profile>/.cache/embeddings.sqlite``, so the profile
stays self-contained.
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
