"""Utilities to create and use embeddings of researcher profile components.

The index lives at ``<profile>/.cache/embeddings.sqlite``, so the profile
stays self-contained.
"""

from ._sqlite import IndexNotBuiltError
from .backends import (
    EmbeddingBackend,
    EmbeddingBackendConfigError,
    EmbeddingBackendUnavailableError,
    EmbeddingModelMismatchError,
    FastEmbedBackend,
    MissingEmbeddingBackendError,
    OpenAIBackend,
    SentenceTransformerBackend,
    TEIBackend,
    TEIServerInfo,
    VoyageBackend,
    check_backend,
    encoder_for,
    get_backend,
    reset_tei_connections,
    same_space,
    stored_name_of,
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
    "EmbeddingBackendConfigError",
    "EmbeddingBackendUnavailableError",
    "EmbeddingModelMismatchError",
    "FastEmbedBackend",
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
    "TEIBackend",
    "TEIServerInfo",
    "VectorHit",
    "VectorIndex",
    "VoyageBackend",
    "build_index",
    "check_backend",
    "chunk_abstract",
    "chunk_cv",
    "chunk_expertise",
    "chunk_grant",
    "chunk_soul",
    "chunk_summary",
    "chunk_web",
    "encoder_for",
    "get_backend",
    "index_backend_name",
    "mmr_indices",
    "public_chunk_keys",
    "rank_works_against_profile",
    "rebuild_sqlite_from_flat",
    "reset_tei_connections",
    "same_space",
    "slugify_backend",
    "stored_name_of",
    "write_flat_export",
]
