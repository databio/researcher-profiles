"""``prof.index``: the per-profile embedding index.

Defines :class:`IndexManager`, the object ``ResearcherProfile.index`` hands
back (``prof.index.build()`` / ``.search()`` / ``.search_similar()`` /
``.embedding()``). Distinct from
:class:`researcher_profiles.analytics.indexes.IndexFleetManager`, which
operates over every profile in a store; this manager is one profile's own
index. Importing this module has no side effect on the profile class.
"""

from ..embeddings import _index_root
from ..embeddings.cache import IndexReport, SqliteEmbeddingIndex


class IndexManager:
    """``prof.index``: the per-profile embedding index.

    Needs a local directory: the index is a ``.cache/embeddings.sqlite`` handle.
    """

    def __init__(self, profile):
        self._profile = profile
        self._idx: SqliteEmbeddingIndex | None = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"IndexManager(slug={self._profile.slug!r})"

    def _index(self) -> SqliteEmbeddingIndex:
        if self._idx is None:
            root = _index_root(self._profile)
            self._idx = SqliteEmbeddingIndex(root, profile_document=self._profile.metadata)
        return self._idx

    def build(self, *, force: bool = False, backend=None) -> IndexReport:
        """Build (or refresh) this profile's embedding index."""
        return self._index().build_index(force=force, backend=backend)

    def search(self, query: str, k: int = 5, filter: dict | None = None):
        """Semantic search over this profile's chunks."""
        return self._index().search(query, k=k, filter=filter)

    def search_similar(self, source_type: str, source_id: str, chunk_index: int = 0, k: int = 5):
        """Chunks nearest to one this profile already holds."""
        return self._index().search_similar(source_type, source_id, chunk_index=chunk_index, k=k)

    def embedding(self, kind: str = "centroid"):
        """The profile-level vector: ``centroid`` / ``summary`` / ``expertise``."""
        from ..embeddings.profile_vec import _profile_embedding_impl

        return _profile_embedding_impl(self._profile, kind)


__all__ = ["IndexManager"]
