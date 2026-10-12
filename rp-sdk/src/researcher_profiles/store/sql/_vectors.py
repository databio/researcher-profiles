"""The SQL store's vectors: shredding an index into rows, and serving rows back.

**Shred** (:func:`shred_profile`): a profile's built index becomes one
:class:`~researcher_profiles.store.db.ChunkVectorRow` per public chunk plus one
:class:`~researcher_profiles.store.db.ProfileVectorRow` centroid.

**Serve** (:func:`flat_bytes`, :func:`centroid`, :func:`centroids_matrix`):
rows become the published byte shapes, read by
:class:`~researcher_profiles.embeddings.flat.FlatEmbeddingIndex`.

Privacy: the shredded rows are the *public* subset, filtered exactly as
:func:`~researcher_profiles.embeddings.flat.write_flat_export` filters, because
embeddings are partially invertible (spec section 6). The centroid is computed
over the whole index including private chunks, exactly as the published
``collection/embeddings/`` blob is: one averaged vector is not invertible.

``numpy`` and ``sqlite_vec`` are imported inside functions so a core-only
install can import the SQL store (``test_guardrails.py::TestImportCost``).
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from sqlmodel import Session, select

from ...errors import CapabilityUnavailableError
from ...schema.jsonld import canonical_dumps
from ..db import ArtifactRow, ChunkVectorRow, ProfileRow, ProfileVectorRow

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

    from ...profile import ResearcherProfile

logger = logging.getLogger(__name__)

#: The published flat form's manifest address. Its stored body is the metadata
#: base :func:`flat_bytes` rebuilds from (it carries the probe, which no column
#: holds), while the numbers come from the vector rows.
FLAT_INDEX_URL = "embeddings/index.json"

#: Errors a source profile's bytes may fail with. A profile with no readable
#: index is not an error: it simply contributes no vector rows.
_SOURCE_ERRORS = (OSError, KeyError, ValueError, CapabilityUnavailableError)


# ---------------------------------------------------------------------------
# Write path: index in, rows out
# ---------------------------------------------------------------------------


def shred_profile(rid: str, profile: "ResearcherProfile") -> list:
    """Every vector row ``profile`` contributes, ready to ``session.add``.

    Returns the :class:`ChunkVectorRow` list followed by the profile's
    :class:`ProfileVectorRow` centroid, or ``[]`` when the profile ships no
    readable index. Never raises for a missing or broken index.
    """
    try:
        shredded = _from_sqlite(profile) or _from_flat(profile)
    except _SOURCE_ERRORS:
        logger.debug("no vector rows shredded for rid=%s", rid, exc_info=True)
        return []
    if shredded is None:
        return []
    chunks, centroid_vec, dim, backend_spec = shredded
    if not chunks:
        return []

    rows: list = [
        ChunkVectorRow(
            profile_rid=rid,
            ordinal=i,
            source_type=str(c["source_type"]),
            source_id=str(c["source_id"]),
            chunk_index=int(c["chunk_index"]),
            section=c.get("section"),
            char_count=c.get("char_count"),
            dim=dim,
            backend_spec=backend_spec,
            vector=c["vector"],
        )
        for i, c in enumerate(chunks)
    ]
    if centroid_vec is not None:
        rows.append(
            ProfileVectorRow(
                profile_rid=rid,
                kind="centroid",
                dim=dim,
                backend_spec=backend_spec,
                vector=_pack(centroid_vec),
            )
        )
    return rows


def _from_sqlite(profile: "ResearcherProfile"):
    """Shred the build-local ``.cache/embeddings.sqlite``, or ``None``.

    The preferred source: the index the centroid is defined over, so the
    stored centroid matches every other backend.
    """
    directory = profile.directory
    if directory is None:
        return None
    from ...utils.paths import cache_dir

    db_path = cache_dir(directory) / "embeddings.sqlite"
    if not db_path.is_file():
        return None

    from ...build_state import BuildState
    from ...embeddings._sqlite import IndexNotBuiltError
    from ...embeddings.flat import _read_rows, _servable_rows
    from ...embeddings.profile_vec import _centroid_vec

    rows, dim, backend_spec = _read_rows(db_path)
    if not backend_spec or dim <= 0:
        return None
    kept = _servable_rows(rows, profile.metadata, BuildState.load(directory))
    chunks = [
        {
            "source_type": r[0],
            "source_id": r[1],
            "chunk_index": r[2],
            "section": r[3],
            "char_count": r[4],
            "vector": bytes(r[5]),
        }
        for r in kept
    ]
    try:
        centroid_vec = _centroid_vec(db_path, dim)
    except IndexNotBuiltError:
        centroid_vec = None
    return chunks, centroid_vec, dim, str(backend_spec)


def _from_flat(profile: "ResearcherProfile"):
    """Shred the published flat form off the profile's own storage, or ``None``.

    Lossy: the flat form is the public subset, so its centroid is the mean of
    public rows only.

    Needs byte-faithful ``artifact_bytes`` (directory and SQL backends). The
    published-site client mangles a ``.bin`` and yields no rows; ingest such
    a corpus from a local copy (``install_profile``).
    """
    storage = profile.storage
    index_json = storage.artifact_bytes(FLAT_INDEX_URL)
    if not index_json:
        return None
    index = json.loads(index_json)
    name = str(index["file"])
    blob = storage.artifact_bytes(f"embeddings/{name}")
    chunks_json = storage.artifact_bytes(f"embeddings/{Path(name).stem}.chunks.json")
    if not blob or not chunks_json:
        return None

    from ...embeddings.flat import FlatEmbeddingIndex

    idx = FlatEmbeddingIndex.from_bytes(index_json, blob, chunks_json)
    dim = idx.dim
    vectors = idx.vectors
    chunks = [
        {
            "source_type": c["source_type"],
            "source_id": c["source_id"],
            "chunk_index": c["chunk_index"],
            "section": c.get("section"),
            "char_count": c.get("char_count"),
            "vector": _pack(vectors[i]),
        }
        for i, c in enumerate(idx.chunks)
    ]
    centroid_vec = idx.centroid() if chunks else None
    return chunks, centroid_vec, dim, idx.backend_spec


def _pack(vec) -> bytes:
    """A vector as row-major little-endian float32, the one on-wire form."""
    import numpy as np

    return np.asarray(vec, dtype=np.dtype("<f4")).reshape(-1).tobytes()


# ---------------------------------------------------------------------------
# Read path: rows out, published bytes in
# ---------------------------------------------------------------------------


def chunk_rows(s: Session, rid: str) -> list[ChunkVectorRow]:
    """One profile's chunk vectors in blob order."""
    return list(
        s.exec(
            select(ChunkVectorRow)
            .where(ChunkVectorRow.profile_rid == rid)
            .order_by(ChunkVectorRow.ordinal, ChunkVectorRow.id)
        ).all()
    )


def has_vectors(s: Session, rid: str) -> bool:
    """Whether ``rid`` has any chunk vector. One row, not a blob."""
    return (
        s.exec(select(ChunkVectorRow.id).where(ChunkVectorRow.profile_rid == rid).limit(1)).first()
        is not None
    )


def flat_bytes(s: Session, rid: str) -> Optional[tuple[bytes, bytes, bytes]]:
    """``(index_json, blob, chunks_json)`` for ``rid``, or ``None``.

    The stored ``embeddings/index.json`` is the metadata base when present
    (it carries the probe vector); ``dim``, ``count``, ``rows`` and ``sha256``
    are always recomputed from the rows.
    """
    rows = chunk_rows(s, rid)
    if not rows:
        return None

    from ...embeddings.flat import slugify_backend

    dim = int(rows[0].dim)
    backend_spec = rows[0].backend_spec or ""
    blob = b"".join(bytes(r.vector) for r in rows)
    expected = len(rows) * dim * 4
    if len(blob) != expected:  # pragma: no cover - a short row is a corrupt write
        raise ValueError(
            f"profile {rid}: chunk vectors total {len(blob)} bytes, expected "
            f"count*dim*4 ({expected}); a row was written at the wrong dim"
        )

    index: dict[str, Any] = {}
    stored = s.exec(
        select(ArtifactRow)
        .where(ArtifactRow.profile_rid == rid)
        .where(ArtifactRow.content_url == FLAT_INDEX_URL)
    ).first()
    if stored is not None and stored.text:
        try:
            index = dict(json.loads(stored.text))
        except ValueError:
            index = {}

    index.update(
        {
            "backend_spec": backend_spec,
            "file": f"{slugify_backend(backend_spec)}.bin",
            "dtype": "float32",
            "byte_order": "little",
            "dim": dim,
            "count": len(rows),
            "layout": "row_major",
            # Chunk vectors are stored exactly as embedded, as in the export.
            "normalized": False,
            "metric": "cosine",
            "row_key": "chunk_index",
            "rows": list(range(len(rows))),
            "sha256": hashlib.sha256(blob).hexdigest(),
        }
    )
    chunks_meta = [
        {
            "source_type": r.source_type,
            "source_id": r.source_id,
            "chunk_index": r.chunk_index,
            "section": r.section,
            "char_count": r.char_count,
        }
        for r in rows
    ]
    return (
        canonical_dumps(index).encode("utf-8"),
        blob,
        canonical_dumps(chunks_meta).encode("utf-8"),
    )


def centroid(s: Session, rid: str) -> "np.ndarray | None":
    """One profile's stored centroid, or ``None`` when it has no vectors."""
    row = s.get(ProfileVectorRow, (rid, "centroid"))
    if row is None or not row.vector:
        return None
    return _unpack(row.vector)


def centroids_matrix(s: Session) -> "tuple[list[str], np.ndarray] | None":
    """``(slugs, matrix)`` over every profile holding a centroid, or ``None``.

    One ``SELECT`` for the whole roster. Slug-ordered and L2-normalized.
    """
    import numpy as np

    rows = list(
        s.exec(
            select(ProfileRow.slug, ProfileVectorRow)
            .join(ProfileVectorRow, ProfileVectorRow.profile_rid == ProfileRow.rid)
            .where(ProfileVectorRow.kind == "centroid")
            .order_by(ProfileRow.slug)
        ).all()
    )
    kept = [(slug, vec) for slug, vec in rows if vec.vector]
    if not kept:
        return None
    dims = {int(v.dim) for _, v in kept}
    if len(dims) > 1:
        # Cosine across models is meaningless (spec section 7); a mixed store
        # has no single matrix, so the caller stacks per profile instead.
        logger.warning(
            "store holds centroids at %s different dims; no stacked matrix",
            sorted(dims),
        )
        return None
    mat = np.vstack([_unpack(v.vector) for _, v in kept]).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms < 1e-12] = 1.0
    return [slug for slug, _ in kept], (mat / norms).astype(np.float32)


def backend_spec(s: Session) -> Optional[str]:
    """The embedding space this store's vectors live in, or ``None``.

    First readable name, not a consensus.
    """
    row = s.exec(
        select(ChunkVectorRow.backend_spec).where(ChunkVectorRow.backend_spec != "").limit(1)
    ).first()
    return str(row) if row else None


def _unpack(blob: bytes) -> "np.ndarray":
    import numpy as np

    return np.frombuffer(bytes(blob), dtype=np.dtype("<f4")).astype(np.float32)
