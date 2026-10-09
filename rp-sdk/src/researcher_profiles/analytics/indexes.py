"""Per-profile embedding-index maintenance and reporting across a store.

These are the *embedding* indexes under each profile's ``.cache/``, not the
store's ``rid <-> slug`` lookup index.
"""

import logging
from typing import TYPE_CHECKING, Any

from ..embeddings.cache import IndexStats, SqliteEmbeddingIndex
from ..utils.paths import derived_cache_path, drop_derived_caches

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..profile import ResearcherProfile
    from ..store.protocol import ProfileStore
    from .roster import _RosterCache

logger = logging.getLogger(__name__)


class IndexFleetManager:
    """``store.indexes``: build and report on every profile's index.

    Directory-only: on a store whose profiles have no directory (SQL, HTTP)
    every method raises
    :class:`~researcher_profiles.errors.CapabilityUnavailableError`.
    """

    def __init__(self, store: "ProfileStore", rostered: "_RosterCache") -> None:
        self._store = store
        self._rostered = rostered

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"IndexFleetManager(store={self._store.location!r})"

    def _index_for(self, profile: "ResearcherProfile") -> SqliteEmbeddingIndex:
        return SqliteEmbeddingIndex(
            profile.require_directory("the fleet index manager"),
            profile_document=profile.metadata,
        )

    def stats(self) -> dict[str, IndexStats]:
        """``slug -> IndexStats``, one index open per profile."""
        return {p.slug: self._index_for(p).stats() for p in self._rostered().profiles}

    def coverage(self) -> list[dict[str, Any]]:
        """Per-profile index / topic / calibration status, slug-ordered."""
        out = []
        for p in self._rostered().profiles:
            st = self._index_for(p).stats()
            profile_dir = p.require_directory("the fleet index manager")
            out.append(
                {
                    "slug": p.slug,
                    "rid": getattr(p, "rid", None),
                    "name": p.name,
                    "has_index": st.exists,
                    "n_chunks": st.n_chunks,
                    "n_papers": st.n_papers,
                    "has_topics": (
                        derived_cache_path(profile_dir, "topics.json").is_file()
                        or (profile_dir / "personality" / "topics.json").is_file()
                    ),
                    "has_calibration": derived_cache_path(
                        profile_dir, "calibration.json"
                    ).is_file(),
                    "last_built_at": st.last_built_at,
                    "backend_name": st.backend_name,
                }
            )
        return out

    def rebuild_all(self, force: bool = False) -> dict[str, Any]:
        """Rebuild every profile's index, then drop everything derived from it.

        Fail-soft per profile: one broken profile is reported as ``None`` and
        does not stop the rest.
        """
        from ..embeddings import build_index

        store = self._store
        reports: dict[str, Any] = {}
        for p in self._rostered().profiles:
            try:
                reports[p.slug] = build_index(p, force=force)
            # Any failure in one profile's build is reported and skipped.
            except Exception as e:
                logger.warning("rebuild failed for %s: %s", p.slug, e)
                reports[p.slug] = None
            drop_derived_caches(p.require_directory("the fleet index manager"))
        # The roster stays current: a rebuild does not change which profiles exist.
        store.centroids.invalidate()
        store.match.invalidate()
        store.write_lookup_index()
        # Prime the centroid cache.
        _ = store.centroids.matrix
        return reports


__all__ = ["IndexFleetManager"]
