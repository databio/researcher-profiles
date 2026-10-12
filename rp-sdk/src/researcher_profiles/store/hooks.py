"""``_HookedStore``: shared write-hook and write-generation bookkeeping."""

from collections.abc import Iterable

from ..profile import ResearcherProfile
from ..profile.write_unit import WriteHook


class _HookedStore:
    """Shared hook bookkeeping for the concrete stores.

    ``_live_profiles`` is each backend's view of profiles already handed out
    (an LRU for the filesystem, a weak set for SQL).
    """

    _pre_commit_hooks: list[WriteHook]
    _post_commit_hooks: list[WriteHook]
    _generation: int

    def _init_hooks(self) -> None:
        self._pre_commit_hooks = []
        self._post_commit_hooks = []
        self._generation = 0
        # Appended directly: at init there are no live profiles to fan out to.
        # A write through a handed-out profile never touches a store method,
        # so this hook is what moves the generation for it.
        self._post_commit_hooks.append(self._bump_generation_hook)

    # --- the write generation ------------------------------------------------

    @property
    def generation(self) -> int:
        """How many writes this store has seen. Only ever increases.

        A coarse store-wide "something changed" stamp; a derived view rebuilds
        when it moves. A read-only backend's generation never moves.
        """
        return self._generation

    def _bump_generation(self) -> None:
        """Mark the store changed. Every mutating method calls this."""
        self._generation += 1

    def _bump_generation_hook(self, ctx) -> None:  # noqa: ARG002 - the ctx is unused
        self._bump_generation()

    def _live_profiles(self) -> Iterable[ResearcherProfile]:
        """Profiles already handed out that a new hook must still reach."""
        raise NotImplementedError

    def add_pre_commit_hook(self, hook: WriteHook) -> None:
        """See :meth:`~.protocol.ProfileStore.add_pre_commit_hook`."""
        self._pre_commit_hooks.append(hook)
        for prof in self._live_profiles():
            prof.add_pre_commit_hook(hook)

    def add_post_commit_hook(self, hook: WriteHook) -> None:
        """See :meth:`~.protocol.ProfileStore.add_post_commit_hook`."""
        self._post_commit_hooks.append(hook)
        for prof in self._live_profiles():
            prof.add_post_commit_hook(hook)

    def _thread_hooks(self, prof: ResearcherProfile) -> None:
        """Copy every registered hook onto ``prof``."""
        for hook in self._pre_commit_hooks:
            prof.add_pre_commit_hook(hook)
        for hook in self._post_commit_hooks:
            prof.add_post_commit_hook(hook)
