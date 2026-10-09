"""The transactional boundary and hook engine for one profile's writes."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import WriteHookError

if TYPE_CHECKING:  # pragma: no cover
    from . import ResearcherProfile
    from .storage import ArtifactStorage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WriteContext:
    """What a pre-commit hook is handed. One logical profile write.

    Frozen: a hook observes a write. It does not reshape it.
    """

    #: The profile being written. Reads through it (including
    #: ``content_hash``) observe POST-write state.
    profile: "ResearcherProfile"
    slug: str
    rid: str
    #: Which artifact(s) this unit is writing: ``"document"``, ``"soul"``,
    #: ``"expertise"``, ``"papers"``, ``"grants"``, ``"citations"``,
    #: ``"summary"``, ``"build_state"``, ``"create"``, ``"delete"``,
    #: ``"import"``, ``"merge"``. A hook may filter on it; most will not.
    kind: str
    #: The backend's transaction handle, or ``None`` when the backend has none.
    #: For a SQL store this is the SQLAlchemy ``Session`` the write is going
    #: through. A hook must use this session, never open its own, or it will
    #: deadlock against the row this unit already holds.
    session: Any | None
    #: ``True`` when the hook's writes will commit or roll back atomically with
    #: this unit. Branch on this, not on ``session is None``; they are not
    #: synonyms.
    atomic: bool
    #: The ambient HTTP request when the write came from an API route; ``None``
    #: for CLI, pipeline, and out-of-process writes.
    request: Any | None = None
    #: For ``kind == "merge"`` only: the rid and slug of the profile this write
    #: retires into ``rid`` (``SqlProfileStore.merge_into``). ``None`` otherwise.
    retired_rid: str | None = None
    retired_slug: str | None = None


#: A pre-commit hook: one callable, one :class:`WriteContext`, no return value.
WriteHook = Callable[[WriteContext], None]


class WriteUnit:
    """The transactional boundary and hook engine for one profile.

    A storage backend supplies only ``new_write_context`` /
    ``refresh_derived`` / ``commit`` / ``rollback``; this owns the ordering.

    On rollback, registered compensations run first (only the filesystem
    backend registers any), then the backend's ``rollback``.
    """

    def __init__(self, profile: "ResearcherProfile", storage: "ArtifactStorage") -> None:
        self._profile = profile
        self._storage = storage
        self._pre_commit_hooks: list[WriteHook] = []
        self._post_commit_hooks: list[WriteHook] = []
        self._ctx: WriteContext | None = None
        self._depth: int = 0
        self._hooks_pending: bool = False
        self._compensations: list[Callable[[], None]] = []
        self._after_commit: dict[str, Any] = {}

    # --- registration ---------------------------------------------------

    def add_pre_commit_hook(self, hook: WriteHook) -> None:
        self._pre_commit_hooks.append(hook)

    def add_post_commit_hook(self, hook: WriteHook) -> None:
        self._post_commit_hooks.append(hook)

    def register_compensation(self, undo: Callable[[], None]) -> None:
        """Register a best-effort undo for the open write unit.

        This is a compensating write, not a transaction: the bytes may already
        be on disk, and the only thing that can be put back is what the writer
        happened to read first.
        """
        if self._ctx is not None:
            self._compensations.append(undo)

    def defer_cache_update(self, name: str, value: Any) -> None:
        """Stage a cache change, visible in this unit and applied on commit."""
        if self._ctx is None:
            raise RuntimeError("cache updates must be staged inside a write unit")
        self._after_commit[name] = value

    def pending_cache(self, name: str) -> tuple[bool, Any]:
        """Return a transaction-local cache value when one has been staged."""
        return (name in self._after_commit, self._after_commit.get(name))

    # --- firing ---------------------------------------------------------

    def _fire_pre_commit_hooks(self, ctx: WriteContext) -> None:
        self._hooks_pending = False
        for hook in list(self._pre_commit_hooks):
            try:
                hook(ctx)
            # Boundary: a host-registered hook; anything it raises aborts the write.
            except Exception as e:
                name = getattr(hook, "__qualname__", None) or repr(hook)
                raise WriteHookError(name, e) from e

    def run_pre_commit_hooks(self, ctx: WriteContext) -> None:
        """Fire the pre-commit hooks for this unit, exactly once.

        A nested writer defers to the outermost unit, so several writes in one
        unit produce one hook run just before the single commit.
        """
        if self._depth > 1:
            self._hooks_pending = True
            return
        self._fire_pre_commit_hooks(ctx)

    def _fire_post_commit_hooks(self, ctx: WriteContext) -> None:
        """Run every post-commit hook; a failure is logged at WARNING, never raised."""
        for hook in list(self._post_commit_hooks):
            try:
                hook(ctx)
            # Boundary: a host-registered hook, after the write is already durable.
            except Exception:
                name = getattr(hook, "__qualname__", None) or repr(hook)
                logger.warning(
                    "post-commit hook %s failed for %s",
                    name,
                    self._profile.slug,
                    exc_info=True,
                )

    def _run_compensations(self) -> None:
        for undo in reversed(self._compensations):
            try:
                undo()
            # Boundary: a compensating undo; every remaining one still runs.
            except Exception:
                logger.exception(
                    "compensating write failed for %s; the store may now hold "
                    "a partially-applied write",
                    self._profile.slug,
                )

    # --- the unit -------------------------------------------------------

    @contextmanager
    def open(self, kind: str) -> Iterator[WriteContext]:
        """The transactional boundary for one logical write.

        Joins an ambient unit if one is open on this profile; otherwise opens
        one and commits it on clean exit. Nesting never produces nested
        commits or a second hook run.

        Ordering contract, within one unit:

        1. The artifact bytes/rows are written through the storage backend.
        2. Derived state is refreshed (``storage.refresh_derived``), so a hook
           calling ``content_hash`` sees the new content.
        3. Pre-commit hooks run in registration order.
        4. The unit commits.
        5. Staged cache updates apply, so a rolled-back write leaves the cache
           matching the store.
        6. Post-commit hooks run in registration order; a raise is logged,
           never propagated.

        Failure contract: a raising pre-commit hook aborts the write. The unit
        does not commit, a transactional backend rolls back, and the exception
        propagates as :class:`~researcher_profiles.errors.WriteHookError`
        naming the hook (the original as ``__cause__``). The first raise stops
        the rest.

        The filesystem backend has no transaction but runs hooks at the same
        point; only atomicity differs, and ``ctx.atomic`` says which. A hook
        that needs atomicity must branch on ``ctx.atomic``: raise
        :class:`~researcher_profiles.errors.TransactionRequired`, or knowingly
        degrade. It must never pass ``ctx.session`` (``None`` on the
        filesystem) into a call that opens its own autocommitting connection.
        """
        if self._ctx is not None:
            self._depth += 1
            try:
                yield self._ctx
            finally:
                self._depth -= 1
            return

        ctx = self._new_context(kind)
        self._ctx = ctx
        self._depth = 1
        self._hooks_pending = False
        self._compensations = []
        self._after_commit = {}
        try:
            yield ctx
            if self._hooks_pending:
                self._fire_pre_commit_hooks(ctx)
            self._commit(ctx)
            for name, value in self._after_commit.items():
                setattr(self._profile, name, value)
            # Only the outermost unit reaches here, so post-commit hooks fire once.
            self._fire_post_commit_hooks(ctx)
        except BaseException:
            self._run_compensations()
            self._rollback(ctx)
            raise
        finally:
            self._ctx = None
            self._depth = 0
            self._hooks_pending = False
            self._compensations = []
            self._after_commit = {}

    # --- backend calls --------------------------------------------------

    def _new_context(self, kind: str) -> WriteContext:
        return self._storage.new_write_context(self._profile, kind)

    def _commit(self, ctx: WriteContext) -> None:
        self._storage.commit(ctx)

    def _rollback(self, ctx: WriteContext) -> None:
        self._storage.rollback(ctx)


__all__ = ["WriteContext", "WriteHook", "WriteUnit"]
