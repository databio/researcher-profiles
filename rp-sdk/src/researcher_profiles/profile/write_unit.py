"""The transactional boundary and hook engine for one profile's writes.

:class:`WriteUnit` owns the per-profile write machinery so that
:class:`~researcher_profiles.profile.ResearcherProfile` does not: the pre/post
commit hook lists, the open context, the nesting depth, the deferred-hook flag,
and the compensation list. The profile keeps only ``write_unit(kind)`` / ``add_pre_commit_hook`` / ``add_post_commit_hook``
as one-line delegations, because those three are the public interface that
``api.deps`` and out-of-tree callers register against.
"""

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
    #: ``"import"``. A hook may filter on it; most will not.
    kind: str
    #: The backend's transaction handle, or ``None`` when the backend has none.
    #: For a SQL store this is the SQLAlchemy ``Session`` the write is going
    #: through. A hook must use this session, never open its own, or it will
    #: deadlock against the row this unit already holds.
    session: Any | None
    #: ``True`` when the hook's writes will commit or roll back atomically with
    #: this unit. Branch on this, not on ``session is None``. The two are not
    #: synonyms and will diverge the moment a second backend appears.
    atomic: bool
    #: The ambient HTTP request when the write came from an API route; ``None``
    #: for CLI, pipeline, and out-of-process writes. A hook that dereferences
    #: this unconditionally will break every non-HTTP write path.
    request: Any | None = None


#: A pre-commit hook: one callable, one :class:`WriteContext`, no return value.
WriteHook = Callable[[WriteContext], None]


class WriteUnit:
    """The transactional boundary and hook engine for one profile.

    Composed onto the profile rather than inherited, so a storage backend
    supplies only ``new_write_context`` / ``refresh_derived`` / ``commit`` /
    ``rollback`` and never has to reimplement the ordering contract.

    On rollback, this always runs its own registered compensations (only the
    filesystem backend registers any), then calls the backend's ``rollback``.
    On the filesystem, the compensations are the rollback. Separating them
    means a transactional backend cannot accidentally inherit compensation
    semantics.
    """

    def __init__(self, profile: "ResearcherProfile", storage: "ArtifactStorage") -> None:
        self._profile = profile
        self._storage = storage
        self._pre_commit_hooks: list[WriteHook] = []
        #: Fire-and-forget notifications run after a write unit commits; see
        #: :meth:`add_post_commit_hook`. Unlike ``_pre_commit_hooks`` these
        #: never abort a write, so there is no pending/deferral bookkeeping.
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

        A nested writer defers to the outermost unit rather than firing again,
        so wrapping several writes in one unit produces one hook run
        immediately before the single commit.
        """
        if self._depth > 1:
            self._hooks_pending = True
            return
        self._fire_pre_commit_hooks(ctx)

    def _fire_post_commit_hooks(self, ctx: WriteContext) -> None:
        """Run every registered post-commit hook, swallowing its failures.

        The write already committed by the time this runs, so a failing hook
        has nothing to roll back and must never propagate: it is logged at
        WARNING and the remaining hooks still run.
        """
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

        Joins an ambient unit if one is already open on this profile;
        otherwise opens one and commits it on clean exit. Nesting is therefore
        safe and does not produce nested commits or a second hook run, which
        is what lets a caller wrap several writes in a single atomic
        operation.

        Ordering contract: within one unit, in this exact order:

        1. The artifact bytes/rows are written through the storage backend.
        2. Store-maintained derived state is refreshed
           (``storage.refresh_derived``): for a SQL store, the
           ``content_hash`` column. This happens before the hooks, so a
           hook calling ``content_hash`` observes the new content. A hook that
           hashed pre-write content would mark dependent state stale against
           the wrong digest. ``content_hash`` spans the document and the
           soul, so a soul-only write refreshes it too. This is why derived
           state is refreshed by the write unit, not by ``save_document``.
        3. Registered ``pre_commit_hook``s run, in registration order, each
           receiving the same :class:`WriteContext`.
        4. Only then does the unit commit.
        5. Registered ``post_commit_hook``s run, in registration order, each
           receiving the same :class:`WriteContext`. Unlike step 3, a raising
           hook here is caught and logged, never propagated. The write is
           already committed, and there is nothing left to abort.

        In-memory cache updates staged by a public writer run after the
        outer commit and before post-commit hooks, so a rolled-back write
        leaves the profile cache matching the store rather than a write that
        never landed.

        Failure contract: a hook raising aborts the write. The unit does
        not commit, a transactional backend rolls back, and the exception
        propagates wrapped in :class:`~researcher_profiles.errors.WriteHookError`
        naming the hook (the original as ``__cause__``). Nothing is swallowed.
        Hooks run in registration order, and the first raise stops the rest.
        A partial run followed by a rollback is fine; a partial run followed
        by a commit is not.

        Filesystem semantics: that backend has no session and no
        transaction. It still runs every registered hook, at the same logical
        point, so the observable contract is identical across backends. Only
        atomicity differs, and ``ctx.atomic`` is how a hook is told which it is
        getting. A raising hook does not undo the write there beyond the
        best-effort compensations registered on this unit.

        A hook written against a transactional store must branch on
        ``ctx.atomic`` and choose explicitly: raise
        :class:`~researcher_profiles.errors.TransactionRequired`, or degrade to
        a non-atomic write it has consciously decided is acceptable. What it
        must not do is pass ``ctx.session`` (``None`` on the filesystem) into a
        call that quietly opens its own autocommitting connection and then
        reports success.
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
            # Only the outermost unit reaches this line (a nested `open`
            # call returns early in the branch above, before any commit), so a
            # batch of writes grouped under one explicit unit fires post-commit
            # hooks once, after the single commit, exactly like the pre-commit
            # hooks it mirrors.
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
    #
    # The storage backend owns the transaction; this owns the ordering.

    def _new_context(self, kind: str) -> WriteContext:
        return self._storage.new_write_context(self._profile, kind)

    def _commit(self, ctx: WriteContext) -> None:
        self._storage.commit(ctx)

    def _rollback(self, ctx: WriteContext) -> None:
        self._storage.rollback(ctx)


__all__ = ["WriteContext", "WriteHook", "WriteUnit"]
