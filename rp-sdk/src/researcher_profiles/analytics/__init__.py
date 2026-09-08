"""What the whole set of profiles looks like together: centroids, query matching, and per-profile index building.

Three concerns, one module and one manager each. None of them is an aggregate
over the store; a "collection of profiles" is the store itself, and each
manager is reached the way ``prof.cite`` is reached one level down:

- ``store.centroids`` (:class:`~.centroids.CentroidManager`): the centroid
  matrix, its ``<root>/.cache/centroids.npz`` memo, and the query backend.
- ``store.match`` (:class:`~.match.MatchManager`): ranking a query against
  every profile, MMR diversification, the topic index, clustering.
- ``store.indexes`` (:class:`~.indexes.IndexFleetManager`): building and
  reporting on each profile's *embedding* index.

All three read profiles and their vectors through a
:class:`~researcher_profiles.store.ProfileStore`; ranking and centroids need
the :class:`~researcher_profiles.store.VectorStore` capability on top of it,
checked once, up front, by
:func:`researcher_profiles.store._analytics.require_vector_store`. A store with
no directory (a published site read over HTTP) ranks exactly as well as one
with; what it does without is the optional on-disk memos.

The snapshot the managers compute over is private
(:mod:`.roster`) and is stamped with the store's write generation, so a write
through the store or through a handed-out profile invalidates it with no
explicit call.
"""

from .centroids import CentroidManager
from .indexes import IndexFleetManager
from .match import MatchManager

__all__ = [
    "CentroidManager",
    "MatchManager",
    "IndexFleetManager",
]
