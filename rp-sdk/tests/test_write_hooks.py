"""The write unit and the ``pre_commit_hook`` hook, on the filesystem backend.

A write hook is a callback the SDK runs at the boundary of a profile write:
``prof.add_pre_commit_hook(fn)`` registers one, and it receives a
``WriteContext`` describing the pending write (defined in ``write_unit.py``).

Everything here is about *when* a hook runs relative to a write, what it can
see when it does, and what happens when it fails. The filesystem backend has no
transaction, so it is also the place the honest limits of that get pinned down:
``ctx.atomic`` is False, and a raising hook does not undo the write beyond one
compensating write of the profile document.
"""

import json

import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.errors import ProfileWriteError, WriteHookError
from researcher_profiles.profile.write_unit import WriteContext


def _record(sink):
    def hook(ctx: WriteContext) -> None:
        sink.append(ctx)

    return hook


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

    def test_filesystem_backend_is_not_atomic(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        seen: list[WriteContext] = []
        prof.add_pre_commit_hook(_record(seen))
        prof.save_soul("# soul\n")
        assert seen[0].session is None
        assert seen[0].atomic is False


class TestOrdering:
    """Derived state is refreshed BEFORE the hooks. That is the whole point."""

    def test_hook_observes_post_write_content_hash(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        before = prof.content_hash()
        observed: list[str] = []
        prof.add_pre_commit_hook(lambda ctx: observed.append(ctx.profile.content_hash()))

        prof.save_profile(prof.metadata.model_copy(update={"field": "Genomics"}))

        assert observed[0] != before
        assert observed[0] == prof.content_hash()

    def test_soul_only_write_changes_the_content_hash(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        before = prof.content_hash()
        observed: list[str] = []
        prof.add_pre_commit_hook(lambda ctx: observed.append(ctx.profile.content_hash()))

        prof.save_soul("# a genuinely different soul\n")

        assert observed[0] != before
        assert observed[0] == prof.content_hash()

    def test_hooks_run_in_registration_order(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        order: list[str] = []
        prof.add_pre_commit_hook(lambda ctx: order.append("first"))
        prof.add_pre_commit_hook(lambda ctx: order.append("second"))
        prof.save_soul("# soul\n")
        assert order == ["first", "second"]


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

    def test_raising_hook_restores_the_profile_document(self, jane_doe_dir):
        """The compensating write: for the document only, and only one."""
        prof = ResearcherProfile.from_files(jane_doe_dir)
        before = (jane_doe_dir / "profile.jsonld").read_text()
        prof.add_pre_commit_hook(lambda ctx: (_ for _ in ()).throw(RuntimeError("no")))

        with pytest.raises(WriteHookError):
            prof.save_profile(prof.metadata.model_copy(update={"field": "Genomics"}))

        assert (jane_doe_dir / "profile.jsonld").read_text() == before

    def test_raising_hook_does_NOT_restore_a_soul_write(self, jane_doe_dir):
        """Documents the stated limitation rather than leaving it to be found.

        The filesystem backend compensates for the profile document only. A
        soul write that a hook rejects stays on disk. This is not a bug to fix
        here; it is why a transactional store exists.
        """
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.add_pre_commit_hook(lambda ctx: (_ for _ in ()).throw(RuntimeError("no")))

        with pytest.raises(WriteHookError):
            prof.save_soul("# soul the hook rejected\n")

        assert (jane_doe_dir / "personality" / "SOUL.md").read_text().startswith("# soul the hook")

    def test_in_memory_cache_is_not_updated_when_the_unit_fails(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        original_field = prof.metadata.field
        prof.add_pre_commit_hook(lambda ctx: (_ for _ in ()).throw(RuntimeError("no")))
        with pytest.raises(WriteHookError):
            prof.save_profile(prof.metadata.model_copy(update={"field": "Genomics"}))
        assert prof.metadata.field == original_field


class TestNesting:
    def test_nested_units_fire_hooks_once(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        seen: list[WriteContext] = []
        prof.add_pre_commit_hook(_record(seen))

        with prof.write_unit("create"):
            prof.save_soul("# soul\n")
            prof.save_expertise("# expertise\n")

        assert len(seen) == 1
        assert seen[0].kind == "create"

    def test_a_failure_inside_a_grouped_unit_suppresses_the_hooks(self, jane_doe_dir):
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
        from researcher_profiles.store import FilesystemProfileStore

        root = fixture_profiles_root("jane-doe")
        cache = FilesystemProfileStore(root)
        seen: list[WriteContext] = []
        # Registered BEFORE the first get: the profile is born with the hook.
        cache.add_pre_commit_hook(_record(seen))
        cache.get("jane-doe").save_soul("# soul\n")
        assert len(seen) == 1

    def test_registration_after_a_get_still_reaches_the_cached_profile(self, fixture_profiles_root):
        from researcher_profiles.store import FilesystemProfileStore

        root = fixture_profiles_root("jane-doe")
        cache = FilesystemProfileStore(root)
        prof = cache.get("jane-doe")
        seen: list[WriteContext] = []
        cache.add_pre_commit_hook(_record(seen))
        prof.save_soul("# soul\n")
        assert len(seen) == 1

    def test_a_write_that_never_touches_a_route_still_fires(self, fixture_profiles_root):
        """The structural payoff: no route, no request, still a hook."""
        from researcher_profiles.store import FilesystemProfileStore

        root = fixture_profiles_root("jane-doe")
        cache = FilesystemProfileStore(root)
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

    def test_raising_hook_is_swallowed_not_propagated(self, jane_doe_dir, caplog):
        prof = ResearcherProfile.from_files(jane_doe_dir)

        def exploding_hook(ctx: WriteContext) -> None:
            raise RuntimeError("the downstream index is down")

        prof.add_post_commit_hook(exploding_hook)
        # No exception, unlike a raising PRE-commit hook (WriteHookError).
        prof.save_soul("# soul\n")
        assert (jane_doe_dir / "personality" / "SOUL.md").read_text() == "# soul\n"

    def test_one_failing_hook_does_not_stop_the_next(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        ran: list[str] = []

        def boom(ctx: WriteContext) -> None:
            ran.append("boom")
            raise RuntimeError("no")

        prof.add_post_commit_hook(boom)
        prof.add_post_commit_hook(lambda ctx: ran.append("second"))
        prof.save_soul("# soul\n")
        assert ran == ["boom", "second"]

    def test_hooks_run_in_registration_order(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        order: list[str] = []
        prof.add_post_commit_hook(lambda ctx: order.append("first"))
        prof.add_post_commit_hook(lambda ctx: order.append("second"))
        prof.save_soul("# soul\n")
        assert order == ["first", "second"]

    def test_a_pre_commit_abort_means_no_post_commit_run(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        post_seen: list[WriteContext] = []
        prof.add_pre_commit_hook(lambda ctx: (_ for _ in ()).throw(RuntimeError("no")))
        prof.add_post_commit_hook(_record(post_seen))

        with pytest.raises(WriteHookError):
            prof.save_soul("# soul the pre-commit hook rejected\n")

        assert post_seen == []

    def test_nested_units_fire_once_after_the_single_commit(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        seen: list[WriteContext] = []
        prof.add_post_commit_hook(_record(seen))

        with prof.write_unit("create"):
            prof.save_soul("# soul\n")
            prof.save_expertise("# expertise\n")

        assert len(seen) == 1
        assert seen[0].kind == "create"

    def test_store_registration_reaches_cached_and_future_profiles(self, fixture_profiles_root):
        from researcher_profiles.store import FilesystemProfileStore

        root = fixture_profiles_root("jane-doe")
        cache = FilesystemProfileStore(root)
        prof = cache.get("jane-doe")
        seen: list[WriteContext] = []
        cache.add_post_commit_hook(_record(seen))
        prof.save_soul("# soul\n")
        assert len(seen) == 1


def test_document_and_soul_are_the_two_halves_of_the_content_hash(jane_doe_dir):
    """The digest spans exactly the surface a patch can touch."""
    import hashlib

    from researcher_profiles.schema.jsonld import canonical_dumps

    prof = ResearcherProfile.from_files(jane_doe_dir)
    h = hashlib.sha256()
    h.update(canonical_dumps(json.loads((jane_doe_dir / "profile.jsonld").read_text())).encode())
    h.update(b"\x00")
    h.update((jane_doe_dir / "personality" / "SOUL.md").read_text().encode())
    assert prof.content_hash() == f"sha256:{h.hexdigest()}"
