"""The collection-wide embedding files: every profile's centroid stacked into one blob, the collection bundle, and the topics index."""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..errors import ProfileError
from ..profile import ResearcherProfile
from ..schema.jsonld import canonical_dumps

if TYPE_CHECKING:  # pragma: no cover
    from ._site import SiteResult

logger = logging.getLogger(__name__)

_Writer = Callable[[str, str | bytes], None]


def _collect_centroid(
    prof_dir: Path,
    prof: ResearcherProfile,
    entries: list[tuple[str, str, Any, dict | None]],
    result: "SiteResult",
) -> None:
    """Append ``(slug, backend_spec, centroid, probe)`` when the profile is Searchable.

    A profile is Searchable when it ships ``embeddings/index.json``. The
    centroid is computed from the local sqlite (all chunks, including restricted
    sources, since a single averaged vector is not invertible, spec §6), so this is
    a no-op wrapped in a guard when the sqlite is absent or unreadable.
    """
    index_json = prof_dir / "embeddings" / "index.json"
    if not index_json.is_file():
        return
    # Imported here, not at module scope: ``..embeddings`` pulls numpy, and
    # publishing a site of non-searchable profiles must not need the vectors
    # extra. Past this point the profile is searchable, so it does.
    from ..embeddings import IndexNotBuiltError, MissingEmbeddingBackendError

    try:
        import json as _json

        idx_data = _json.loads(index_json.read_text(encoding="utf-8"))
        backend_spec = idx_data["backend_spec"]
    except (OSError, ValueError, KeyError) as e:
        result.warnings.append(f"collection centroid skipped for {prof.slug}: {e}")
        return
    probe = idx_data.get("probe")
    try:
        vec = prof.index.embedding("centroid")
    except (IndexNotBuiltError, MissingEmbeddingBackendError, OSError) as e:
        result.warnings.append(f"collection centroid skipped for {prof.slug}: {e}")
        return
    entries.append((prof.slug, str(backend_spec), vec, probe))


def _write_collection_centroids(
    entries: list[tuple[str, str, Any, dict | None]],
    write: _Writer,
    result: "SiteResult",
) -> dict[str, Any] | None:
    """Write ``collection/embeddings/`` from collected per-profile centroids.

    All centroids in one blob must share a ``backend_spec`` (spec §8), so group
    by backend, keep the majority, and warn+skip the minority. Rows are the
    slugs in blob order; the matrix is L2-normalized (``normalized: true``).

    Returns metadata dict for the collection bundle, or None.
    """
    if not entries:
        return None
    import hashlib

    from ..embeddings.flat import slugify_backend

    grouped = _majority_backend_centroids(entries, result)
    if grouped is None:
        return None
    majority, kept, probes = grouped
    blob, dim = _stack_centroids(kept, result)
    backend_file = f"{slugify_backend(majority)}.bin"
    collection_probe = _collection_probe(kept, probes, result)

    index: dict[str, Any] = {
        "backend_spec": majority,
        "file": backend_file,
        "dtype": "float32",
        "byte_order": "little",
        "dim": dim,
        "count": len(kept),
        "layout": "row_major",
        "normalized": True,
        "metric": "cosine",
        "row_key": "slug",
        "rows": [slug for slug, _ in kept],
        "sha256": hashlib.sha256(blob).hexdigest(),
    }
    if collection_probe:
        index["probe"] = collection_probe

    write(f"collection/embeddings/{backend_file}", blob)
    write("collection/embeddings/index.json", canonical_dumps(index))

    return {
        "backend_spec": majority,
        "dim": dim,
        "count": len(kept),
        "backend_file": backend_file,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "blob_bytes": len(blob),
    }


def _majority_backend_centroids(
    entries: list[tuple[str, str, Any, dict | None]],
    result: "SiteResult",
) -> tuple[str, list[tuple[str, Any]], list[dict | None]] | None:
    """Keep the centroids on the majority ``backend_spec``, sorted by slug.

    Returns ``(majority, kept, probes)`` with ``kept`` as ``(slug, float32
    vector)`` pairs and ``probes`` parallel to it, or None when nothing survives.
    """
    from collections import Counter

    import numpy as np

    counts = Counter(backend for _, backend, _, _probe in entries)
    majority = counts.most_common(1)[0][0]
    kept = []
    probes: list[dict | None] = []
    for slug, backend, vec, probe in entries:
        if backend != majority:
            result.warnings.append(
                f"collection centroid: {slug} uses backend {backend!r}, not the "
                f"majority {majority!r}; skipped to avoid cross-model mixing"
            )
            continue
        kept.append((slug, np.asarray(vec, dtype=np.float32).reshape(-1)))
        probes.append(probe)
    if not kept:
        return None

    # Sort by slug for deterministic blob order.
    order = sorted(range(len(kept)), key=lambda i: kept[i][0])
    return majority, [kept[i] for i in order], [probes[i] for i in order]


def _stack_centroids(kept: list[tuple[str, Any]], result: "SiteResult") -> tuple[bytes, int]:
    """Row-stack the kept vectors into one little-endian float32 blob.

    Returns ``(blob, dim)``; a vector whose dim disagrees with the first row
    is warned about and left as a zero row.
    """
    import numpy as np

    dim = int(kept[0][1].shape[0])
    mat = np.zeros((len(kept), dim), dtype=np.dtype("<f4"))
    for i, (_slug, v) in enumerate(kept):
        if v.shape[0] != dim:
            result.warnings.append(
                f"collection centroid: {_slug} has dim {v.shape[0]} != {dim}; skipped"
            )
            continue
        mat[i] = v
    return mat.tobytes(), dim


def _collection_probe(
    kept: list[tuple[str, Any]],
    probes: list[dict | None],
    result: "SiteResult",
) -> dict | None:
    """Resolve the collection-level probe: all kept profiles should agree."""
    import numpy as np

    collection_probe = None
    for p in probes:
        if p and isinstance(p, dict) and p.get("text") and p.get("vector"):
            collection_probe = p
            break
    if collection_probe:
        ref_vec = np.asarray(collection_probe["vector"], dtype=np.float32)
        for i, p in enumerate(probes):
            if not p or not p.get("vector"):
                continue
            pv = np.asarray(p["vector"], dtype=np.float32)
            if pv.shape != ref_vec.shape or not np.allclose(ref_vec, pv, atol=1e-6):
                result.warnings.append(
                    f"collection centroid: {kept[i][0]} probe disagrees with majority; skipped"
                )
    return collection_probe


def _write_collection_bundle(
    profile_summaries: list[dict[str, Any]],
    centroid_meta: dict[str, Any] | None,
    write: _Writer,
    result: "SiteResult",  # noqa: ARG001
    timestamp: str,
    base_url: str | None,
) -> None:
    """Write ``collection.jsonld``: the single document describing the collection."""
    cards = []
    for ps in profile_summaries:
        slug = ps["slug"]
        cards.append(
            {
                "slug": slug,
                "rid": ps.get("rid"),
                "name": ps["name"],
                "level": ps.get("level", "full"),
                "affiliation": ps.get("affiliation"),
                "field": ps.get("field"),
                "paper_count": ps.get("paper_count", 0),
                "summary_count": ps.get("summary_count", 0),
                "fulltext_pct": ps.get("fulltext_pct", 0.0),
                "base": f"profiles/{slug}/",
            }
        )

    artifacts: list[dict[str, Any]] = []
    if centroid_meta:
        artifacts.append(
            {
                "rel": "centroids",
                "href": f"collection/embeddings/{centroid_meta['backend_file']}",
                "media_type": "application/octet-stream",
                "bytes": centroid_meta["blob_bytes"],
                "sha256": centroid_meta["sha256"],
            }
        )
        artifacts.append(
            {
                "rel": "embedding_index",
                "href": "collection/embeddings/index.json",
                "media_type": "application/json",
            }
        )

    bundle = {
        "@context": "https://profiles.databio.org/context/v1.jsonld",
        "@id": base_url.rstrip("/") + "/collection.jsonld" if base_url else "collection.jsonld",
        "generated_at": timestamp,
        "generator": "rp-sdk",
        "backend_spec": centroid_meta["backend_spec"] if centroid_meta else None,
        "dim": centroid_meta["dim"] if centroid_meta else None,
        "count": len(cards),
        "cards": cards,
        "artifacts": artifacts,
    }
    write("collection.jsonld", canonical_dumps(bundle))


def _write_topics_index(
    root: Path,
    slugs: list[str],
    write: _Writer,
    result: "SiteResult",  # noqa: ARG001
    timestamp: str,
) -> None:
    """Write ``collection/topics.json`` from per-profile topic labels."""
    from ..embeddings import IndexNotBuiltError

    index: dict[str, list[str]] = {}
    for slug in slugs:
        entry = root / slug
        try:
            prof = ResearcherProfile.from_files(entry)
            topics = prof.topics.get(method="cached")
        except (OSError, ProfileError, ValidationError, IndexNotBuiltError) as e:
            logger.debug("topics index skipped for %s: %s", slug, e)
            continue
        for t in topics:
            key = (t.label or "").lower().strip()
            if not key:
                continue
            base = f"profiles/{slug}/"
            index.setdefault(key, []).append(base)

    if not index:
        return

    doc = {
        "version": 1,
        "computed_at": timestamp,
        "index": index,
    }
    write("collection/topics.json", canonical_dumps(doc))
