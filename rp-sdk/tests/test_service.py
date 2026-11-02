"""Permission checks in the service functions, called directly (routes and MCP tools are thin adapters).

A fake :class:`Hooks` stands in for a host: an edit gate, a write-scope check, and a ``record_edit`` recorder.
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


def _gate(caller, prof, *, read_ok=False, ref=None):  # noqa: ARG001
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


@pytest.mark.parametrize(
    "caller, call, slug, args, exc",
    [
        (STRANGER, svc.edit_metadata, SLUG, ({"field": "Genomics"},), Forbidden),
        (Caller(), svc.add_work, SLUG, ({"paper_id": "x2026", "name": "X"},), Unauthenticated),
        (OWNER, svc.edit_metadata, "nobody", ({"field": "X"},), NotFound),
        (SUMMARY_ONLY, svc.edit_metadata, SLUG, ({"soul": "# new\n"},), InsufficientScope),
        (OWNER, svc.edit_metadata, SLUG, ({"no_such_field": 1},), Invalid),
        (OWNER, svc.add_work, SLUG, ({"name": "No id"},), Invalid),
        (OWNER, svc.edit_work, SLUG, ("nope", {"doi": "10.1/x"}), NotFound),
    ],
    ids=[
        "forbidden",
        "unauthenticated",
        "no-such-profile",
        "soul-needs-its-own-part",
        "unknown-field",
        "work-needs-an-id",
        "no-such-work",
    ],
)
def test_a_refused_call_raises_and_writes_nothing(service, recorder, caller, call, slug, args, exc):
    before = _hash(service)
    with pytest.raises(exc):
        call(service, caller, slug, *args)
    assert _hash(service) == before
    assert recorder.calls == []


def test_an_allowed_edit_writes_records_and_invalidates(service, recorder):
    service.graph = object()
    out = svc.edit_metadata(service, OWNER, SLUG, {"field": "Genomics", "summary": "S"})
    assert out.updated == ["field", "summary"]
    assert service.store.get(SLUG).metadata.field == "Genomics"
    assert recorder.calls == [(OWNER, SLUG, "metadata", ["field", "summary"], out.content_hash)]
    assert out.content_hash == _hash(service)
    assert service.graph is None


class TestEditGate:
    def test_bare_service_admits_only_the_operator(self, store):
        bare = Service(store)
        with pytest.raises(Unauthenticated):
            svc.edit_metadata(bare, Caller(), SLUG, {"field": "Genomics"})
        assert svc.edit_metadata(bare, Caller(is_operator=True), SLUG, {"field": "Genomics"})

    def test_open_mode_admits_anyone(self, store):
        assert svc.edit_metadata(Service(store, open_mode=True), Caller(), SLUG, {"field": "X"})


class TestWriteScope:
    def test_missing_part_is_insufficient_scope(self, service):
        with pytest.raises(InsufficientScope) as e:
            svc.add_work(service, SUMMARY_ONLY, SLUG, {"paper_id": "x2026", "name": "X"})
        assert e.value.missing == ["works"]
        assert all(p.paper_id != "x2026" for p in service.store.get(SLUG).papers)

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


class TestRecordEdit:
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


class TestHttpMapper:
    """The one place typed errors become HTTP: the shapes the SPA and ``rp`` read."""

    def _reply(self, exc):
        import json

        from researcher_profiles.api import service_error_response

        r = service_error_response(None, exc)
        return r.status_code, json.loads(r.body)["detail"], r.headers

    def test_conflict_carries_the_current_version_header(self):
        status, detail, headers = self._reply(Conflict("stale", current="v2", kind="paper"))
        assert status == 409
        assert detail == {"error": "conflict", "current": "v2", "message": "stale"}
        assert headers["X-RP-Paper-Version"] == "v2"

    def test_insufficient_scope_names_the_parts(self):
        status, detail, _ = self._reply(
            InsufficientScope({"profiles:write"}, required=["summary"], missing=["summary"])
        )
        assert status == 403
        assert detail["error"] == "insufficient_access"
        assert detail["required"] == ["summary"] and detail["missing"] == ["summary"]

    @pytest.mark.parametrize(
        "exc, status, detail",
        [
            (
                Conflict("x", kind="exists", detail="you already have one"),
                409,
                "you already have one",
            ),
            (Forbidden("x", detail={"error": "not_delegable"}), 403, {"error": "not_delegable"}),
            (
                Invalid("no", code="unknown_section", valid=["a"]),
                400,
                {"error": "unknown_section", "message": "no", "valid": ["a"]},
            ),
            (Invalid("bad patch"), 400, "bad patch"),
            (NotFound("profile 'x' not found"), 404, "profile 'x' not found"),
            (Unauthenticated("login required"), 401, "login required"),
        ],
        ids=[
            "conflict-detail",
            "forbidden-detail",
            "invalid-code",
            "invalid-plain",
            "not-found",
            "unauthenticated",
        ],
    )
    def test_service_error_maps_to_http(self, exc, status, detail):
        assert self._reply(exc)[:2] == (status, detail)


def test_a_gate_not_found_names_the_callers_ref(store):
    def hide(caller, prof, *, read_ok=False, ref=None):  # noqa: ARG001
        raise NotFound(f"profile {prof.slug!r} not found")

    service = Service(store, Hooks(edit_gate=hide))
    rid = store.get(SLUG).rid
    with pytest.raises(NotFound) as e:
        svc.edit_metadata(service, OWNER, rid, {"field": "X"})
    assert e.value.what == f"profile {rid!r} not found"


class TestSeedPapers:
    """Interest seeds: gated like every edit, deduped, hidden from the public by default."""

    SEED_OWNER = Caller(scopes=frozenset({"user:owner", "write:seed_papers"}))

    def test_add_dedupes_reports_and_records(self, service, recorder):
        jane = service.store.get(SLUG)
        authored = jane.edit.patch_work(PAPER, {"doi": "10.1093/jane.1"})
        out = svc.add_seed_papers(
            service,
            self.SEED_OWNER,
            SLUG,
            [
                {"title": "New seed", "openalex_id": "W77"},
                {"title": "Again", "openalex_id": "https://openalex.org/W77"},
                {"title": "Hers", "doi": authored.doi},
            ],
            added_via="interview",
        )
        assert (out.added, out.already_seed, out.already_authored, out.over_cap) == (
            ["W77"],
            ["W77"],
            [f"doi:{authored.doi}"],
            0,
        )
        assert [a for _c, _s, a, _f, _h in recorder.calls] == ["seed_papers"]
        rows = svc.list_seed_papers(service, Caller(is_operator=True), SLUG)
        assert [(r["seed_id"], r["added_via"]) for r in rows] == [("W77", "interview")]
        assert len(service.store.get(SLUG).papers) == len(jane.papers)  # never a work

    def test_needs_the_seed_papers_write_scope(self, service):
        with pytest.raises(InsufficientScope):
            svc.add_seed_papers(
                service, OWNER, SLUG, [{"title": "S", "openalex_id": "W1"}], added_via="owner"
            )

    def test_a_record_without_an_identifier_is_invalid(self, service):
        with pytest.raises(Invalid):
            svc.add_seed_papers(service, self.SEED_OWNER, SLUG, [{"title": "S"}], added_via="owner")

    def test_the_public_cannot_list_seeds_by_default(self, service):
        svc.add_seed_papers(
            service, self.SEED_OWNER, SLUG, [{"title": "S", "openalex_id": "W1"}], added_via="owner"
        )
        with pytest.raises(NotFound):
            svc.list_seed_papers(service, Caller(), SLUG)

    def test_remove(self, service):
        svc.add_seed_papers(
            service,
            self.SEED_OWNER,
            SLUG,
            [{"title": "S", "doi": "10.1093/s.1"}],
            added_via="owner",
        )
        out = svc.remove_seed_paper(service, self.SEED_OWNER, SLUG, "doi:10.1093/s.1")
        assert out.updated == ["doi:10.1093/s.1"]
        assert service.store.get(SLUG).seed_papers == []
        with pytest.raises(NotFound):
            svc.remove_seed_paper(service, self.SEED_OWNER, SLUG, "doi:10.1093/s.1")
