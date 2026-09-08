"""The private roster snapshot the analytics compute over.

A matrix row ``i`` has to still mean profile ``i`` for the whole of a
``rank()`` call, so the analytics do not iterate a live store directly: they
read a :class:`_Roster`, a slug-sorted snapshot of every loadable profile,
stamped with the store's write generation at the moment it was built.

:class:`_RosterCache` is the one object per store that hands that snapshot out.
It rebuilds whenever ``store.generation`` has moved, which is what makes
analytics on a live, mutable store safe: a snapshot is always coherent, and can
never outlast a write. Cross-process staleness is a separate problem with a
separate answer (the on-disk memos validate themselves; see
:mod:`.centroids`).

Both names are private. Nothing outside this package should hold a roster: an
aggregate object over a store is exactly what was dissolved to get here.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..errors import ProfileLoadError
from ..utils.paths import store_cache_dir

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..profile import ResearcherProfile
    from ..store.protocol import ProfileStore

logger = logging.getLogger(__name__)


class _Roster:
    """Every loadable profile in a store, slug-sorted, at one write generation."""

    def __init__(
        self,
        store: "ProfileStore",
        profiles: "list[ResearcherProfile]",
        generation: int,
    ) -> None:
        self.store = store
        #: The store's write generation when this snapshot was taken.
        self.generation = generation
        self.profiles: "list[ResearcherProfile]" = sorted(profiles, key=lambda p: p.slug)
        self.slugs: list[str] = [p.slug for p in self.profiles]
        self.by_slug: "dict[str, ResearcherProfile]" = {p.slug: p for p in self.profiles}
        # A duplicate rid cannot reach here: the filesystem backend raises
        # ``DuplicateIdentityError`` while building its rid map, SQL keys on
        # the rid, and a published ``by-rid.json`` is a mapping.
        self.by_rid: "dict[str, ResearcherProfile]" = {
            rid: p for p in self.profiles if (rid := getattr(p, "rid", None))
        }

    def __len__(self) -> int:
        return len(self.profiles)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Roster(store={self.store.location!r}, n={len(self)}, gen={self.generation})"

    @property
    def root(self) -> Optional[Path]:
        """The store's filesystem root, or ``None``. For the on-disk memos only."""
        return self.store.root

    def store_cache_dir(self) -> Optional[Path]:
        """``<root>/.cache/``, or ``None``."""
        return store_cache_dir(self.root)

    @classmethod
    def from_store(cls, store: "ProfileStore") -> "_Roster":
        """Load every profile in ``store``. Pure: nothing is written.

        A profile whose document is malformed is logged and skipped rather than
        failing the whole load, because one bad directory in a roster of 300
        must not take down ``/match``. An *unindexed* profile is kept: it
        contributes a zero centroid row and can never rank, which is what the
        CLI has always done. "profiles exist but none is indexed" is a question
        the serving layer asks explicitly (``api.deps.get_match_store``), not
        something a filtered roster answers by being empty.
        """
        generation = getattr(store, "generation", 0)
        profiles: "list[ResearcherProfile]" = []
        for slug in store.list_slugs():
            try:
                profiles.append(store.get(slug))
            except ProfileLoadError as e:
                logger.warning("skipping malformed profile %s in %s: %s", slug, store.location, e)
                continue
        return cls(store, profiles, generation)


class _RosterCache:
    """One per store: hands out the current roster, rebuilding after a write."""

    def __init__(self, store: "ProfileStore") -> None:
        self._store = store
        self._roster: Optional[_Roster] = None

    def __call__(self) -> _Roster:
        """The roster as of now. Rebuilt when the store's generation has moved."""
        generation = getattr(self._store, "generation", 0)
        if self._roster is None or self._roster.generation != generation:
            self._roster = _Roster.from_store(self._store)
        return self._roster

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_RosterCache(store={self._store.location!r})"


__all__ = ["_Roster", "_RosterCache"]
