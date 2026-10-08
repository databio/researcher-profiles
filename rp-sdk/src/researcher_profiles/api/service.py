"""The service layer: plain functions that hold the logic and every check.

Each function takes a :class:`~researcher_profiles.api.service.Service` (the
store, the host hooks, and the app's caches, bound once per app), an explicit
:class:`~researcher_profiles.api.caller.Caller`, and its arguments. It checks
permission itself (through the hooks), does the work, and raises only the
typed errors in :mod:`researcher_profiles.errors`. The HTTP routes and a
host's MCP tools are two thin adapters over these functions; neither holds a
check of its own.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..errors import NotFound
from ..privacy import ViewerTier, narrow_viewer, profile_visible
from ..store import ProfileNotFoundError, ProfileStore
from ..utils.paths import STORE_CACHE_DIRNAME
from .caller import Caller
from .deps import TierFloor
from .hooks import Hooks

logger = logging.getLogger(__name__)


def profile_missing(ref: str) -> NotFound:
    """The not-found for "no such profile" and for "not for you": the same sentence."""
    return NotFound(f"profile {ref!r} not found")


class Service:
    """The store, the hooks, and the two caches every request shares. One per app."""

    def __init__(self, store: ProfileStore, hooks: Optional[Hooks] = None):
        self.store = store
        self.hooks = hooks if hooks is not None else Hooks()
        #: The profile graph, built lazily by ``deps.get_graph``.
        self.graph: Any = None
        #: The temp directory a rootless store is exported to for the graph.
        self.registry_tempdir: Any = None

    # -- the hooks, with their defaults ------------------------------------

    def store_for(self, caller: Caller) -> ProfileStore:
        """The store as this caller reads it. Never used by an edit."""
        hook = self.hooks.store_for
        return self.store if hook is None else hook(caller, self.store)

    def viewer(self, caller: Caller, slug: Optional[str]) -> ViewerTier:
        """This caller's viewer tier for one profile, preview cap applied."""
        tier: ViewerTier = self.hooks.viewer_resolver(caller, slug)
        if caller.viewer_cap is not None:
            tier = narrow_viewer(tier, caller.viewer_cap)
        return tier

    def floor(self, caller: Caller, prof: Any, slug: Optional[str] = None) -> TierFloor:
        """The host's floor for this profile, and why: never ``None``."""
        return self.hooks.profile_tier_floor(caller, prof, slug) or TierFloor()

    def proofs(self, rid: str) -> list:
        """The registry-issued proofs for ``rid``; ``[]`` when none or the hook fails.

        A proof failure must never fail a public read, so a raising hook is
        logged and treated as ``[]``.
        """
        hook = self.hooks.registry_proofs
        if hook is None or not rid:
            return []
        try:
            return list(hook(rid) or [])
        # Boundary: whatever the host's hook raises, the read still succeeds.
        except Exception:
            logger.warning("registry_proofs hook failed for %r", rid, exc_info=True)
            return []

    def invalidate(self, slug: str) -> None:
        """Drop every cache that could still reflect the pre-edit profile.

        The cached profile object and the store's on-disk ``.cache`` memos
        (centroids/topics/graph): the exact set a tarball push invalidates.
        ``store.evict`` also bumps the store's write generation, which is the
        whole in-process invalidation for ranking.

        **Cache invalidation only.** Dependent-state maintenance belongs on
        the write hooks (``store.add_pre_commit_hook``), which run inside the
        write. Failures here stay swallowed: a stale-cache rebuild is cheap and
        re-eviction is idempotent.
        """
        store = self.store
        store.evict(slug)
        # The derived graph follows the same rule: drop the snapshot so the
        # next graph query rebuilds over the new corpus.
        self.graph = None
        # A rootless store materializes into a temp directory the graph build
        # reuses; drop it so the next build re-exports the live corpus.
        tempdir = self.registry_tempdir
        if tempdir is not None:
            cleanup = getattr(tempdir, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup()
                except OSError:
                    pass
            self.registry_tempdir = None
        root = store.root
        if root is None:
            return
        # Names defined by ``CentroidManager.cache_path``,
        # ``MatchManager.topics_cache_path`` and ``graph.cache.graph_db_path``;
        # literal here because this path must work without importing them.
        for cache_name in ("centroids.npz", "topics.json", "graph.sqlite"):
            fp = root / STORE_CACHE_DIRNAME / cache_name
            if fp.exists():
                try:
                    fp.unlink()
                except OSError:
                    pass

    # -- the one entry point every read starts from -------------------------

    def load_visible(self, caller: Caller, ref: str) -> tuple[Any, ViewerTier]:
        """``(profile, viewer tier)`` for a profile this caller may see, else ``NotFound``.

        The profile comes from the caller's view of the store; a profile the
        caller may not see is the same ``NotFound`` as one that does not exist.
        """
        store = self.store_for(caller)
        try:
            prof = store.get(ref)
        except (ProfileNotFoundError, KeyError) as e:
            raise profile_missing(ref) from e
        viewer = self.viewer(caller, ref)
        floor = self.floor(caller, prof, ref)
        if not profile_visible(prof.metadata, viewer, floor=floor.tier):
            raise profile_missing(ref)
        return prof, viewer


__all__ = ["Service", "profile_missing"]
