"""The write unit and the pre/post-commit hooks, on the filesystem backend.

A hook receives a ``WriteContext`` (``write_unit.py``) describing the pending write. This file
pins *when* a hook runs relative to a write, what it sees, and what happens when it fails. The
filesystem backend has no transaction, so ``ctx.atomic`` is False and a raising hook undoes
only the profile document (one compensating write).
"""

import hashlib
import json

import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.errors import ProfileWriteError, WriteHookError
from researcher_profiles.profile.write_unit import WriteContext
from researcher_profiles.schema.jsonld import canonical_dumps
from researcher_profiles.store import FilesystemProfileStore

HOOK_KINDS = ["add_pre_commit_hook", "add_post_commit_hook"]


def _record(sink):
    def hook(ctx: WriteContext) -> None:
        sink.append(ctx)

    return hook


def _raise(ctx: WriteContext) -> None:
    raise RuntimeError("no")


class TestHookFires:
    def test_fires_on_document_soul_and_papers_writes(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        seen: list[WriteContext] = []
        prof.add_pre_commit_hook(_record(seen))

        prof.save_profile(prof.metadata.model_copy(update={"field": "Genomics"}))
        prof.save_soul("# a new soul\n")
        prof.save_papers()

        assert [c.kind for c in seen] == ["document", "soul", "papers"]
        for ctx in seen:
            assert ctx.profile is prof
            assert ctx.slug == prof.slug
            assert ctx.rid == prof.rid
            assert ctx.request is None
            # The filesystem backend has no transaction.
            assert ctx.session is None
            assert ctx.atomic is False


@pytest.mark.parametrize("add", HOOK_KINDS)
def test_hooks_run_in_registration_order(jane_doe_dir, add):
    prof = ResearcherProfile.from_files(jane_doe_dir)
    order: list[str] = []
    getattr(prof, add)(lambda ctx: order.append("first"))
    getattr(prof, add)(lambda ctx: order.append("second"))
    prof.save_soul("# soul\n")
    assert order == ["first", "second"]


@pytest.mark.parametrize("add", HOOK_KINDS)
def test_nested_units_fire_once(jane_doe_dir, add):
    prof = ResearcherProfile.from_files(jane_doe_dir)
    seen: list[WriteContext] = []
    getattr(prof, add)(_record(seen))

    with prof.write_unit("create"):
        prof.save_soul("# soul\n")
        prof.save_expertise("# expertise\n")

    assert len(seen) == 1
    assert seen[0].kind == "create"


@pytest.mark.parametrize("add", HOOK_KINDS)
def test_store_registration_after_a_get_reaches_the_cached_profile(fixture_profiles_root, add):
    cache = FilesystemProfileStore(fixture_profiles_root("jane-doe"))
    prof = cache.get("jane-doe")
    seen: list[WriteContext] = []
    getattr(cache, add)(_record(seen))
    prof.save_soul("# soul\n")
    assert len(seen) == 1


class TestOrdering:
    """Derived state is refreshed BEFORE the hooks."""

    @pytest.mark.parametrize("write", ["document", "soul"])
    def test_hook_observes_the_post_write_content_hash(self, jane_doe_dir, write):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        before = prof.content_hash()
        observed: list[str] = []
        prof.add_pre_commit_hook(lambda ctx: observed.append(ctx.profile.content_hash()))

        if write == "document":
            prof.save_profile(prof.metadata.model_copy(update={"field": "Genomics"}))
        else:
            prof.save_soul("# a genuinely different soul\n")

        assert observed[0] != before
        assert observed[0] == prof.content_hash()


class TestFailure:
    def test_raising_hook_becomes_a_write_hook_error_naming_the_hook(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)

        def exploding_hook(ctx: WriteContext) -> None:
            raise RuntimeError("dependent state is unreachable")

        prof.add_pre_commit_hook(exploding_hook)
        with pytest.raises(WriteHookError) as excinfo:
            prof.save_soul("# soul\n")
        assert "exploding_hook" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, RuntimeError)

    def test_write_hook_error_is_not_a_profile_write_error(self):
        """``edit.py`` maps ProfileWriteError to a 400; a hook fault is a 500."""
        assert not issubclass(WriteHookError, ProfileWriteError)

    def test_first_raise_stops_the_rest(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        ran: list[str] = []

        def boom(ctx: WriteContext) -> None:
            ran.append("boom")
            raise RuntimeError("no")

        prof.add_pre_commit_hook(boom)
        prof.add_pre_commit_hook(lambda ctx: ran.append("second"))
        with pytest.raises(WriteHookError):
            prof.save_soul("# soul\n")
        assert ran == ["boom"]

    def test_a_rejected_document_write_is_undone_on_disk_and_in_memory(self, jane_doe_dir):
        """The compensating write: for the document only, and only one."""
        prof = ResearcherProfile.from_files(jane_doe_dir)
        before = (jane_doe_dir / "profile.jsonld").read_text()
        original_field = prof.metadata.field
        prof.add_pre_commit_hook(_raise)

        with pytest.raises(WriteHookError):
            prof.save_profile(prof.metadata.model_copy(update={"field": "Genomics"}))

        assert (jane_doe_dir / "profile.jsonld").read_text() == before
        assert prof.metadata.field == original_field

    def test_raising_hook_does_NOT_restore_a_soul_write(self, jane_doe_dir):
        """Pins the stated limitation: a soul write a hook rejects stays on disk (why a transactional store exists)."""
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.add_pre_commit_hook(_raise)

        with pytest.raises(WriteHookError):
            prof.save_soul("# soul the hook rejected\n")

        assert (jane_doe_dir / "personality" / "SOUL.md").read_text().startswith("# soul the hook")


def test_a_failure_inside_a_grouped_unit_suppresses_the_hooks(jane_doe_dir):
    prof = ResearcherProfile.from_files(jane_doe_dir)
    seen: list[WriteContext] = []
    prof.add_pre_commit_hook(_record(seen))

    with pytest.raises(RuntimeError):
        with prof.write_unit("create"):
            prof.save_soul("# soul\n")
            raise RuntimeError("caller gave up")

    assert seen == []


class TestReadOnlyBackends:
    def test_write_unit_refuses_before_yielding(self, jane_doe_dir):
        prof = ResearcherProfile.from_url("https://example.org/profiles/jane-doe")
        seen: list[WriteContext] = []
        prof.add_pre_commit_hook(_record(seen))
        with pytest.raises(ProfileWriteError):
            with prof.write_unit("document"):
                pass  # pragma: no cover
        assert seen == []


class TestStoreRegistration:
    def test_the_store_threads_hooks_onto_every_profile(self, fixture_profiles_root):
        cache = FilesystemProfileStore(fixture_profiles_root("jane-doe"))
        seen: list[WriteContext] = []
        # Registered BEFORE the first get: the profile is born with the hook.
        cache.add_pre_commit_hook(_record(seen))
        cache.get("jane-doe").save_soul("# soul\n")
        assert len(seen) == 1

    def test_a_write_that_never_touches_a_route_still_fires(self, fixture_profiles_root):
        """The structural payoff: no route, no request, still a hook."""
        cache = FilesystemProfileStore(fixture_profiles_root("jane-doe"))
        seen: list[WriteContext] = []
        cache.add_pre_commit_hook(_record(seen))
        prof = cache.get("jane-doe")
        prof.papers  # noqa: B018 - force the lazy load
        prof.save_papers()
        assert [c.kind for c in seen] == ["papers"]
        assert seen[0].request is None


class TestPostCommitHooks:
    """Fire-and-forget notifications that run AFTER the write already landed."""

    def test_fires_after_commit_and_never_aborts(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        seen: list[WriteContext] = []
        prof.add_post_commit_hook(_record(seen))

        prof.save_soul("# a new soul\n")

        assert [c.kind for c in seen] == ["soul"]
        assert seen[0].profile is prof

    def test_a_raising_post_commit_hook_is_swallowed_and_the_next_runs(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        ran: list[str] = []

        def boom(ctx: WriteContext) -> None:
            ran.append("boom")
            raise RuntimeError("the downstream index is down")

        prof.add_post_commit_hook(boom)
        prof.add_post_commit_hook(lambda ctx: ran.append("second"))
        # No exception, unlike a raising PRE-commit hook (WriteHookError).
        prof.save_soul("# soul\n")
        assert ran == ["boom", "second"]
        assert (jane_doe_dir / "personality" / "SOUL.md").read_text() == "# soul\n"

    def test_a_pre_commit_abort_means_no_post_commit_run(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        post_seen: list[WriteContext] = []
        prof.add_pre_commit_hook(_raise)
        prof.add_post_commit_hook(_record(post_seen))

        with pytest.raises(WriteHookError):
            prof.save_soul("# soul the pre-commit hook rejected\n")

        assert post_seen == []


def test_document_and_soul_are_the_two_halves_of_the_content_hash(jane_doe_dir):
    """The digest spans exactly the surface a patch can touch."""
    prof = ResearcherProfile.from_files(jane_doe_dir)
    h = hashlib.sha256()
    h.update(canonical_dumps(json.loads((jane_doe_dir / "profile.jsonld").read_text())).encode())
    h.update(b"\x00")
    h.update((jane_doe_dir / "personality" / "SOUL.md").read_text().encode())
    assert prof.content_hash() == f"sha256:{h.hexdigest()}"
