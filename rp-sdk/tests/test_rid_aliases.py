"""Finding profiles by email (``rids_with_email``), and retiring one into another (``merge_into``).
A merge leaves an alias, so the retired rid and slug keep resolving like a live ref.
"""

from pathlib import Path

import pytest
from sqlmodel import select

from researcher_profiles.errors import ProfileWriteError, WriteHookError
from researcher_profiles.profile import ResearcherProfile
from researcher_profiles.resolve import resolve_person
from researcher_profiles.schema import ProfileDocument
from researcher_profiles.store import (
    FilesystemProfileStore,
    ProfileNotFoundError,
    RetiredRidError,
)
from researcher_profiles.store.db import RidAliasRow
from researcher_profiles.store.http import HttpProfileStore

from .factories import build_profile_dir, tar_gz_of_dir

ORCID_A = "0000-0002-1825-0097"
ORCID_B = "0000-0004-4600-113X"
LOCAL_L = "local:lee-local-abc123"
LOCAL_M = "local:mia-local-def456"


def _doc(rid: str, name: str, email: str | None = None) -> ProfileDocument:
    provenance = "synthetic" if rid.startswith("local:") else "third_party"
    return ProfileDocument(name=name, rid=rid, provenance=provenance, email=email)


def _all_aliases():
    return select(RidAliasRow)


def _stage(tmp_path: Path, rid: str, name: str, **extra) -> Path:
    provenance = "synthetic" if rid.startswith("local:") else "third_party"
    return build_profile_dir(
        tmp_path / f"stage-{rid.replace(':', '-')}",
        name=name,
        rid=rid,
        level="lite",
        papers=False,
        personality=False,
        summaries=False,
        provenance=provenance,
        **extra,
    )


def _merge(store, retired, tmp_path, survivor_rid, survivor_name, survivor_slug):
    return store.merge_into(
        retired,
        _stage(tmp_path, survivor_rid, survivor_name),
        survivor_rid=survivor_rid,
        survivor_slug=survivor_slug,
        build_missing_index=False,
    )


@pytest.fixture
def lee_pair(empty_sql_store):
    """A SQL store holding a local ``lee`` and an ORCID ``lee-orcid``, ready to merge."""
    empty_sql_store.create(_doc(LOCAL_L, "Lee Local"), slug="lee")
    empty_sql_store.create(_doc(ORCID_A, "Lee Orcid"), slug="lee-orcid")
    return empty_sql_store


class TestRidsWithEmail:
    @pytest.mark.parametrize(
        "stored, query, expected",
        [
            ([(LOCAL_L, "  Lee@Example.ORG ")], "lee@example.org", [LOCAL_L]),
            ([(LOCAL_L, "  Lee@Example.ORG ")], " LEE@example.org", [LOCAL_L]),
            ([(LOCAL_L, "lee@example.org")], "someone@example.org", []),
            ([(LOCAL_L, "lee@example.org")], "", []),
            (
                [(LOCAL_M, "shared@example.org"), (LOCAL_L, "Shared@example.org")],
                "shared@example.org",
                sorted([LOCAL_L, LOCAL_M]),
            ),
            ([(LOCAL_L, "jane@gmail.com")], "j.ane@gmail.com", []),
        ],
        ids=["case", "outer-space", "no-match", "empty-query", "two-sorted", "no-dot-folding"],
    )
    def test_rids_with_email(self, any_store, stored, query, expected):
        for rid, email in stored:
            any_store.create(_doc(rid, "Person " + rid[6:9], email), slug=rid[6:9])
        assert any_store.rids_with_email(query) == expected

    def test_http_store_refuses(self):
        store = HttpProfileStore("https://profiles.example.org")
        with pytest.raises(NotImplementedError):
            store.rids_with_email("lee@example.org")


class TestMergeInto:
    def test_old_rid_and_old_slug_resolve_to_the_survivor(self, lee_pair, tmp_path):
        store = lee_pair
        seen = []
        store.add_pre_commit_hook(
            lambda ctx: (
                seen.append((ctx.kind, ctx.rid, ctx.retired_rid, ctx.retired_slug))
                if ctx.kind == "merge"
                else None
            )
        )
        result = _merge(store, "lee", tmp_path, ORCID_A, "Lee Orcid", "lee-orcid")
        assert seen == [("merge", ORCID_A, LOCAL_L, "lee")]
        assert result.rid == ORCID_A
        assert store.rid_for(LOCAL_L) == ORCID_A
        assert store.rid_for("lee") == ORCID_A
        assert store.get("lee").slug == "lee-orcid"
        assert store.resolve_slug(LOCAL_L) == "lee-orcid"
        assert store.exists(LOCAL_L)
        assert store.successor_of(LOCAL_L) == ORCID_A
        assert store.successor_of("lee") == ORCID_A
        assert store.successor_of(ORCID_A) is None
        assert store.list_slugs() == ["lee-orcid"]
        assert store.alias_slugs() == {"lee"}

    def test_a_convert_keeps_the_slug_and_aliases_only_the_rid(self, empty_sql_store, tmp_path):
        store = empty_sql_store
        store.create(_doc(LOCAL_L, "Lee Local"), slug="lee")
        _merge(store, LOCAL_L, tmp_path, ORCID_A, "Lee Local", "lee")
        assert store.get("lee").rid == ORCID_A
        assert store.rid_for(LOCAL_L) == ORCID_A
        assert store.alias_slugs() == set()

    def test_chains_collapse_to_one_hop(self, lee_pair, tmp_path):
        store = lee_pair
        store.create(_doc(LOCAL_M, "Lee Two"), slug="lee-two")
        _merge(store, "lee", tmp_path, LOCAL_M, "Lee Two", "lee-two")
        _merge(store, "lee-two", tmp_path, ORCID_A, "Lee Orcid", "lee-orcid")
        with store.session() as s:
            aliases = {a.old_rid: a.successor_rid for a in s.exec(_all_aliases()).all()}
        assert aliases == {LOCAL_L: ORCID_A, LOCAL_M: ORCID_A}
        assert store.rid_for("lee") == ORCID_A

    def test_a_failing_hook_rolls_back_all_three_changes(self, lee_pair, tmp_path):
        store = lee_pair
        before = store.content_hash(ORCID_A)

        def _explode(ctx):
            if ctx.kind == "merge":
                raise RuntimeError("accounts re-key failed")

        store.add_pre_commit_hook(_explode)
        with pytest.raises(WriteHookError):
            _merge(store, "lee", tmp_path, ORCID_A, "Lee Orcid Renamed", "lee-orcid")
        assert store.get(LOCAL_L).slug == "lee"
        assert store.content_hash(ORCID_A) == before
        assert store.get(ORCID_A).name == "Lee Orcid"
        assert store.successor_of(LOCAL_L) is None
        assert store.alias_slugs() == set()

    @pytest.mark.parametrize(
        "retired, survivor_rid, survivor_slug, exc",
        [
            ("lee", LOCAL_L, "lee", ProfileWriteError),  # into itself
            ("nobody", ORCID_A, "lee-orcid", ProfileNotFoundError),
        ],
        ids=["into-itself", "missing-retired"],
    )
    def test_a_merge_with_a_bad_endpoint_is_refused(
        self, empty_sql_store, tmp_path, retired, survivor_rid, survivor_slug, exc
    ):
        empty_sql_store.create(_doc(LOCAL_L, "Lee Local"), slug="lee")
        with pytest.raises(exc):
            _merge(empty_sql_store, retired, tmp_path, survivor_rid, "Lee Local", survivor_slug)

    def test_resolve_person_answers_the_successor_and_mints_nothing(
        self, empty_sql_store, tmp_path
    ):
        store = empty_sql_store
        store.create(_doc(ORCID_B, "Lee Before"), slug="lee-before")
        store.create(_doc(ORCID_A, "Lee Orcid"), slug="lee-orcid")
        _merge(store, "lee-before", tmp_path, ORCID_A, "Lee Orcid", "lee-orcid")
        result = resolve_person(store, rid=ORCID_B, name="Lee Before")
        assert result.rid == ORCID_A
        assert result.created is False
        assert store.list_slugs() == ["lee-orcid"]

    def test_non_sql_stores_cannot_merge(self, tmp_path):
        store = FilesystemProfileStore(tmp_path / "root")
        with pytest.raises(NotImplementedError):
            store.merge_into("x", tmp_path, survivor_rid=ORCID_A, survivor_slug="x")
        assert store.successor_of("x") is None
        assert store.alias_slugs() == set()


class TestRetiredRidsStayRetired:
    """A write at a retired rid or slug would take it back from its survivor."""

    @pytest.fixture
    def merged(self, lee_pair, tmp_path):
        _merge(lee_pair, "lee", tmp_path, ORCID_A, "Lee Orcid", "lee-orcid")
        return lee_pair

    @pytest.mark.parametrize(
        "write",
        [
            lambda s, tp: s.put(
                ResearcherProfile.from_files(_stage(tp, LOCAL_L, "Mallory")), slug="fresh"
            ),
            lambda s, tp: s.commit_directory("fresh", _stage(tp, LOCAL_L, "Mallory")),
            lambda s, tp: s.commit_directory("lee", _stage(tp, LOCAL_M, "Mallory")),
            lambda s, tp: s.create(_doc(LOCAL_L, "Mallory"), slug="fresh"),
            lambda s, tp: s.create_bundle(_doc(LOCAL_L, "Mallory"), slug="fresh"),
            lambda s, tp: s.put_document("fresh", _doc(LOCAL_L, "Mallory")),
            lambda s, tp: s.put_document("lee", _doc(ORCID_A, "Lee Orcid")),
            lambda s, tp: (
                s.create(_doc(LOCAL_M, "Mia Local"), slug="mia"),
                _merge(s, "mia", tp, LOCAL_L, "Mallory", "fresh"),
            ),
        ],
        ids=[
            "put-rid",
            "commit-rid",
            "commit-slug",
            "create-rid",
            "create-bundle-rid",
            "put-document-rid",
            "put-document-slug",
            "merge-revive",
        ],
    )
    def test_writes_cannot_reuse_a_retired_handle(self, merged, tmp_path, write):
        with pytest.raises(RetiredRidError) as e:
            write(merged, tmp_path / "m")
        assert e.value.successor_rid == ORCID_A
        assert isinstance(e.value, ProfileWriteError)
        assert merged.rid_for(LOCAL_L) == merged.rid_for("lee") == ORCID_A
        assert not merged.exists("fresh")

    def test_a_merge_leaves_the_other_profile_alone(self, merged, tmp_path):
        merged.create(_doc(LOCAL_M, "Mia Local"), slug="mia")
        with pytest.raises(RetiredRidError):
            _merge(merged, "mia", tmp_path / "m", LOCAL_L, "Mallory", "mallory")
        assert merged.rid_for("mia") == LOCAL_M

    def test_http_push_is_a_409(self, merged, tmp_path, make_api_client):
        c = make_api_client(merged, token="op")
        auth = {"Authorization": "Bearer op"}
        staged = _stage(tmp_path / "m", LOCAL_L, "Mallory")
        r = c.put("/api/v1/profiles/mallory", content=tar_gz_of_dir(staged), headers=auth)
        assert r.status_code == 409, r.text
        assert "retired" in r.json()["detail"]
        body = {"name": "Mallory", "rid": LOCAL_L, "provenance": "synthetic"}
        r = c.put("/api/v1/profiles/mallory", json=body, headers=auth)
        assert r.status_code == 409, r.text
        assert merged.rid_for(LOCAL_L) == ORCID_A
        assert not merged.exists("mallory")
