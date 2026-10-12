"""``VectorIndex``: one profile's vectors, whatever they are stored in.

Implemented by :class:`SqliteEmbeddingIndex` (``.cache/embeddings.sqlite``) and
:class:`FlatEmbeddingIndex` (the published ``embeddings/`` files).

Importing this module must not pull numpy (the ``vectors`` extra), so
:mod:`researcher_profiles.store.protocol` can name ``VectorIndex``.
"""

from typing import TYPE_CHECKING, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np


@runtime_checkable
class VectorHit(Protocol):
    """One chunk-level search result.

    Carries no text, since the published form drops it (spec section 5).
    Resolve text from ``(source_type, source_id)`` through the manifest.

    - ``cosine``: raw cosine similarity in [-1, 1].
    - ``score``: ``0.5 * (1 + cosine)``, in [0, 1]. Same ranking as ``cosine``;
      do not mix the two scales when averaging.
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

        Takes text so the index embeds it with its own backend.
        """
        ...

    def centroid(self) -> "np.ndarray":
        """The profile-level vector: the L2-normalized mean of every chunk row.

        Raises :class:`~researcher_profiles.embeddings.IndexNotBuiltError` when
        there are no rows to average.
        """
        ...


__all__ = ["VectorHit", "VectorIndex"]
