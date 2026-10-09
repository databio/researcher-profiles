"""The SQL-backed ``ResearcherProfile`` and the store that holds it.

A profile here loads, serves, edits and exports like any other, because those
paths go through ``ArtifactStorage`` rather than a directory.

:class:`SqlProfileStore`
    A :class:`~researcher_profiles.store.ProfileStore` over an engine: the whole
    protocol, plus the operations only a relational store can offer
    (``put`` of any profile, ``rename``, ``list_profiles``, ``import_directory``).

:class:`SqlArtifactStorage`
    One profile's ``rp_*`` rows, as an ``ArtifactStorage``. Writes run in a
    real transaction (``WriteContext.atomic`` is ``True``,
    ``WriteContext.session`` is the ``Session``), so a pre-commit hook can stay
    consistent with the write.

Modules: ``_store`` -> ``_storage`` / ``_vectors`` -> ``_shared`` (the leaf).

Requires the ``sql`` extra. A :class:`~researcher_profiles.store.VectorStore`;
see :mod:`._vectors`.

Not supported: ``validate()`` and ``prof.index`` (index building needs a local
directory). Each raises ``CapabilityUnavailableError``; export with
:meth:`SqlProfileStore.export_directory` (or ``rp db pull``) first.
"""

from ._storage import SqlArtifactStorage
from ._store import SqlProfileStore

__all__ = [
    "SqlProfileStore",
    "SqlArtifactStorage",
]
