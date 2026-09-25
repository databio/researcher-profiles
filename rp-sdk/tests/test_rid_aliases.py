"""Finding profiles by email, and retiring one profile into another.

``rids_with_email`` is how a management host learns which stored documents
carry an address. ``merge_into`` retires one profile into a survivor in one
write unit and leaves an alias behind, so the retired rid and slug keep
resolving everywhere a live ref would.
"""

from pathlib import Path

import pytest

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
from researcher_profiles.store.sql import SqlProfileStore

from .factories import build_profile_dir

ORCID_A = "0000-0002-1825-0097"
ORCID_B = "0000-0004-4600-113X"
LOCAL_L = "local:lee-local-abc123"
LOCAL_M = "local:mia-local-def456"


def _doc(rid: str, name: str, email: str | None = None) -> ProfileDocument:
    provenance = "synthetic" if rid.startswith("local:") else "third_party"
    return ProfileDocument(name=name, rid=rid, provenance=provenance, email=email)


@pytest.fixture
def sql_store() -> SqlProfileStore:
    store = SqlProfileStore("sqlite://")
    store.create_all()
    return store


@pytest.fixture(params=["filesystem", "sql"])
def any_store(request, tmp_path):
    if request.param == "filesystem":
        return FilesystemProfileStore(tmp_path / "root")
    store = SqlProfileStore("sqlite://")
    store.create_all()
    return store


class TestRidsWithEmail:
    def test_case_and_outer_space_are_ignored(self, any_store):
        any_store.create(_doc(LOCAL_L, "Lee Local", "  Lee@Example.ORG "), slug="lee")
        assert any_store.rids_with_email("lee@example.org") == [LOCAL_L]
        assert any_store.rids_with_email(" LEE@example.org") == [LOCAL_L]

    def test_no_match(self, any_store):
        any_store.create(_doc(LOCAL_L, "Lee Local", "lee@example.org"), slug="lee")
        assert any_store.rids_with_email("someone@example.org") == []
        assert any_store.rids_with_email("") == []

    def test_two_matches_are_both_returned_sorted(self, any_store):
        any_store.create(_doc(LOCAL_M, "Mia Local", "shared@example.org"), slug="mia")
        any_store.create(_doc(LOCAL_L, "Lee Local", "Shared@example.org"), slug="lee")
        assert any_store.rids_with_email("shared@example.org") == sorted([LOCAL_L, LOCAL_M])

    def test_no_dot_folding(self, any_store):
        any_store.create(_doc(LOCAL_L, "Lee Local", "jane@gmail.com"), slug="lee")
        assert any_store.rids_with_email("j.ane@gmail.com") == []

    def test_http_store_refuses(self):
        store = HttpProfileStore("https://profiles.example.org")
        with pytest.raises(NotImplementedError):
            store.rids_with_email("lee@example.org")


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


class TestMergeInto:
    def test_old_rid_and_old_slug_resolve_to_the_survivor(self, sql_store, tmp_path):
        sql_store.create(_doc(LOCAL_L, "Lee Local"), slug="lee")
        sql_store.create(_doc(ORCID_A, "Lee Orcid"), slug="lee-orcid")
        result = sql_store.merge_into(
            "lee",
            _stage(tmp_path, ORCID_A, "Lee Orcid"),
            survivor_rid=ORCID_A,
            survivor_slug="lee-orcid",
            build_missing_index=False,
        )
        assert result.rid == ORCID_A
        assert sql_store.rid_for(LOCAL_L) == ORCID_A
        assert sql_store.rid_for("lee") == ORCID_A
        assert sql_store.get("lee").slug == "lee-orcid"
        assert sql_store.resolve_slug(LOCAL_L) == "lee-orcid"
        assert sql_store.exists(LOCAL_L)
        assert sql_store.successor_of(LOCAL_L) == ORCID_A
        assert sql_store.successor_of("lee") == ORCID_A
        assert sql_store.successor_of(ORCID_A) is None
        assert sql_store.list_slugs() == ["lee-orcid"]
        assert sql_store.alias_slugs() == {"lee"}

    def test_a_convert_keeps_the_slug_and_aliases_only_the_rid(self, sql_store, tmp_path):
        sql_store.create(_doc(LOCAL_L, "Lee Local"), slug="lee")
        sql_store.merge_into(
            LOCAL_L,
            _stage(tmp_path, ORCID_A, "Lee Local"),
            survivor_rid=ORCID_A,
            survivor_slug="lee",
            build_missing_index=False,
        )
        assert sql_store.get("lee").rid == ORCID_A
        assert sql_store.rid_for(LOCAL_L) == ORCID_A
        assert sql_store.alias_slugs() == set()

    def test_chains_collapse_to_one_hop(self, sql_store, tmp_path):
        sql_store.create(_doc(LOCAL_L, "Lee Local"), slug="lee")
        sql_store.create(_doc(LOCAL_M, "Lee Two"), slug="lee-two")
        sql_store.create(_doc(ORCID_A, "Lee Orcid"), slug="lee-orcid")
        sql_store.merge_into(
            "lee",
            _stage(tmp_path, LOCAL_M, "Lee Two"),
            survivor_rid=LOCAL_M,
            survivor_slug="lee-two",
            build_missing_index=False,
        )
        sql_store.merge_into(
            "lee-two",
            _stage(tmp_path, ORCID_A, "Lee Orcid"),
            survivor_rid=ORCID_A,
            survivor_slug="lee-orcid",
            build_missing_index=False,
        )
        with sql_store.session() as s:
            aliases = {a.old_rid: a.successor_rid for a in s.exec(_all_aliases()).all()}
        assert aliases == {LOCAL_L: ORCID_A, LOCAL_M: ORCID_A}
        assert sql_store.rid_for("lee") == ORCID_A

    def test_hooks_see_a_merge_context(self, sql_store, tmp_path):
        seen = []
        sql_store.add_pre_commit_hook(
            lambda ctx: (
                seen.append((ctx.kind, ctx.rid, ctx.retired_rid, ctx.retired_slug))
                if ctx.kind == "merge"
                else None
            )
        )
        sql_store.create(_doc(LOCAL_L, "Lee Local"), slug="lee")
        sql_store.create(_doc(ORCID_A, "Lee Orcid"), slug="lee-orcid")
        sql_store.merge_into(
            "lee",
            _stage(tmp_path, ORCID_A, "Lee Orcid"),
            survivor_rid=ORCID_A,
            survivor_slug="lee-orcid",
            build_missing_index=False,
        )
        assert seen == [("merge", ORCID_A, LOCAL_L, "lee")]

    def test_a_failing_hook_rolls_back_all_three_changes(self, sql_store, tmp_path):
        sql_store.create(_doc(LOCAL_L, "Lee Local"), slug="lee")
        sql_store.create(_doc(ORCID_A, "Lee Orcid"), slug="lee-orcid")
        before = sql_store.content_hash(ORCID_A)

        def _explode(ctx):
            if ctx.kind == "merge":
                raise RuntimeError("accounts re-key failed")

        sql_store.add_pre_commit_hook(_explode)
        with pytest.raises(WriteHookError):
            sql_store.merge_into(
                "lee",
                _stage(tmp_path, ORCID_A, "Lee Orcid Renamed"),
                survivor_rid=ORCID_A,
                survivor_slug="lee-orcid",
                build_missing_index=False,
            )
        assert sql_store.get(LOCAL_L).slug == "lee"
        assert sql_store.content_hash(ORCID_A) == before
        assert sql_store.get(ORCID_A).name == "Lee Orcid"
        assert sql_store.successor_of(LOCAL_L) is None
        assert sql_store.alias_slugs() == set()

    def test_merging_into_itself_is_refused(self, sql_store, tmp_path):
        sql_store.create(_doc(LOCAL_L, "Lee Local"), slug="lee")
        with pytest.raises(ProfileWriteError):
            sql_store.merge_into(
                "lee",
                _stage(tmp_path, LOCAL_L, "Lee Local"),
                survivor_rid=LOCAL_L,
                survivor_slug="lee",
                build_missing_index=False,
            )

    def test_a_missing_retired_profile_is_a_miss(self, sql_store, tmp_path):
        with pytest.raises(ProfileNotFoundError):
            sql_store.merge_into(
                "nobody",
                _stage(tmp_path, ORCID_A, "Lee Orcid"),
                survivor_rid=ORCID_A,
                survivor_slug="lee-orcid",
                build_missing_index=False,
            )

    def test_resolve_person_answers_the_successor_and_mints_nothing(self, sql_store, tmp_path):
        sql_store.create(_doc(ORCID_B, "Lee Before"), slug="lee-before")
        sql_store.create(_doc(ORCID_A, "Lee Orcid"), slug="lee-orcid")
        sql_store.merge_into(
            "lee-before",
            _stage(tmp_path, ORCID_A, "Lee Orcid"),
            survivor_rid=ORCID_A,
            survivor_slug="lee-orcid",
            build_missing_index=False,
        )
        result = resolve_person(sql_store, rid=ORCID_B, name="Lee Before")
        assert result.rid == ORCID_A
        assert result.created is False
        assert sql_store.list_slugs() == ["lee-orcid"]

    def test_non_sql_stores_cannot_merge(self, tmp_path):
        store = FilesystemProfileStore(tmp_path / "root")
        with pytest.raises(NotImplementedError):
            store.merge_into("x", tmp_path, survivor_rid=ORCID_A, survivor_slug="x")
        assert store.successor_of("x") is None
        assert store.alias_slugs() == set()


def _tar_of_dir(src: Path) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for child in sorted(src.iterdir()):
            tf.add(child, arcname=child.name)
    return buf.getvalue()


class TestRetiredRidsStayRetired:
    """After a merge, neither the retired rid nor the retired slug can be reused.

    Before this guard, a write at the retired rid silently made it live again,
    taking it back from the survivor it aliased to.
    """

    @pytest.fixture
    def merged(self, sql_store, tmp_path):
        sql_store.create(_doc(LOCAL_L, "Lee Local"), slug="lee")
        sql_store.create(_doc(ORCID_A, "Lee Orcid"), slug="lee-orcid")
        sql_store.merge_into(
            "lee",
            _stage(tmp_path, ORCID_A, "Lee Orcid"),
            survivor_rid=ORCID_A,
            survivor_slug="lee-orcid",
            build_missing_index=False,
        )
        return sql_store

    def test_put_refuses_the_retired_rid(self, merged, tmp_path):
        staged = _stage(tmp_path / "m", LOCAL_L, "Mallory")
        with pytest.raises(RetiredRidError) as e:
            merged.put(ResearcherProfile.from_files(staged), slug="mallory")
        assert e.value.successor_rid == ORCID_A
        assert isinstance(e.value, ProfileWriteError)
        assert merged.rid_for(LOCAL_L) == ORCID_A

    def test_commit_directory_refuses_the_retired_rid(self, merged, tmp_path):
        with pytest.raises(RetiredRidError):
            merged.commit_directory("mallory", _stage(tmp_path / "m", LOCAL_L, "Mallory"))
        assert merged.rid_for(LOCAL_L) == ORCID_A
        assert not merged.exists("mallory")

    def test_commit_directory_refuses_the_retired_slug(self, merged, tmp_path):
        with pytest.raises(RetiredRidError):
            merged.commit_directory("lee", _stage(tmp_path / "m", LOCAL_M, "Mallory"))
        assert merged.rid_for("lee") == ORCID_A

    def test_create_refuses_the_retired_rid(self, merged):
        with pytest.raises(RetiredRidError):
            merged.create(_doc(LOCAL_L, "Mallory"), slug="fresh")
        assert not merged.exists("fresh")

    def test_create_bundle_refuses_the_retired_rid(self, merged):
        with pytest.raises(RetiredRidError):
            merged.create_bundle(_doc(LOCAL_L, "Mallory"), slug="fresh")

    def test_put_document_refuses_the_retired_rid(self, merged):
        with pytest.raises(RetiredRidError):
            merged.put_document("fresh", _doc(LOCAL_L, "Mallory"))
        assert not merged.exists("fresh")

    def test_put_document_refuses_the_retired_slug(self, merged):
        with pytest.raises(RetiredRidError):
            merged.put_document("lee", _doc(ORCID_A, "Lee Orcid"))

    def test_a_merge_cannot_revive_the_retired_rid(self, merged, tmp_path):
        merged.create(_doc(LOCAL_M, "Mia Local"), slug="mia")
        with pytest.raises(RetiredRidError):
            merged.merge_into(
                "mia",
                _stage(tmp_path / "m", LOCAL_L, "Mallory"),
                survivor_rid=LOCAL_L,
                survivor_slug="mallory",
                build_missing_index=False,
            )
        assert merged.rid_for("mia") == LOCAL_M
        assert merged.rid_for(LOCAL_L) == ORCID_A

    def test_http_push_is_a_409(self, merged, tmp_path, make_api_client):
        c = make_api_client(merged, token="op")
        auth = {"Authorization": "Bearer op"}
        staged = _stage(tmp_path / "m", LOCAL_L, "Mallory")
        r = c.put("/api/v1/profiles/mallory", content=_tar_of_dir(staged), headers=auth)
        assert r.status_code == 409, r.text
        assert "retired" in r.json()["detail"]
        body = {"name": "Mallory", "rid": LOCAL_L, "provenance": "synthetic"}
        r = c.put("/api/v1/profiles/mallory", json=body, headers=auth)
        assert r.status_code == 409, r.text
        assert merged.rid_for(LOCAL_L) == ORCID_A
        assert not merged.exists("mallory")


def _all_aliases():
    from sqlmodel import select

    return select(RidAliasRow)
