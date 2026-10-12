"""The store's centroid matrix, its on-disk cache, and the query backend.

One L2-normalized row per profile, slug-ordered, so ``matrix @ query`` is the
cosine against every profile at once.

``<root>/.cache/centroids.npz`` is an optional write-through memo (directory
stores only). In process, a write bumps the store's generation, which drops
the memoized matrix. Across processes, the ``.npz`` is checked against the
current slug list and each profile's index mtime before it is trusted.
"""

import logging
import os
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

from ..embeddings.backends import get_backend
from ..embeddings.cache import SqliteEmbeddingIndex
from ..embeddings.profile_vec import _resolve_backend
from ..store._analytics import require_vector_store
from ..utils.clock import now_iso

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..store.protocol import VectorStore
    from .roster import _Roster, _RosterCache

logger = logging.getLogger(__name__)


class CentroidManager:
    """``store.centroids``, the centroid matrix and the query embedder.

    Kept together because a query vector is only comparable to the matrix when
    it comes from the backend the indexes were built with.
    """

    def __init__(self, store: "VectorStore", rostered: "_RosterCache") -> None:
        self._store = store
        self._rostered = rostered
        self._matrix: np.ndarray | None = None
        #: The roster generation ``_matrix`` was computed at. Not a slug list:
        #: a rename leaves the slugs identical and the rows wrong.
        self._matrix_generation: int | None = None
        self._backend = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CentroidManager(store={self._store.location!r})"

    # ------------------------------------------------------------------
    # The matrix
    # ------------------------------------------------------------------

    @property
    def cache_path(self) -> Optional[Path]:
        """``<root>/.cache/centroids.npz``, or ``None`` without a root."""
        from ..utils.paths import store_cache_dir

        d = store_cache_dir(self._store.root)
        return None if d is None else d / "centroids.npz"

    @property
    def matrix(self) -> np.ndarray:
        """L2-normalized centroids, one row per profile, slug-ordered."""
        return self.snapshot()[1]

    def snapshot(self) -> "tuple[_Roster, np.ndarray]":
        """``(roster, matrix)``, guaranteed row-aligned.

        Row ``i`` is ``roster.slugs[i]``. Fetching the two separately could
        pair a roster with a matrix rebuilt after a write in between.
        """
        roster = self._rostered()
        if self._matrix is not None and self._matrix_generation == roster.generation:
            return roster, self._matrix
        cached = self._load_cache(roster)
        if cached is not None:
            return roster, cached
        return roster, self._compute(roster)

    def invalidate(self) -> None:
        """Drop the in-memory matrix and unlink the ``.npz``."""
        self._matrix = None
        self._matrix_generation = None
        cp = self.cache_path
        if cp is not None and cp.exists():
            try:
                cp.unlink()
            except OSError:
                logger.debug("could not unlink centroid cache at %s", cp, exc_info=True)

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _memoize(self, roster: "_Roster", mat: np.ndarray) -> None:
        self._matrix = mat
        self._matrix_generation = roster.generation

    def _cache_is_fresh(self, roster: "_Roster", cache_mtime: float) -> bool:
        """True when no profile's embedding index is newer than the cache.

        A profile with no index makes the cache stale, so a store holding an
        unbuilt profile recomputes on every load.
        """
        root = roster.root
        if root is None:  # unreachable: there is no cache file without a root
            return False
        for p in roster.profiles:
            mtime = SqliteEmbeddingIndex(root / p.slug, profile_document=p.metadata).mtime()
            if mtime is None or mtime > cache_mtime:
                return False
        return True

    def _load_cache(self, roster: "_Roster") -> np.ndarray | None:
        """The cached matrix, or ``None`` when there is no usable one."""
        cache_path = self.cache_path
        if cache_path is None or not cache_path.exists():
            return None
        try:
            if not self._cache_is_fresh(roster, cache_path.stat().st_mtime):
                return None
            with np.load(str(cache_path), allow_pickle=True) as data:
                cached_slugs = [str(s) for s in list(data["slugs"])]
                if cached_slugs != roster.slugs:
                    return None
                vectors = np.array(data["vectors"], dtype=np.float32)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            # A bad cache degrades to a recompute; it must never break a load.
            logger.debug("centroid cache at %s unusable; recomputing", cache_path, exc_info=True)
            return None
        self._memoize(roster, vectors)
        return vectors

    def _compute(self, roster: "_Roster") -> np.ndarray:
        """Read every profile's centroid through the store, normalize, memoize, persist.

        A profile with no vectors gets a zero row (logged at WARNING) so rows
        stay aligned with the roster; a zero row never ranks.
        """
        from ..embeddings import IndexNotBuiltError

        store = require_vector_store(self._store)
        published = self._published_rows(store)

        vecs: list[np.ndarray | None] = []
        for p in roster.profiles:
            row = published.get(p.slug) if published is not None else None
            if row is not None:
                vecs.append(row)
                continue
            try:
                vecs.append(np.asarray(store.centroid(p.slug), dtype=np.float32))
            except IndexNotBuiltError as e:
                logger.warning(
                    "no centroid for %s (index unbuilt; build it to let the cache hit): %s",
                    p.slug,
                    e,
                )
                vecs.append(None)
        dims = {v.shape[0] for v in vecs if v is not None}
        if not dims:
            return np.zeros((0, 0), dtype=np.float32)
        dim = dims.pop()
        mat = np.vstack([np.zeros(dim, dtype=np.float32) if v is None else v for v in vecs])
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms < 1e-12] = 1.0
        mat = (mat / norms).astype(np.float32)
        self._memoize(roster, mat)
        self._save_cache(roster, mat)
        return mat

    @staticmethod
    def _published_rows(store) -> dict[str, np.ndarray] | None:
        """``slug -> centroid`` when the store holds the whole matrix, else ``None``.

        A published site ships every centroid in one blob, saving a fetch per
        profile. Keyed by slug, since the store's rows need not match the roster.
        """
        matrix = store.centroids_matrix()
        if matrix is None:
            return None
        slugs, vectors = matrix
        return {slug: np.asarray(vectors[i], dtype=np.float32) for i, slug in enumerate(slugs)}

    def _save_cache(self, roster: "_Roster", mat: np.ndarray) -> None:
        """Write the ``.npz`` memo, when there is a directory to write it into."""
        cache_path = self.cache_path
        if cache_path is None:
            return
        try:
            np.savez(
                str(cache_path),
                slugs=np.array(roster.slugs, dtype=object),
                vectors=mat,
                backend_name=np.array(self._store.backend_spec or ""),
                created_at=np.array(now_iso()),
            )
        except OSError:
            logger.debug("could not write centroid cache at %s", cache_path, exc_info=True)

    # ------------------------------------------------------------------
    # Query backend
    # ------------------------------------------------------------------

    @property
    def backend(self):
        """The backend queries are embedded with, resolved once and kept.

        The environment override wins, then the store's detected index
        backend, then whatever the first profile resolves to.
        """
        if self._backend is None:
            env = os.environ.get("RESEARCHER_PROFILES_EMBEDDING_BACKEND")
            spec = env or self._store.backend_spec
            profiles = self._rostered().profiles
            self._backend = (
                get_backend(spec)
                if spec
                else (_resolve_backend(profiles[0]) if profiles else get_backend(None))
            )
        return self._backend

    def embed_query(self, text: str) -> np.ndarray:
        """``text`` as a unit vector in the same space as :attr:`matrix`."""
        v = np.array(self.backend.embed([text])[0], dtype=np.float32)
        n = float(np.linalg.norm(v))
        if n < 1e-12:
            return v
        return v / n


__all__ = ["CentroidManager"]
