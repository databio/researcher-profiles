"""``_HookedStore``: shared write-hook and write-generation bookkeeping.

Two pieces of state every backend needs and none of them should reimplement:
the registered write hooks, and the *write generation*, a counter that moves
every time anything in the store changes. The generation is what lets the
cross-profile analytics (``store.match``, ``store.centroids``) cache a coherent
roster snapshot without ever serving one that predates a write: they compare
the generation their snapshot was built at against the store's current one and
rebuild when it has moved.
"""

from collections.abc import Iterable

from ..profile import ResearcherProfile
from ..profile.write_unit import WriteHook


class _HookedStore:
    """Shared hook bookkeeping for the two concrete stores.

    Hooks live on the STORE, not on an HTTP app, so every profile a store hands
    out carries them and a write fires them whether it came from a route, the
    CLI, or an out-of-process pipeline run (see ``ResearcherProfile.write_unit``).
    Registration reaches profiles handed out from now on and any already handed
    out: ``_live_profiles`` is each backend's view of the latter (an LRU for
    the filesystem, a weak set for SQL).

    Not the ``ProfileStore`` protocol: the backends share no
    storage implementation, only this bookkeeping.
    """

    _pre_commit_hooks: list[WriteHook]
    _post_commit_hooks: list[WriteHook]
    _generation: int

    def _init_hooks(self) -> None:
        self._pre_commit_hooks = []
        self._post_commit_hooks = []
        self._generation = 0
        # Appended directly rather than through ``add_post_commit_hook``: that
        # one fans out to already-handed-out profiles, and at init there are
        # none (nor, on some backends, a container to look them up in yet).
        # ``_thread_hooks`` carries it onto every profile from here on, which
        # is the point: a write through a handed-out profile (``prof.edit``,
        # ``save_profile``) never touches a store method, so without this hook
        # the analytics would keep serving a snapshot that predates it.
        self._post_commit_hooks.append(self._bump_generation_hook)

    # --- the write generation ------------------------------------------------

    @property
    def generation(self) -> int:
        """How many writes this store has seen. Only ever increases.

        Not a version of any one profile: it is a coarse "something changed"
        stamp over the whole store, and a caller that holds a derived view of
        the store compares it against the value the view was built at. A
        read-only backend's generation never moves.
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
        """Register a callable to run inside every write, before commit.

        Applies to profiles handed out from now on and to any already handed
        out, so registration order relative to a first ``get()`` does not
        matter.
        """
        self._pre_commit_hooks.append(hook)
        for prof in self._live_profiles():
            prof.add_pre_commit_hook(hook)

    def add_post_commit_hook(self, hook: WriteHook) -> None:
        """Register a fire-and-forget hook to run after a write commits.

        Same reach as :meth:`add_pre_commit_hook`. See
        ``ResearcherProfile.add_post_commit_hook`` for what a post-commit hook
        may and may not do.
        """
        self._post_commit_hooks.append(hook)
        for prof in self._live_profiles():
            prof.add_post_commit_hook(hook)

    def _thread_hooks(self, prof: ResearcherProfile) -> None:
        """Copy every registered hook onto ``prof``.

        Named ``_thread_`` rather than after the old monkey-patching extension
        model, which is gone: a guardrail greps ``src/`` for that model's
        prefix, and this bookkeeping is unrelated to it.
        """
        for hook in self._pre_commit_hooks:
            prof.add_pre_commit_hook(hook)
        for hook in self._post_commit_hooks:
            prof.add_post_commit_hook(hook)
