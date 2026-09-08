"""``VectorIndex``: one profile's vectors, whatever they are stored in.

The registry's analytics need exactly three things from a profile's vectors:
the profile-level :meth:`~VectorIndex.centroid`, a chunk-level
:meth:`~VectorIndex.search` over free text, and the ``backend_spec`` that says
which embedding space those live in. Everything else on a concrete index
(building it, pruning it, counting rows) is a *build* concern that a served
profile does not have.

Two implementers ship in the SDK, and they have nothing in common but this
shape:

=====================================  ==============================
Index                                  Backing
=====================================  ==============================
:class:`SqliteEmbeddingIndex`          ``.cache/embeddings.sqlite``
:class:`FlatEmbeddingIndex`            the published ``embeddings/`` files
=====================================  ==============================

A ``Protocol`` for the same reason :class:`ProfileStore
<researcher_profiles.store.ProfileStore>` is one: the two share no
implementation, so a base class would only give them an ``__init__`` neither
wants. ``@runtime_checkable`` makes ``isinstance`` a method-presence check,
which is the question a composition root asks.

Importing this module pulls no numpy: the array types are under
``TYPE_CHECKING`` so :mod:`researcher_profiles.store.protocol` can name
``VectorIndex`` without dragging the vectors extra onto the core import path.
"""

from typing import TYPE_CHECKING, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np


@runtime_checkable
class VectorHit(Protocol):
    """One chunk-level search result.

    Carries no text: :class:`~researcher_profiles.embeddings.flat.FlatHit` has
    none to give (the published form drops it, spec section 5), so the shape
    the registry may rely on is the intersection, not
    :class:`~researcher_profiles.embeddings.cache.SearchHit`'s superset. A
    caller that wants text resolves it from ``(source_type, source_id)``
    through the profile manifest.

    Both ``cosine`` and ``score`` are present on every hit:

    - ``cosine``: raw cosine similarity in [-1, 1].
    - ``score``: L2-rescaled similarity, ``0.5 * (1 + cosine)``, in [0, 1].

    The two are monotone transforms of each other, so rankings are identical.
    Using a consistent scale (either one) avoids mixing incompatible magnitudes
    when averaging centroid and chunk scores.
    """

    source_type: str
    source_id: str
    chunk_index: int
    section: str | None
    cosine: float
    score: float


@runtime_checkable
class VectorIndex(Protocol):
    """One profile's vectors: a centroid, a chunk search, and the space they live in."""

    @property
    def backend_spec(self) -> str:
        """The embedding model these vectors came out of, e.g. ``st:all-MiniLM-L6-v2``.

        Cosine similarity across models is meaningless (spec section 7), so
        every consumer that mixes two indexes checks this first.
        """
        ...

    def search(self, query: str, k: int = 5) -> Sequence[VectorHit]:
        """Chunk-level semantic search over free text, best first.

        Text in, not a vector: the index knows which backend built it and
        which one can therefore embed a comparable query. A caller that
        already holds a vector uses the concrete class.
        """
        ...

    def centroid(self) -> "np.ndarray":
        """The profile-level vector: the L2-normalized mean of every chunk row.

        Raises :class:`~researcher_profiles.embeddings.IndexNotBuiltError` when
        there are no rows to average.
        """
        ...


__all__ = ["VectorHit", "VectorIndex"]
