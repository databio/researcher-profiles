"""The public form of a profile's embeddings: the private sqlite index projected to portable files, with restricted rows dropped.

``.cache/embeddings.sqlite`` is a build-local index at tier ``restricted``; it
never leaves the machine. The servable form lives in the profile as public
artifacts (see ``docs/rp-spec/embeddings.md``)::

    embeddings/
      index.json               # one-model index: dims, count, sha256, row manifest
      <backend>.bin            # raw little-endian float32, row-major, headerless
      <backend>.chunks.json    # one metadata entry per row, blob order, no text

The exporter reads the sqlite and writes those three files, dropping every row
whose effective privacy tier exceeds ``public`` through the general derivation
rule (:func:`researcher_profiles.privacy.chunk_source_tiers` /
:func:`researcher_profiles.privacy.drop_above_public`). There is no
``PUBLISHED_SOURCE_TYPES`` allowlist. Embeddings are partially invertible, so a
row built from a ``restricted`` source (a grant, a CV, a scraped web page) must
not appear in a public ``.bin`` (spec section 6).

The readers reconstruct a usable index from the flat form: :class:`FlatEmbeddingIndex`
for an external consumer that has ``embeddings/`` but no ``meta/`` (pure numpy,
no sqlite), and :func:`rebuild_sqlite_from_flat` as an offline optimization that
rebuilds ``.cache/embeddings.sqlite`` without re-embedding.
"""

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from ..build_state import BuildState
from ..privacy import chunk_source_tiers, drop_above_public
from ..schema import ProfileDocument
from ..schema.jsonld import canonical_dumps
from ..utils.clock import now_iso
from ..utils.const import EMBEDDING_PROBE_TEXT
from ..utils.paths import cache_dir

if TYPE_CHECKING:  # pragma: no cover
    from .backends import EmbeddingBackend
from ._sqlite import connect_vec, ensure_schema, read_index_meta, serialize_vec, write_index_meta

logger = logging.getLogger(__name__)

# Little-endian float32: the one and only on-wire element type (spec section 4).
_F32_LE = np.dtype("<f4")


# ---------------------------------------------------------------------------
# Result / hit dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FlatExportResult:
    """Outcome of :func:`write_flat_export`, for logging and tests."""

    count: int
    dim: int
    backend_spec: str
    sha256: str
    dropped: int
    index_path: Path
    blob_path: Path
    chunks_path: Path


@dataclass
class FlatHit:
    """One search result from :class:`FlatEmbeddingIndex`.

    Carries no text: a consumer resolves display text by fetching the source
    document named by ``(source_type, source_id)`` through the profile manifest
    (spec section 5).
    """

    source_type: str
    source_id: str
    chunk_index: int
    section: str | None
    cosine: float
    score: float


# ---------------------------------------------------------------------------
# Backend filename sanitization
# ---------------------------------------------------------------------------


def slugify_backend(backend_spec: str) -> str:
    """A filesystem/URL-safe slug of a backend name.

    A raw model name like ``st:all-MiniLM-L6-v2`` contains a colon, so it cannot
    be a filename directly. Lowercase and map ``:`` and ``/`` to ``-``. The real
    filename is always recorded in ``index.json``'s ``file`` field; consumers
    resolve by that field, never by reconstructing this slug (spec section 5).
    """
    slug = backend_spec.lower()
    for ch in (":", "/", "\\", " "):
        slug = slug.replace(ch, "-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


# ---------------------------------------------------------------------------
# sqlite reading + privacy filtering
# ---------------------------------------------------------------------------


def _load_profile_document(profile_dir: Path) -> ProfileDocument | None:
    """Parse ``profile.jsonld`` for tier resolution; ``None`` if unavailable.

    Mirrors ``SqliteEmbeddingIndex._level``: the CLI entry point does not hand us a
    loaded document, so fall back to a direct read. When it cannot be parsed the
    profile default is treated as ``public`` and only the role defaults filter.
    """
    path = profile_dir / "profile.jsonld"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ProfileDocument.model_validate(data)
    except (OSError, ValueError):
        return None


def _read_rows(db_path: Path) -> tuple[list[tuple], int, str]:
    """Return ``(rows, dim, backend_spec)`` from the sqlite index.

    Each row is ``(source_type, source_id, chunk_index, section, char_count,
    embedding_blob)`` in the stable blob order ``source_type, source_id,
    chunk_index``.

    A corrupt or non-database file (e.g. a placeholder) yields
    ``([], 0, "")`` rather than raising, so a render over a junk index
    produces no flat form instead of aborting.
    """
    try:
        conn = connect_vec(db_path)
    except sqlite3.Error:
        return [], 0, ""
    try:
        meta = read_index_meta(conn)
        backend_spec = meta.get("backend_name", "")
        dim = int(meta.get("embedding_dim", "0") or 0)
        rows = conn.execute(
            "SELECT c.source_type, c.source_id, c.chunk_index, c.section, "
            "c.char_count, v.embedding "
            "FROM chunks c JOIN chunk_vec v ON c.id = v.id "
            "ORDER BY c.source_type, c.source_id, c.chunk_index"
        ).fetchall()
    except sqlite3.Error:
        return [], 0, ""
    finally:
        conn.close()
    return rows, dim, backend_spec


def _public_rows(
    rows: list[tuple],
    profile_document: ProfileDocument | None,
    build_state: BuildState,
) -> list[tuple]:
    """Filter sqlite rows to those safe to publish, in blob order.

    Two independent gates:

    1. Tier: drop any row whose effective tier exceeds ``public``, via the
       general derivation rule (never an allowlist). Restricted rows never reach
       the blob or the chunks file.
    2. Contamination: drop ``paper_summary`` / ``paper_abstract`` rows for
       contaminated papers. Contamination is build bookkeeping, orthogonal to
       tier, and contaminated papers must never be published.
    """
    tiers = chunk_source_tiers(profile_document, [(r[0], r[1]) for r in rows])
    public_idx = set(drop_above_public((i, tiers[(r[0], r[1])]) for i, r in enumerate(rows)))
    kept: list[tuple] = []
    for i, r in enumerate(rows):
        if i not in public_idx:
            continue
        source_type, source_id = r[0], r[1]
        if source_type in ("paper_summary", "paper_abstract") and build_state.is_contaminated(
            source_id
        ):
            continue
        kept.append(r)
    return kept


def public_chunk_keys(
    profile_dir: str | Path, *, profile_document: ProfileDocument | None = None
) -> list[tuple[str, str, int]]:
    """The ``(source_type, source_id, chunk_index)`` of rows the export keeps.

    Used by the build postcondition to know how many rows the served form must
    carry, without re-writing the files. Returns an empty list when there is no
    sqlite index.
    """
    profile_dir = Path(profile_dir)
    db_path = cache_dir(profile_dir) / "embeddings.sqlite"
    if not db_path.is_file():
        return []
    rows, _dim, _backend = _read_rows(db_path)
    if profile_document is None:
        profile_document = _load_profile_document(profile_dir)
    kept = _public_rows(rows, profile_document, BuildState.load(profile_dir))
    return [(r[0], r[1], r[2]) for r in kept]


# ---------------------------------------------------------------------------
# The exporter
# ---------------------------------------------------------------------------


def _remove_flat_files(embeddings_dir: Path) -> None:
    """Delete any prior flat form so an all-restricted profile stops advertising it."""
    if not embeddings_dir.is_dir():
        return
    for name in ("index.json",):
        (embeddings_dir / name).unlink(missing_ok=True)
    for f in embeddings_dir.glob("*.bin"):
        f.unlink(missing_ok=True)
    for f in embeddings_dir.glob("*.chunks.json"):
        f.unlink(missing_ok=True)


def write_flat_export(
    profile_dir: str | Path,
    *,
    profile_document: ProfileDocument | None = None,
    backend: "EmbeddingBackend | None" = None,
) -> FlatExportResult | None:
    """Write the flat servable form from ``.cache/embeddings.sqlite``.

    sqlite in, public flat form out. Returns a :class:`FlatExportResult`, or
    ``None`` when there is no sqlite index, no backend metadata, or no public
    row survives filtering (in which case any stale flat form is removed and
    ``hasEmbeddingIndex`` stays false).
    """
    profile_dir = Path(profile_dir)
    db_path = cache_dir(profile_dir) / "embeddings.sqlite"
    embeddings_dir = profile_dir / "embeddings"
    if not db_path.is_file():
        return None

    rows, dim, backend_spec = _read_rows(db_path)
    if not backend_spec or dim <= 0:
        return None

    # Read probe from the sqlite meta; generate on the fly if absent.
    try:
        conn = connect_vec(db_path)
        meta = read_index_meta(conn)
        conn.close()
    except sqlite3.Error:
        meta = {}
    probe_text = meta.get("probe_text")
    probe_vector_raw = meta.get("probe_vector")
    if probe_text and probe_vector_raw:
        probe_vector = json.loads(probe_vector_raw)
    elif backend is not None:
        probe_text = EMBEDDING_PROBE_TEXT
        probe_vector = [float(v) for v in backend.embed([probe_text])[0]]
        try:
            conn = connect_vec(db_path)
            write_index_meta(
                conn, {"probe_text": probe_text, "probe_vector": json.dumps(probe_vector)}
            )
            conn.close()
        except sqlite3.Error:
            # The probe is already computed and goes into index.json regardless;
            # failing to cache it back only costs the next export an embed call.
            logger.debug("could not cache the probe vector in %s", db_path, exc_info=True)
    else:
        raise RuntimeError(
            "embeddings/index.json cannot be written without a probe; "
            "rebuild the index (rp index --force) or pass backend="
        )

    if profile_document is None:
        profile_document = _load_profile_document(profile_dir)

    kept = _public_rows(rows, profile_document, BuildState.load(profile_dir))
    dropped = len(rows) - len(kept)

    if not kept:
        _remove_flat_files(embeddings_dir)
        return None

    # Blob: float32 (count, dim), row-major, little-endian, headerless.
    mat = np.zeros((len(kept), dim), dtype=_F32_LE)
    for i, r in enumerate(kept):
        vec = np.frombuffer(r[5], dtype=_F32_LE)
        if vec.shape[0] != dim:
            raise ValueError(f"row {i} vector has {vec.shape[0]} floats, expected dim={dim}")
        mat[i] = vec
    blob = mat.tobytes()
    assert len(blob) == len(kept) * dim * 4, (
        f"blob length {len(blob)} != count*dim*4 ({len(kept) * dim * 4})"
    )
    sha256 = hashlib.sha256(blob).hexdigest()

    slug = slugify_backend(backend_spec)
    blob_name = f"{slug}.bin"
    chunks_name = f"{slug}.chunks.json"

    index = {
        "backend_spec": backend_spec,
        "file": blob_name,
        "dtype": "float32",
        "byte_order": "little",
        "dim": dim,
        "count": len(kept),
        "layout": "row_major",
        "normalized": False,  # per-profile chunk vectors are stored as embedded
        "metric": "cosine",
        "row_key": "chunk_index",
        "rows": list(range(len(kept))),
        "sha256": sha256,
        "probe": {"text": probe_text, "vector": probe_vector},
    }
    chunks_meta: list[dict[str, Any]] = [
        {
            "source_type": r[0],
            "source_id": r[1],
            "chunk_index": r[2],
            "section": r[3],
            "char_count": r[4],
        }
        for r in kept
    ]

    embeddings_dir.mkdir(parents=True, exist_ok=True)
    # Overwrite any stale slug files first (a backend change renames the blob).
    _remove_flat_files(embeddings_dir)
    blob_path = embeddings_dir / blob_name
    chunks_path = embeddings_dir / chunks_name
    index_path = embeddings_dir / "index.json"
    blob_path.write_bytes(blob)
    chunks_path.write_text(canonical_dumps(chunks_meta), encoding="utf-8")
    index_path.write_text(canonical_dumps(index), encoding="utf-8")

    return FlatExportResult(
        count=len(kept),
        dim=dim,
        backend_spec=backend_spec,
        sha256=sha256,
        dropped=dropped,
        index_path=index_path,
        blob_path=blob_path,
        chunks_path=chunks_path,
    )


# ---------------------------------------------------------------------------
# Reader for external consumers (no sqlite)
# ---------------------------------------------------------------------------


class FlatEmbeddingIndex:
    """Read the flat form back into a searchable index: pure numpy, no sqlite.

    A profile downloaded from the web has ``embeddings/`` but no ``meta/``. This
    reconstructs a usable index from the three public files and verifies the
    blob against the index (spec section 4).
    """

    def __init__(
        self,
        index: dict[str, Any],
        vectors: np.ndarray,
        chunks: list[dict[str, Any]],
        *,
        backend: "EmbeddingBackend | None" = None,
    ):
        self._index = index
        self._vectors = vectors
        self._chunks = chunks
        # Bound at construction by a caller that already holds one (a store
        # serving many profiles builds it once); otherwise resolved from
        # ``backend_spec`` on the first text query and kept.
        self._backend = backend

    @classmethod
    def from_bytes(
        cls,
        index_json: bytes,
        blob: bytes,
        chunks_json: bytes,
        *,
        backend: "EmbeddingBackend | None" = None,
    ) -> "FlatEmbeddingIndex":
        """Build an index from the three published files' bytes.

        The verification (declared length, declared sha256) is here rather than
        in :meth:`load` because it is a property of the *bytes*, not of where
        they came from. A store that fetched them over HTTP gets the same
        tamper check a directory read does, which is the point: the flat form
        is designed to be served, and a served blob is the one most worth
        checking.
        """
        index = json.loads(index_json)
        dim = int(index["dim"])
        count = int(index["count"])
        name = str(index["file"])

        expected = count * dim * 4
        if len(blob) != expected:
            raise ValueError(
                f"{name}: byte length {len(blob)} != count*dim*4 "
                f"({expected}); corrupt or truncated blob"
            )
        declared_sha = index.get("sha256")
        if declared_sha:
            actual_sha = hashlib.sha256(blob).hexdigest()
            if actual_sha != declared_sha:
                raise ValueError(
                    f"{name}: sha256 mismatch (index says {declared_sha}, "
                    f"blob is {actual_sha}); tampered or corrupt blob"
                )
        vectors = np.frombuffer(blob, dtype=_F32_LE).reshape(count, dim)
        chunks = json.loads(chunks_json)
        return cls(index, vectors, chunks, backend=backend)

    @classmethod
    def load(
        cls,
        embeddings_dir: str | Path,
        *,
        backend: "EmbeddingBackend | None" = None,
    ) -> "FlatEmbeddingIndex":
        """Read the three published files from a directory. See :meth:`from_bytes`."""
        embeddings_dir = Path(embeddings_dir)
        index_json = (embeddings_dir / "index.json").read_bytes()
        index = json.loads(index_json)
        blob = (embeddings_dir / index["file"]).read_bytes()
        chunks_name = Path(index["file"]).stem + ".chunks.json"
        chunks_json = (embeddings_dir / chunks_name).read_bytes()
        return cls.from_bytes(index_json, blob, chunks_json, backend=backend)

    # --- properties ------------------------------------------------------

    @property
    def backend_spec(self) -> str:
        return str(self._index["backend_spec"])

    @property
    def dim(self) -> int:
        return int(self._index["dim"])

    @property
    def count(self) -> int:
        return int(self._index["count"])

    @property
    def normalized(self) -> bool:
        return bool(self._index["normalized"])

    @property
    def metric(self) -> str:
        return str(self._index["metric"])

    @property
    def rows(self) -> list:
        return list(self._index["rows"])

    @property
    def vectors(self) -> np.ndarray:
        return self._vectors

    @property
    def chunks(self) -> list[dict[str, Any]]:
        return self._chunks

    # --- search ----------------------------------------------------------

    def search_vector(self, query_vec, k: int = 5) -> list[FlatHit]:
        """Cosine top-``k`` over the stored vectors, most similar first.

        The vector-level primitive. Rows are normalized on the fly when
        ``normalized`` is false (spec section 4). Returns :class:`FlatHit`s
        carrying no text.
        """
        q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        if q.shape[0] != self.dim:
            raise ValueError(f"query has {q.shape[0]} dims, index is {self.dim}-dim")
        if self._vectors.shape[0] == 0:
            return []
        mat = self._unit_rows()
        qn = float(np.linalg.norm(q))
        if qn:
            q = q / qn
        scores = mat @ q
        order = np.argsort(-scores)[: max(0, k)]
        hits: list[FlatHit] = []
        for i in order:
            i = int(i)
            c = self._chunks[i]
            cosine = float(scores[i])
            hits.append(
                FlatHit(
                    source_type=c["source_type"],
                    source_id=c["source_id"],
                    chunk_index=c["chunk_index"],
                    section=c.get("section"),
                    cosine=round(cosine, 4),
                    score=round(max(0.0, 0.5 * (1.0 + cosine)), 4),
                )
            )
        return hits

    def _unit_rows(self) -> np.ndarray:
        """The stored matrix with unit-length rows."""
        mat = self._vectors.astype(np.float32)
        if self.normalized:
            return mat
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return mat / norms

    def search(self, query: str, k: int = 5) -> list[FlatHit]:
        """Embed ``query`` with this index's own backend, then search.

        The :class:`~researcher_profiles.embeddings.protocol.VectorIndex`
        entry point: text in, hits out, with the backend resolved from
        ``backend_spec`` so a caller cannot accidentally compare across models.
        """
        return self.search_text(query, self.backend, k=k)

    @property
    def backend(self) -> "EmbeddingBackend":
        """The backend this index's vectors came out of, constructed on demand."""
        if self._backend is None:
            from .backends import get_backend

            self._backend = get_backend(self.backend_spec)
        return self._backend

    def search_text(self, query: str, backend, k: int = 5) -> list[FlatHit]:
        """Embed ``query`` with an explicitly given ``backend`` then search.

        Refuses when ``backend.name != backend_spec``. Cosine similarity across
        embedding models is meaningless (spec section 7).
        """
        if getattr(backend, "name", None) != self.backend_spec:
            raise ValueError(
                f"backend {getattr(backend, 'name', None)!r} does not match "
                f"index backend_spec {self.backend_spec!r}; refusing cross-model "
                "similarity"
            )
        if not query or not query.strip():
            return []
        vec = backend.embed([query])[0]
        return self.search_vector(vec, k=k)

    # --- the profile-level vector ----------------------------------------

    def centroid(self) -> np.ndarray:
        """The L2-normalized mean of every row.

        The same quantity ``.cache/embeddings.sqlite`` yields, computed the same
        way (mean of the stored vectors, then normalize) so a centroid read
        through this index is comparable to one read through the sqlite. It is
        *not* the same number when the sqlite held restricted rows: those are
        dropped at export by design, so this is the public subset's centroid.
        """
        from ._sqlite import IndexNotBuiltError

        if self._vectors.shape[0] == 0:
            raise IndexNotBuiltError("flat index has no vectors")
        mean = self._vectors.astype(np.float32).mean(axis=0)
        n = float(np.linalg.norm(mean))
        if n < 1e-12:
            return mean.astype(np.float32)
        return (mean / n).astype(np.float32)


# ---------------------------------------------------------------------------
# Local sqlite rebuild (offline optimization)
# ---------------------------------------------------------------------------


def rebuild_sqlite_from_flat(profile_dir: str | Path, *, backend=None):
    """Rebuild ``.cache/embeddings.sqlite`` from the flat form without re-embedding.

    The flat form is a lossy public projection (no text, public rows only), so
    the *authoritative* rebuild is still ``build_index`` (re-chunk + re-embed).
    This is an offline optimization for a host where the embedding model cannot
    run: it re-chunks the on-disk source docs to recover chunk ``text`` (the
    sqlite needs it to display search hits) and loads the *vectors* from the
    ``.bin`` by matching ``(source_type, source_id, chunk_index)``.

    Rows present in sources but absent from the flat form (restricted chunks the
    public export dropped) are re-embedded when ``backend`` is given, else
    skipped, so a flat-only rebuild yields a public-subset index.
    """
    from .cache import IndexReport, SqliteEmbeddingIndex, _sha256

    profile_dir = Path(profile_dir)
    flat = FlatEmbeddingIndex.load(profile_dir / "embeddings")
    dim = flat.dim
    backend_name = flat.backend_spec

    idx = SqliteEmbeddingIndex(profile_dir)
    chunks = idx._enumerate_chunks()

    vec_by_key: dict[tuple[str, str, int], np.ndarray] = {}
    for i, c in enumerate(flat.chunks):
        vec_by_key[(c["source_type"], c["source_id"], c["chunk_index"])] = flat.vectors[i]

    if idx.db_path.exists():
        idx.db_path.unlink()

    conn = connect_vec(idx.db_path, create_parents=True)
    report = IndexReport(backend_name=backend_name)
    try:
        ensure_schema(conn, dim=dim)
        write_index_meta(
            conn,
            {
                "schema_version": SqliteEmbeddingIndex.SCHEMA_VERSION,
                "backend_name": backend_name,
                "embedding_dim": str(dim),
                "created_at": now_iso(),
            },
        )
        now = now_iso()
        for ch in chunks:
            key = (ch.source_type, ch.source_id, ch.chunk_index)
            vec = vec_by_key.get(key)
            if vec is None:
                if backend is None:
                    report.skipped += 1
                    continue
                vec = np.asarray(backend.embed([ch.embed_text])[0], dtype=np.float32)
            report.added += 1
            cur = conn.execute(
                """
                INSERT INTO chunks (
                    source_type, source_id, chunk_index, text, text_hash,
                    section, char_count, indexed_at, meta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ch.source_type,
                    ch.source_id,
                    ch.chunk_index,
                    ch.text,
                    _sha256(ch.embed_text),
                    ch.section,
                    len(ch.text),
                    now,
                    "",
                ),
            )
            conn.execute(
                "INSERT INTO chunk_vec (id, embedding) VALUES (?, ?)",
                (cur.lastrowid, serialize_vec(np.asarray(vec, dtype=np.float32).tolist())),
            )
        conn.commit()
        write_index_meta(conn, {"last_built_at": now_iso()})
    finally:
        conn.close()
    return report


__all__ = [
    "FlatEmbeddingIndex",
    "FlatExportResult",
    "FlatHit",
    "public_chunk_keys",
    "rebuild_sqlite_from_flat",
    "slugify_backend",
    "write_flat_export",
]
