"""The three cross-profile analytics accessors every store carries.

``store.centroids``, ``store.match`` and ``store.indexes``, each built on first
access. Imports live inside the property bodies because this module is on the
core import path and the managers pull numpy; a guardrail asserts the store
contract imports without the vector extras.
"""

from typing import TYPE_CHECKING, Any

from ..errors import CapabilityUnavailableError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..analytics.centroids import CentroidManager
    from ..analytics.indexes import IndexFleetManager
    from ..analytics.match import MatchManager
    from ..analytics.roster import _RosterCache

#: The :class:`~researcher_profiles.store.VectorStore` surface, as names. A duck
#: check, not ``isinstance``, which would invoke the accessors below.
_VECTOR_METHODS = (
    "backend_spec",
    "has_vector_index",
    "vector_index",
    "centroid",
    "centroids_matrix",
)


def require_vector_store(store: Any) -> Any:
    """``store``, proved to serve vectors.

    Raises ``CapabilityUnavailableError`` when it does not. The single up-front
    check behind ranking and centroids.
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
    per store, which drops its snapshot when the write generation moves, so no
    analytic serves a roster older than the last write.

    The accessors never raise; a store without vectors is told so where vectors
    are needed (``match.rank``, ``centroids.matrix``).
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
        """See :attr:`~.protocol.VectorStore.centroids`."""
        if self._centroid_mgr is None:
            from ..analytics.centroids import CentroidManager

            self._centroid_mgr = CentroidManager(self, self._rostered)
        return self._centroid_mgr

    @property
    def match(self) -> "MatchManager":
        """See :attr:`~.protocol.VectorStore.match`."""
        if self._match_mgr is None:
            from ..analytics.match import MatchManager

            self._match_mgr = MatchManager(self, self._rostered)
        return self._match_mgr

    @property
    def indexes(self) -> "IndexFleetManager":
        """See :attr:`~.protocol.VectorStore.indexes`."""
        if self._index_fleet_mgr is None:
            from ..analytics.indexes import IndexFleetManager

            self._index_fleet_mgr = IndexFleetManager(self, self._rostered)
        return self._index_fleet_mgr


__all__ = ["_AnalyticsAccessors", "require_vector_store"]
