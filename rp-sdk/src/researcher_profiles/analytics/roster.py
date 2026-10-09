"""The private roster snapshot the analytics compute over.

Matrix row ``i`` must mean profile ``i`` for a whole ``rank()`` call, so the
analytics read a slug-sorted snapshot stamped with the store's write
generation, rebuilt whenever that generation moves. Private to this package.
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

        A malformed profile is logged and skipped, so one bad directory cannot
        take down ``/match``. An unindexed profile is kept; it gets a zero
        centroid row and never ranks.
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
        """The current roster, rebuilt when the store's generation has moved."""
        generation = getattr(self._store, "generation", 0)
        if self._roster is None or self._roster.generation != generation:
            self._roster = _Roster.from_store(self._store)
        return self._roster

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_RosterCache(store={self._store.location!r})"


__all__ = ["_Roster", "_RosterCache"]
