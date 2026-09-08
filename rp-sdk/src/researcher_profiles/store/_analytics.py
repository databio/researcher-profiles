"""The three cross-profile analytics accessors every store carries.

``store.centroids``, ``store.match`` and ``store.indexes`` are the same idiom
:class:`~researcher_profiles.profile.ResearcherProfile` uses one level down for
``prof.cite`` / ``prof.topics`` / ``prof.edit``: one sub-object per concern,
built on first access, each living in its own module. There is no aggregate
object over a store any more; a "collection of profiles" is the store itself.

Everything here is deliberately lazy. This module sits on the core import path
(every backend mixes it in) and all three managers pull numpy, so the imports
live inside the property bodies. A guardrail asserts the store contract stays
importable without the vector extras.
"""

from typing import TYPE_CHECKING, Any

from ..errors import CapabilityUnavailableError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..analytics.centroids import CentroidManager
    from ..analytics.indexes import IndexFleetManager
    from ..analytics.match import MatchManager
    from ..analytics.roster import _RosterCache

#: The :class:`~researcher_profiles.store.VectorStore` surface, as names. Used
#: for a duck check rather than ``isinstance``: the capability protocol now
#: also declares the accessors below, so an ``isinstance`` here would invoke
#: the very property doing the checking.
_VECTOR_METHODS = (
    "backend_spec",
    "has_vector_index",
    "vector_index",
    "centroid",
    "centroids_matrix",
)


def require_vector_store(store: Any) -> Any:
    """``store``, proved to serve vectors. Raises when it does not.

    The single up-front check behind ranking and centroids. It replaces the
    per-analytic degradation the SDK used to do, where each feature discovered
    separately that there was no directory and turned that into its own 503.
    """
    if any(not hasattr(store, name) for name in _VECTOR_METHODS):
        raise CapabilityUnavailableError(
            f"ranking needs a store that can serve vectors; "
            f"{type(store).__name__} at {store.location} cannot. "
            f"Export the profiles to a directory and use FilesystemProfileStore, "
            f"or point HttpProfileStore at a published site."
        )
    return store


class _AnalyticsAccessors:
    """Mixed into every backend to supply the three analytics accessors.

    The managers share one :class:`~researcher_profiles.analytics.roster._RosterCache`
    per store, so a rank() and the centroid matrix behind it always see the
    same roster, and both drop it the moment the store's write generation
    moves. That is the whole reason analytics can live directly on a live,
    mutable store: the snapshot they compute over can never outlast a write.

    The accessors themselves never raise. A store that cannot serve vectors is
    told so where the vectors are actually needed
    (``match.rank``, ``centroids.matrix``), in one actionable error.
    """

    _centroid_mgr: Any = None
    _match_mgr: Any = None
    _index_fleet_mgr: Any = None
    _roster_cache: Any = None

    @property
    def _rostered(self) -> "_RosterCache":
        """The shared, generation-stamped roster view. One per store."""
        if self._roster_cache is None:
            from ..analytics.roster import _RosterCache

            self._roster_cache = _RosterCache(self)
        return self._roster_cache

    @property
    def centroids(self) -> "CentroidManager":
        """``store.centroids``: the centroid matrix, its cache, the query backend."""
        if self._centroid_mgr is None:
            from ..analytics.centroids import CentroidManager

            self._centroid_mgr = CentroidManager(self, self._rostered)
        return self._centroid_mgr

    @property
    def match(self) -> "MatchManager":
        """``store.match``: ranking, MMR diversification, topics, clustering."""
        if self._match_mgr is None:
            from ..analytics.match import MatchManager

            self._match_mgr = MatchManager(self, self._rostered)
        return self._match_mgr

    @property
    def indexes(self) -> "IndexFleetManager":
        """``store.indexes``: build and report on every profile's index."""
        if self._index_fleet_mgr is None:
            from ..analytics.indexes import IndexFleetManager

            self._index_fleet_mgr = IndexFleetManager(self, self._rostered)
        return self._index_fleet_mgr


__all__ = ["_AnalyticsAccessors", "require_vector_store"]
