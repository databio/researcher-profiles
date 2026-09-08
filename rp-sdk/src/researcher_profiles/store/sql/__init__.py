"""The SQL-backed ``ResearcherProfile`` and the store that holds it.

This is the **third backend**, alongside
:class:`researcher_profiles.profile.storage.DirectoryArtifactStorage` and the
two read-only HTTP ones in :mod:`researcher_profiles.client`. A profile in the
store is a full profile: it loads, serves, edits, exports and
validates-by-format identically, because every one of those paths goes through
the ``ArtifactStorage`` interface rather than through a directory.

Two objects:

:class:`SqlProfileStore`
    A :class:`~researcher_profiles.store.ProfileStore` over an engine: the whole
    protocol, plus the operations only a relational store can offer
    (``put`` of any profile, ``rename``, ``list_profiles``, ``import_directory``).

:class:`SqlArtifactStorage`
    One profile's ``rp_*`` rows, as a
    :class:`~researcher_profiles.profile.storage.ArtifactStorage`. Writes run in a real
    transaction: ``WriteContext.session`` is the SQLAlchemy ``Session`` and
    ``WriteContext.atomic`` is ``True``, so a pre-commit hook can keep its own
    state consistent with the write instead of degrading (see
    ``docs-dev/rp-sdk/developer/storage-seam.md``).

How the SQL store package is laid out
=====================================

One module per object: ``_store`` holds :class:`SqlProfileStore` and
``_storage`` holds :class:`SqlArtifactStorage`. ``_vectors`` holds the vector
shred and the vector queries, so ``_store`` reads as a store rather than as a
store plus an embedding subsystem. ``_shared`` is the leaf they import: the
fixed manifest addresses and the two ``rp_artifacts`` reads (the ordered
row listing, the collection re-render) that a store-level export and a
profile-level load must answer identically. The dependency runs one way,
``_store`` -> ``_storage`` / ``_vectors`` -> ``_shared``; ``_storage`` names
``SqlProfileStore`` only under ``TYPE_CHECKING``.

Requires the ``sql`` extra; see the install instructions in the README.

This store is a :class:`~researcher_profiles.store.VectorStore`: a profile's
public chunk vectors are shredded into ``rp_chunk_vectors`` at ingest and its
centroid into ``rp_profile_vectors``, so ``/match`` and the collection centroid
matrix are queries here, not an export to a temp directory. See
:mod:`._vectors`.

What is not supported here: ``validate()`` (the directory audit walks a
directory), and everything on ``prof.index`` (index *building* needs a local
``.cache/embeddings.sqlite``; reading vectors does not, and goes through
``vector_index``). Each raises ``CapabilityUnavailableError`` naming the way
out: export to a directory with :meth:`SqlProfileStore.export_directory` (or
``rp db pull``) and load that with ``from_files``.
"""

from ._storage import SqlArtifactStorage
from ._store import SqlProfileStore

__all__ = [
    "SqlProfileStore",
    "SqlArtifactStorage",
]
