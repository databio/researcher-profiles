"""Permission checks in the service functions, called directly: no HTTP, no MCP.

The routes and a host's MCP tools are thin adapters over
``researcher_profiles.api.service``; every check lives in a function, so this
is where permissions are tested. A fake :class:`Hooks` stands in for a host:
an edit gate that refuses a chosen caller, a write-scope check that refuses a
chosen part, and a ``record_edit`` that records what it was told.
"""

from dataclasses import dataclass

import pytest

from researcher_profiles import (
    Conflict,
    Forbidden,
    InsufficientScope,
    Invalid,
    NotFound,
    Unauthenticated,
)
from researcher_profiles.api import service as svc
from researcher_profiles.api.caller import Caller
from researcher_profiles.api.deps import TierFloor
from researcher_profiles.api.hooks import Hooks
from researcher_profiles.api.service import Service
from researcher_profiles.store import FilesystemProfileStore

SLUG = "jane-doe"
PAPER = "doe2016example"

OWNER = Caller(scopes=frozenset({"user:owner", "write:metadata", "write:works", "write:soul"}))
STRANGER = Caller(scopes=frozenset({"user:stranger"}))
SUMMARY_ONLY = Caller(scopes=frozenset({"user:owner", "write:metadata"}))


@dataclass
class Recorder:
    calls: list

    def __call__(self, caller, prof, action, fields, content_hash):
        self.calls.append((caller, prof.slug, action, fields, content_hash))


def _gate(caller, prof, *, read_ok=False):  # noqa: ARG001
    if not caller.scopes:
        raise Unauthenticated("login required")
    if "user:owner" not in caller.scopes:
        raise Forbidden("you may not edit this profile")


def _write_scope(caller, prof, action, detail):  # noqa: ARG001
    if f"write:{action}" not in caller.scopes:
        raise InsufficientScope({f"profiles:{action}"}, missing=[action])


@pytest.fixture
def store(fixture_profiles_root):
    return FilesystemProfileStore(fixture_profiles_root(SLUG))


@pytest.fixture
def recorder():
    return Recorder(calls=[])


@pytest.fixture
def service(store, recorder):
    hooks = Hooks(edit_gate=_gate, write_scope=_write_scope, record_edit=recorder)
    return Service(store, hooks)


def _hash(service):
    return service.store.content_hash(SLUG)


def _version(service, paper_id=PAPER):
    return svc.get_paper(service, Caller(is_operator=True), SLUG, paper_id).version


class TestEditGate:
    def test_allowed_caller_edits(self, service):
        out = svc.edit_metadata(service, OWNER, SLUG, {"field": "Genomics"})
        assert out.updated == ["field"]
        assert service.store.get(SLUG).metadata.field == "Genomics"

    def test_wrong_caller_is_forbidden(self, service):
        with pytest.raises(Forbidden):
            svc.edit_metadata(service, STRANGER, SLUG, {"field": "Genomics"})
        assert service.store.get(SLUG).metadata.field != "Genomics"

    def test_no_caller_is_unauthenticated(self, service):
        with pytest.raises(Unauthenticated):
            svc.add_work(service, Caller(), SLUG, {"paper_id": "x2026", "name": "X"})

    def test_bare_service_admits_only_the_operator(self, store):
        bare = Service(store)
        with pytest.raises(Unauthenticated):
            svc.edit_metadata(bare, Caller(), SLUG, {"field": "Genomics"})
        assert svc.edit_metadata(bare, Caller(is_operator=True), SLUG, {"field": "Genomics"})

    def test_open_mode_admits_anyone(self, store):
        assert svc.edit_metadata(Service(store, open_mode=True), Caller(), SLUG, {"field": "X"})

    def test_a_missing_profile_is_not_found(self, service):
        with pytest.raises(NotFound):
            svc.edit_metadata(service, OWNER, "nobody", {"field": "X"})


class TestWriteScope:
    def test_missing_part_is_insufficient_scope(self, service):
        with pytest.raises(InsufficientScope) as e:
            svc.add_work(service, SUMMARY_ONLY, SLUG, {"paper_id": "x2026", "name": "X"})
        assert e.value.missing == ["works"]
        assert all(p.paper_id != "x2026" for p in service.store.get(SLUG).papers)

    def test_soul_needs_its_own_part(self, service):
        with pytest.raises(InsufficientScope):
            svc.edit_metadata(service, SUMMARY_ONLY, SLUG, {"soul": "# new\n"})

    def test_the_detail_names_the_paper_and_fields(self, service):
        seen = []

        def spy(caller, prof, action, detail):  # noqa: ARG001
            seen.append((action, detail))

        service.hooks.write_scope = spy
        svc.edit_work(service, OWNER, SLUG, PAPER, {"doi": "10.1/x"})
        assert seen == [("works", {"paper_id": PAPER, "fields": ["doi"]})]


class TestConflicts:
    def test_stale_base_hash_carries_current(self, service):
        current = _hash(service)
        with pytest.raises(Conflict) as e:
            svc.edit_metadata(service, OWNER, SLUG, {"field": "X"}, base_hash="sha256:stale")
        assert e.value.kind == "profile"
        assert e.value.current == current

    def test_matching_base_hash_writes(self, service):
        before = _hash(service)
        out = svc.edit_metadata(service, OWNER, SLUG, {"field": "X"}, base_hash=before)
        assert out.content_hash == _hash(service) != before

    def test_stale_base_version_carries_current(self, service):
        current = _version(service)
        with pytest.raises(Conflict) as e:
            svc.edit_work(service, OWNER, SLUG, PAPER, {"doi": "10.1/x"}, base_version="stale")
        assert e.value.kind == "paper"
        assert e.value.current == current
        with pytest.raises(Conflict):
            svc.remove_work(service, OWNER, SLUG, PAPER, base_version="stale")

    def test_add_work_never_overwrites(self, service):
        with pytest.raises(Conflict) as e:
            svc.add_work(service, OWNER, SLUG, {"paper_id": PAPER, "name": "Clobber"})
        assert e.value.kind == "exists"


class TestInvalid:
    def test_unknown_field_is_invalid(self, service):
        with pytest.raises(Invalid):
            svc.edit_metadata(service, OWNER, SLUG, {"no_such_field": 1})

    def test_a_work_needs_a_paper_id(self, service):
        with pytest.raises(Invalid):
            svc.add_work(service, OWNER, SLUG, {"name": "No id"})

    def test_an_unknown_work_is_not_found(self, service):
        with pytest.raises(NotFound):
            svc.edit_work(service, OWNER, SLUG, "nope", {"doi": "10.1/x"})


class TestRecordEdit:
    def test_called_with_action_fields_and_the_post_write_hash(self, service, recorder):
        out = svc.edit_metadata(service, OWNER, SLUG, {"field": "Genomics", "summary": "S"})
        assert recorder.calls == [(OWNER, SLUG, "metadata", ["field", "summary"], out.content_hash)]
        assert out.content_hash == _hash(service)

    def test_one_row_per_action(self, service, recorder):
        svc.edit_metadata(service, OWNER, SLUG, {"field": "G", "soul": "# s\n"})
        assert [c[2] for c in recorder.calls] == ["metadata", "soul"]

    def test_work_edits_record(self, service, recorder):
        svc.add_work(service, OWNER, SLUG, {"paper_id": "x2026", "name": "X"})
        svc.edit_work(service, OWNER, SLUG, "x2026", {"doi": "10.1/x"})
        svc.remove_work(service, OWNER, SLUG, "x2026")
        assert [(c[2], c[3]) for c in recorder.calls] == [
            ("works", ["*"]),
            ("works", ["doi"]),
            ("works", []),
        ]

    def test_a_refused_edit_records_nothing(self, service, recorder):
        with pytest.raises(Forbidden):
            svc.edit_metadata(service, STRANGER, SLUG, {"field": "X"})
        assert recorder.calls == []


class TestReads:
    def test_a_hidden_profile_is_not_found(self, store):
        hooks = Hooks(profile_tier_floor=lambda caller, prof, slug: TierFloor("limited"))
        hidden = Service(store, hooks)
        with pytest.raises(NotFound) as e:
            svc.get_profile(hidden, Caller(), SLUG)
        assert e.value.what == f"profile {SLUG!r} not found"
        assert svc.get_profile(hidden, Caller(is_operator=True), SLUG).slug == SLUG

    def test_a_missing_profile_reads_like_a_hidden_one(self, service):
        with pytest.raises(NotFound) as e:
            svc.get_profile(service, Caller(), "nobody")
        assert e.value.what == "profile 'nobody' not found"

    def test_an_unknown_section_is_invalid_with_the_valid_names(self, service):
        with pytest.raises(Invalid) as e:
            svc.read_profile_text(service, Caller(is_operator=True), SLUG, section="nope")
        assert e.value.code == "unknown_section"
        assert e.value.valid == ["soul", "expertise"]

    def test_a_foreign_cursor_is_invalid(self, service):
        with pytest.raises(Invalid) as e:
            svc.list_papers(service, Caller(is_operator=True), SLUG, cursor="bm90LWEtY3Vyc29y")
        assert e.value.code == "cursor_mismatch"


class TestStoreFor:
    def test_reads_use_the_view_and_edits_do_not(self, store, recorder):
        seen = []

        def store_for(caller, inner):
            seen.append(caller)
            return inner

        hooks = Hooks(edit_gate=_gate, write_scope=_write_scope, record_edit=recorder)
        hooks.store_for = store_for
        service = Service(store, hooks)
        svc.get_profile(service, OWNER, SLUG)
        svc.list_papers(service, OWNER, SLUG)
        assert seen == [OWNER, OWNER]
        seen.clear()
        svc.edit_metadata(service, OWNER, SLUG, {"field": "X"})
        svc.edit_work(service, OWNER, SLUG, PAPER, {"doi": "10.1/x"})
        assert seen == []


class TestInvalidate:
    def test_drops_the_graph_and_the_temp_dir(self, service):
        cleaned = []

        class _Tmp:
            def cleanup(self):
                cleaned.append(True)

        service.graph = object()
        service.registry_tempdir = _Tmp()
        before = service.store.generation
        service.invalidate(SLUG)
        assert service.graph is None
        assert service.registry_tempdir is None
        assert cleaned == [True]
        assert service.store.generation > before

    def test_an_edit_invalidates(self, service):
        service.graph = object()
        svc.edit_metadata(service, OWNER, SLUG, {"field": "X"})
        assert service.graph is None
