"""Store-wide analytics: centroids, query matching, and per-profile index building.

Reached as ``store.centroids``, ``store.match`` and ``store.indexes``. Ranking
and centroids need the :class:`~researcher_profiles.store.VectorStore`
capability; a store with no directory ranks the same, minus the on-disk memos.
"""

from .centroids import CentroidManager
from .indexes import IndexFleetManager
from .match import MatchManager

__all__ = [
    "CentroidManager",
    "MatchManager",
    "IndexFleetManager",
]
