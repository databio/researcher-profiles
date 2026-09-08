"""The on-disk sqlite-vec index file: connect, schema, meta, vector codec.

One place for every low-level operation on ``<profile>/.cache/embeddings.sqlite``.
:mod:`store` builds and searches it, :mod:`flat` exports and rebuilds it,
:mod:`profile_vec` and :mod:`researcher_profiles.profile.topics` read vectors out of it.
Before this module, they each had their own copy of the extension-load dance
and the ``index_meta`` read, with four different answers for "the table is not
there".

Import-cheap by contract: ``sqlite_vec`` and ``numpy`` are optional-extra
dependencies and are imported inside function bodies, never at module scope, so
``researcher_profiles.embeddings.cache`` stays importable on a core-only install
(see ``tests/test_guardrails.py::TestImportCost``).
"""

import sqlite3
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .backends import MissingEmbeddingBackendError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np


class IndexNotBuiltError(RuntimeError):
    """Raised when an index is read but none exists yet at that path."""


# --------------------------------------------------------------------------
# Connection and schema
# --------------------------------------------------------------------------


def connect_vec(db_path: str | Path, *, create_parents: bool = False) -> sqlite3.Connection:
    """Open ``db_path`` with the sqlite-vec extension loaded.

    ``create_parents=True`` is the build path (``store``/``flat`` writing a new
    index); the read paths leave it False and pre-check existence themselves.
    """
    try:
        import sqlite_vec
    except ImportError as e:
        raise MissingEmbeddingBackendError(
            "sqlite-vec is required for the embeddings index. "
            "Requires the 'vectors' extra; see the install instructions "
            "in the README."
        ) from e
    db_path = Path(db_path)
    if create_parents:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def ensure_schema(conn: sqlite3.Connection, dim: int) -> None:
    """Create ``index_meta`` / ``chunks`` / ``chunk_vec`` if absent."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            text        TEXT NOT NULL,
            text_hash   TEXT NOT NULL,
            section     TEXT,
            char_count  INTEGER NOT NULL,
            indexed_at  TEXT NOT NULL,
            meta        TEXT,
            UNIQUE(source_type, source_id, chunk_index)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_type, source_id)")
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0(
            id INTEGER PRIMARY KEY,
            embedding FLOAT[{dim}]
        )
        """
    )
    conn.commit()


# --------------------------------------------------------------------------
# index_meta
# --------------------------------------------------------------------------


def read_index_meta(conn: sqlite3.Connection) -> dict[str, str]:
    """Tolerant read: ``{}`` when the ``index_meta`` table does not exist.

    For a caller that already holds a connection and has already decided the
    index exists (the build path, the flat exporter).
    """
    try:
        rows = conn.execute("SELECT key, value FROM index_meta").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {k: v for k, v in rows}


def read_index_meta_path(db_path: str | Path) -> dict[str, str]:
    """Strict read: ``IndexNotBuiltError`` when there is no readable index.

    Raises when the file is absent or has no ``index_meta`` table. An existing
    but empty table yields ``{}``. "built but unpopulated" is the caller's
    problem, not this function's. Uses a plain connection: ``index_meta`` is an
    ordinary table and reading it must not require the extension.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise IndexNotBuiltError(f"No index at {db_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT key, value FROM index_meta").fetchall()
    except sqlite3.OperationalError as e:
        raise IndexNotBuiltError(f"Index at {db_path} has no index_meta table: {e}") from e
    finally:
        conn.close()
    return {k: v for k, v in rows}


def write_index_meta(conn: sqlite3.Connection, items: Mapping[str, str]) -> None:
    """Upsert every key/value and commit."""
    for k, v in items.items():
        conn.execute(
            "INSERT INTO index_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, v),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Vector codec
# --------------------------------------------------------------------------


def serialize_vec(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def deserialize_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# --------------------------------------------------------------------------
# Bulk chunk-vector reads
# --------------------------------------------------------------------------

_CHUNK_SOURCE = "FROM chunks c JOIN chunk_vec v ON c.id = v.id ORDER BY c.id"


def _chunk_rows(db_path: Path, *, with_metadata: bool) -> list[tuple]:
    if not db_path.exists():
        raise IndexNotBuiltError(f"No index at {db_path}")
    cols = (
        "c.id, c.source_type, c.source_id, c.chunk_index, c.text, c.section, v.embedding"
        if with_metadata
        else "v.embedding"
    )
    conn = connect_vec(db_path)
    try:
        return conn.execute(f"SELECT {cols} {_CHUNK_SOURCE}").fetchall()
    finally:
        conn.close()


def _matrix(rows: list[tuple], dim: int, blob_index: int) -> "np.ndarray":
    """Decode blobs to a float32 matrix, NaN-scrubbed and L2 row-normalized."""
    import numpy as np

    mat = np.zeros((len(rows), dim), dtype=np.float32)
    for i, row in enumerate(rows):
        mat[i] = np.array(struct.unpack(f"{dim}f", row[blob_index]), dtype=np.float32)
    mat = np.nan_to_num(mat, nan=0.0, posinf=0.0, neginf=0.0)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms < 1e-12] = 1.0
    return (mat / norms).astype(np.float32)


def read_chunk_matrix(db_path: str | Path, dim: int) -> "np.ndarray":
    """Every indexed chunk vector, in ``chunks.id`` order.

    Rows are NaN-scrubbed and L2-normalized. Zero rows is not an error: the
    result is an empty ``(0, dim)`` array and the caller decides.
    """
    return _matrix(_chunk_rows(Path(db_path), with_metadata=False), dim, 0)


def read_chunk_matrix_with_meta(
    db_path: str | Path, dim: int
) -> tuple["np.ndarray", list[dict[str, Any]]]:
    """:func:`read_chunk_matrix` plus one metadata dict per row, same order."""
    rows = _chunk_rows(Path(db_path), with_metadata=True)
    metas = [
        {
            "id": r[0],
            "source_type": r[1],
            "source_id": r[2],
            "chunk_index": r[3],
            "text": r[4],
            "section": r[5],
        }
        for r in rows
    ]
    return _matrix(rows, dim, 6), metas
