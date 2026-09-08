"""The store's centroid matrix, its on-disk cache, and the query backend.

One row per profile, slug-ordered and L2-normalized, so a query vector dotted
against the matrix is a cosine similarity against every profile at once. That
matrix is what makes ranking a whole store cheap: the expensive per-profile
work (reading each profile's vectors) happens once.

The vectors come from the store's :class:`~researcher_profiles.store.VectorStore`
capability, never from a path. A store that already holds the whole matrix
(a published site publishes one) hands it over in a single call; otherwise this
stacks per-profile centroids itself.

``<root>/.cache/centroids.npz`` is a write-through memo of the result, and
purely an optimization: it exists only when the store is a directory, and a
store without one recomputes in process. Two independent things can make it
stale, and each has its own guard. *In process*, a write bumps the store's
generation, which rebuilds the roster and drops the memoized matrix with it.
*Across processes*, the ``.npz`` is validated against the current slug list and
each profile's index mtime before it is trusted.

Reached as ``store.centroids``.
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

    Both live here because they must agree: a query vector is only comparable
    to the matrix when it came out of the same backend the indexes were built
    with.
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
        """``<root>/.cache/centroids.npz``, or ``None`` without a root.

        ``None`` is not a degradation: the matrix is still computed and still
        memoized in process. What a store with no directory does without is
        carrying it across processes.
        """
        from ..utils.paths import store_cache_dir

        d = store_cache_dir(self._store.root)
        return None if d is None else d / "centroids.npz"

    @property
    def matrix(self) -> np.ndarray:
        """L2-normalized centroids, one row per profile, slug-ordered.

        Memoized in process and cached on disk. Both are dropped when the store
        has been written to; the disk cache is additionally discarded when any
        profile's embedding index is newer than it.
        """
        return self.snapshot()[1]

    def snapshot(self) -> "tuple[_Roster, np.ndarray]":
        """``(roster, matrix)``, guaranteed row-aligned.

        The pair, not the matrix alone, is what ranking needs: row ``i`` of the
        matrix is ``roster.slugs[i]`` for as long as the caller holds both. A
        caller that fetched the roster and the matrix in two steps could be
        handed a matrix rebuilt across a write in between, and would then read
        every score off the wrong profile.
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

        A profile with no index at all makes the cache stale: its centroid
        cannot be trusted to have come from anything. (So a store holding an
        unbuilt profile recomputes on every load; see :meth:`_compute`.)
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
            # A corrupt or unreadable cache degrades to a recompute; it must
            # never break a load. Logged so a cache that never hits is
            # diagnosable rather than merely slow.
            logger.debug("centroid cache at %s unusable; recomputing", cache_path, exc_info=True)
            return None
        self._memoize(roster, vectors)
        return vectors

    def _compute(self, roster: "_Roster") -> np.ndarray:
        """Read every profile's centroid through the store, normalize, memoize, persist.

        A profile whose vectors are unbuilt (or empty) gets a zero row rather
        than aborting the whole matrix: rows must stay aligned with the
        roster's slugs, and a zero vector scores 0 against every query so it
        never ranks. It is logged at WARNING so an operator can build the
        missing index.
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

        The one-request path. A published site ships every profile's centroid in
        one blob, so asking per profile would turn one fetch into hundreds.
        Keyed by slug rather than trusted positionally: the store's rows are its
        own, and need not be the profiles the roster loaded.
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
