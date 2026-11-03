"""The SQL profile store: ``researcher_profiles.db`` (schema) + ``store.sql`` (backend), on SQLite.
Contracts shared with the filesystem store live in test_store.py. Postgres-only checks (type
portability, UNIQUE-with-NULL) are in ``tests/integration/test_db_postgres.py``.
"""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlmodel import select

from researcher_profiles import ResearcherProfile, build_export_bundle
from researcher_profiles.build_state import BUILD_STATE_PAPER_FIELDS
from researcher_profiles.errors import ProfileWriteError
from researcher_profiles.privacy import effective_tiers
from researcher_profiles.profile import edit
from researcher_profiles.schema import PaperRecord, ProfileDocument
from researcher_profiles.store.db import (
    ArtifactRow,
    BuildStateRow,
    ChunkVectorRow,
    ExpertiseTopicRow,
    GrantRow,
    PaperRow,
    ProfileRow,
    ProfileVectorRow,
    RidAliasRow,
    SlugAliasRow,
    content_hash_for,
)
from researcher_profiles.store.sql import SqlProfileStore

from .factories import build_profile_dir

ADA_ORCID = "0000-0002-1825-0097"
LEE_LOCAL = "local:lee-local-abc123"


def _doc(rid: str, name: str) -> ProfileDocument:
    provenance = "synthetic" if rid.startswith("local:") else "third_party"
    return ProfileDocument(name=name, rid=rid, provenance=provenance)


def _staged(tmp_path: Path, rid: str, name: str) -> Path:
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
    )


RP_TABLES = {
    "rp_profiles",
    "rp_papers",
    "rp_grants",
    "rp_expertise_topics",
    "rp_artifacts",
    "rp_build_state",
}


@pytest.fixture
def jane_store(empty_sql_store, jane_doe) -> tuple[SqlProfileStore, str]:
    """``jane-doe`` pushed into a store; returns ``(store, profile_id)``."""
    return empty_sql_store, empty_sql_store.put(jane_doe)


@pytest.fixture
def deep_dir(tmp_path) -> Path:
    """A synthetic profile carrying grants, a CV and web sources.

    ``tests/fixtures/published/deep`` is not usable here: it belongs to the
    lying consumer corpus and its rid fails the ORCID checksum, so
    it never loads as a ``ResearcherProfile``. This builds the same *shape*.
    """
    return build_profile_dir(
        tmp_path / "deep-profile", level="deep", grants=True, cv=True, web=True
    )


def _relative_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_directory_round_trip(src: Path, out: Path) -> None:
    assert _relative_files(src) == _relative_files(out)
    for rel in _relative_files(src):
        assert _sha256(src / rel) == _sha256(out / rel), rel


class TestSchema:
    """The tables themselves: names, columns, and what must not be there."""

    def test_tables_are_the_rp_prefixed_set(self, empty_sql_store):
        """Every table carries an ``rp_`` prefix so it cannot collide in a shared Postgres."""
        names = set(inspect(empty_sql_store.engine).get_table_names())
        assert RP_TABLES <= names
        assert not ({"profiles", "papers", "expertise_topics"} & names)

    def test_papers_carry_no_build_field(self):
        """The published record is the bibliographic one; build state lives in rp_build_state."""
        for field in BUILD_STATE_PAPER_FIELDS:
            assert field not in PaperRow.model_fields

    def test_no_column_uses_a_postgres_only_type(self):
        """Portable ``sqlmodel.JSON`` only: the same tables must load on SQLite."""
        for table in ProfileRow.metadata.tables.values():
            if not table.name.startswith("rp_"):
                continue
            for column in table.columns:
                assert "JSONB" not in type(column.type).__name__.upper(), (
                    f"{table.name}.{column.name} uses {column.type!r}"
                )

    def test_papers_are_keyed_by_position_not_paper_id(self):
        """``paper_id`` repeats within real profiles (duplicate citekeys), so it cannot be UNIQUE."""
        constrained = {
            frozenset(c.columns.keys())
            for c in ProfileRow.metadata.tables["rp_papers"].constraints
            if c.__class__.__name__ == "UniqueConstraint"
        }
        assert frozenset({"profile_id", "paper_id"}) not in constrained
        assert frozenset({"profile_id", "ordinal"}) in constrained

    def test_profiles_are_keyed_by_profile_id_not_rid(self, empty_sql_store):
        insp = inspect(empty_sql_store.engine)
        assert insp.get_pk_constraint("rp_profiles")["constrained_columns"] == ["profile_id"]
        assert ["rid"] not in [
            u["column_names"] for u in insp.get_unique_constraints("rp_profiles")
        ]
        columns = {c["name"] for c in insp.get_columns("rp_profiles")}
        assert not {"owner_kind", "owner_principal_id", "is_self"} & columns
        rid_index = next(
            i for i in insp.get_indexes("rp_profiles") if i["name"] == "ix_rp_profiles_rid"
        )
        assert not rid_index["unique"]

    def test_every_child_row_hangs_off_profile_id(self, empty_sql_store):
        insp = inspect(empty_sql_store.engine)
        for table in (
            "rp_papers",
            "rp_grants",
            "rp_expertise_topics",
            "rp_artifacts",
            "rp_chunk_vectors",
            "rp_profile_vectors",
            "rp_build_state",
            "rp_slug_aliases",
        ):
            fks = insp.get_foreign_keys(table)
            assert [(fk["referred_table"], fk["referred_columns"]) for fk in fks] == [
                ("rp_profiles", ["profile_id"])
            ], table

    def test_retired_slugs_and_rids_live_in_their_own_tables(self, empty_sql_store):
        insp = inspect(empty_sql_store.engine)
        assert {c["name"] for c in insp.get_columns("rp_rid_aliases")} == {
            "old_rid",
            "successor_rid",
            "created_at",
        }
        assert {c["name"] for c in insp.get_columns("rp_slug_aliases")} == {
            "old_slug",
            "profile_id",
            "created_at",
        }

    def test_date_modified_is_nullable_and_never_defaulted(self, jane_store):
        """Most reference profiles have no ``dateModified``. Inventing one lies."""
        column = ProfileRow.metadata.tables["rp_profiles"].columns["date_modified"]
        assert column.nullable
        assert column.default is None and column.server_default is None
        store, pid = jane_store
        with store.session() as s:
            assert s.get(ProfileRow, pid).date_modified is None


class TestRoundTrip:
    """put() -> export_directory() must lose nothing. The primary correctness test."""

    @pytest.mark.parametrize("which", ["jane", "deep"])
    def test_a_profile_round_trips_byte_for_byte(self, request, empty_sql_store, tmp_path, which):
        src = request.getfixturevalue("jane_doe_dir" if which == "jane" else "deep_dir")
        pid = empty_sql_store.put(ResearcherProfile.from_files(src))
        out = empty_sql_store.export_directory(pid, tmp_path / "out")
        _assert_directory_round_trip(src, out)

    def test_a_second_put_replaces_rather_than_duplicates(self, empty_sql_store, jane_doe):
        pid = empty_sql_store.put(jane_doe)
        empty_sql_store.put(jane_doe)
        with empty_sql_store.session() as s:
            papers = s.exec(select(PaperRow).where(PaperRow.profile_id == pid)).all()
        assert len(papers) == len(jane_doe.papers)

    def test_import_directory_is_sugar_for_put(self, empty_sql_store, jane_doe_dir, jane_doe):
        pid = empty_sql_store.import_directory(jane_doe_dir)
        assert empty_sql_store.get(pid).rid == jane_doe.rid

    def test_the_recorded_manifest_survives_ingest(self, empty_sql_store, jane_doe):
        """The store must not silently rewrite a published document's manifest."""
        pid = empty_sql_store.put(jane_doe)
        parts, subjects = empty_sql_store.manifest_from_rows(pid)
        assert [p.content_url for p in parts] == [p.content_url for p in jane_doe.metadata.has_part]
        assert [p.content_url for p in subjects] == [
            p.content_url for p in jane_doe.metadata.subject_of
        ]

    def test_binary_bodies_are_opt_in(self, empty_sql_store, tmp_path):
        store = empty_sql_store
        src = build_profile_dir(tmp_path / "indexed", index="sqlite")
        prof = ResearcherProfile.from_files(src)
        pid = store.put(prof)
        with store.session() as s:
            rows = {r.content_url: r for r in s.exec(select(ArtifactRow)).all()}
        sqlite_row = rows.get(".cache/embeddings.sqlite")
        assert sqlite_row is not None, "the manifest entry must survive regardless"
        assert sqlite_row.data is None

        store.put(prof, include_binary=True)
        with store.session() as s:
            row = s.exec(
                select(ArtifactRow)
                .where(ArtifactRow.profile_id == pid)
                .where(ArtifactRow.content_url == ".cache/embeddings.sqlite")
            ).first()
        assert row.data == (src / ".cache" / "embeddings.sqlite").read_bytes()


FIXED = "2026-01-01T00:00:00+00:00"


class TestBackendParity:
    """The same profile, loaded two ways, must agree field for field."""

    @staticmethod
    def _snapshot(prof) -> dict:
        return {
            "name": prof.name,
            "rid": prof.rid,
            "level": prof.level,
            "provenance": prof.provenance,
            "license": prof.license,
            "expertise": prof.expertise,
            "soul": prof.soul,
            "n_papers": len(prof.papers),
            "n_grants": len(prof.grants),
            "citations": prof.citations,
            "summaries": dict(prof.summaries),
            "content_hash": prof.content_hash(),
            "has_persona": prof.has_persona,
            "agent_seed": prof.to_agent_seed(),
            "metadata": prof.metadata.model_dump(mode="json"),
            "bundle": build_export_bundle(prof, now=FIXED).model_dump(),
        }

    def test_the_sql_profile_matches_the_filesystem_one(self, jane_store, jane_doe):
        store, pid = jane_store
        db = store.get(pid)
        assert self._snapshot(db) == self._snapshot(jane_doe)
        assert "build_state" not in build_export_bundle(db).model_dump()
        assert [p.content_url for p in db.build_manifest()] == [
            p.content_url for p in jane_doe.manifest()
        ]
        a, b = db.to_dict(), jane_doe.to_dict()
        assert a.pop("path").startswith("db://")
        b.pop("path")
        assert a == b

    def test_capabilities_needing_a_directory_are_refused(self, jane_store):
        store, pid = jane_store
        db = store.get(pid)
        for call in (
            lambda: db.validate(),
            lambda: db.index.search("x"),
            lambda: db.index.search_similar("paper_summary", "x"),
            lambda: db.index.build(),
        ):
            with pytest.raises(NotImplementedError, match="export_directory"):
                call()


class TestWrites:
    """The public write surface, through the storage layer, against SQL."""

    def test_a_metadata_patch_updates_rows_and_stamps(self, jane_store):
        store, pid = jane_store
        db = store.get(pid)
        before = db.content_hash()

        db.edit.patch_metadata({"field": "Systems Biology"})

        with store.session() as s:
            row = s.get(ProfileRow, pid)
        assert row.field == "Systems Biology"
        assert row.document["field"] == "Systems Biology"
        assert row.date_modified, "a content change must stamp dateModified"
        assert row.date_modified == row.document["dateModified"]
        assert row.content_hash != before

    def test_set_soul_refreshes_the_content_hash(self, jane_store):
        """The digest spans document AND soul, so a soul-only write moves it."""
        store, pid = jane_store
        before = store.get(pid).content_hash()
        store.get(pid).edit.set_soul("# A different soul\n")
        after = store.get(pid)
        assert after.soul == "# A different soul\n"
        assert after.content_hash() != before
        with store.session() as s:
            assert s.get(ProfileRow, pid).content_hash == after.content_hash()

    def test_set_visibility_reaches_the_artifact_rows(self, jane_store):
        store, pid = jane_store
        db = store.get(pid)
        db.edit.set_visibility(
            profile_visibility="limited",
            artifacts=[{"role": "soul", "visibility": "private"}],
        )
        with store.session() as s:
            row = s.get(ProfileRow, pid)
            soul = s.exec(
                select(ArtifactRow)
                .where(ArtifactRow.profile_id == pid)
                .where(ArtifactRow.content_url == "personality/SOUL.md")
            ).first()
        assert row.visibility == "limited"
        assert soul.visibility == "private"
        # And the document's manifest, regenerated from the rows, agrees.
        assert store.get(pid).metadata.subject_of[0].visibility == "private"

    @pytest.mark.parametrize(
        "patch",
        [{"rid": "0000-0002-1825-0097"}, {"name": ""}],
        ids=["out-of-scope-field", "invalid-document"],
    )
    def test_a_rejected_patch_leaves_every_row_untouched(self, jane_store, patch):
        store, pid = jane_store
        before = store.get(pid).content_hash()
        with pytest.raises(edit.EditError):
            store.get(pid).edit.patch_metadata(patch)
        assert store.get(pid).content_hash() == before

    def test_save_papers_replaces_the_collection(self, jane_store):
        store, pid = jane_store
        db = store.get(pid)
        db.save_papers([PaperRecord(title="Only One", year=2030, paper_id="only2030")])
        reloaded = store.get(pid)
        assert [p.title for p in reloaded.papers] == ["Only One"]

    def test_duplicate_paper_ids_survive_ingest(self, empty_sql_store, tmp_path):
        """The real corpus has them; a store that refuses them is broken."""
        rows = [
            {"name": "First", "datePublished": "2020", "paper_id": "dup2020"},
            {"name": "Second", "datePublished": "2020", "paper_id": "dup2020"},
        ]
        src = build_profile_dir(tmp_path / "dupes", papers=rows, summaries=False)
        pid = empty_sql_store.put(ResearcherProfile.from_files(src))
        assert [p.title for p in empty_sql_store.get(pid).papers] == ["First", "Second"]


class TestIdentity:
    """``profile_id`` is the record; ``rid`` the subject; ``slug`` a unique, renameable handle."""

    def test_a_rid_is_a_ref(self, jane_store):
        store, pid = jane_store
        rid = store.get(pid).rid
        assert store.get(rid).profile_id == pid
        assert store.resolve_slug(rid) == "jane-doe"
        assert store.exists(rid)

    def test_a_second_record_about_one_person_is_refused_by_default(self, empty_sql_store):
        store = empty_sql_store
        store.create(_doc(ADA_ORCID, "Ada"), slug="ada")
        with pytest.raises(ProfileWriteError):
            store.create(_doc(ADA_ORCID, "Ada again"), slug="ada-2")
        assert store.list_slugs() == ["ada"]

    def test_a_put_that_would_be_a_second_record_is_a_write_error(self, empty_sql_store, jane_doe):
        store = empty_sql_store
        store.put(jane_doe)
        with pytest.raises(ProfileWriteError):
            store.put(jane_doe, slug="jane-again")
        assert store.list_slugs() == ["jane-doe"]
        assert len(store.profiles_about(jane_doe.rid)) == 1

    def test_a_host_may_keep_several_records_about_one_person(self, empty_sql_store):
        """``one_profile_per_rid=False``: both load; a bare rid names neither."""
        store = SqlProfileStore(empty_sql_store.engine, one_profile_per_rid=False)
        store.create(_doc(ADA_ORCID, "Ada"), slug="ada")
        store.create(_doc(ADA_ORCID, "Ada (curated)"), slug="ada-curated")
        about = store.profiles_about(ADA_ORCID)
        assert [r.slug for r in about] == ["ada", "ada-curated"]
        assert store.get("ada-curated").name == "Ada (curated)"
        assert not store.exists(ADA_ORCID)

    def test_a_put_on_a_live_slug_keeps_the_record(self, empty_sql_store, jane_doe):
        store = empty_sql_store
        first = store.put(jane_doe)
        second = store.put(jane_doe)
        assert first == second

    def test_a_put_on_a_live_slug_with_another_rid_is_refused(self, jane_store, fixture_profile):
        store, pid = jane_store
        other = ResearcherProfile.from_files(fixture_profile("john-smith"))
        with pytest.raises(ProfileWriteError):
            store.put(other, slug="jane-doe")
        assert store.get(pid).rid != other.rid

    def test_a_rename_keeps_the_old_slug_resolving(self, jane_store):
        store, pid = jane_store
        store.rename(pid, "doe-jane")
        with store.session() as s:
            assert s.get(ProfileRow, pid).slug == "doe-jane"
            for model in (PaperRow, ExpertiseTopicRow, ArtifactRow):
                rows = s.exec(select(model).where(model.profile_id == pid)).all()
                assert rows and all(r.profile_id == pid for r in rows)
        assert store.get("jane-doe").profile_id == pid
        assert store.resolve_slug("jane-doe") == "doe-jane"
        assert store.alias_slugs() == {"jane-doe"}
        assert store.get(f"#{pid}").slug == "doe-jane"

    @pytest.mark.parametrize(
        "write",
        [
            lambda s, d: s.get(s.put(ResearcherProfile.from_files(d), slug="jane-doe")),
            lambda s, d: s.commit_directory("jane-doe", d),
            lambda s, d: s.put_document("jane-doe", ResearcherProfile.from_files(d).metadata),
        ],
        ids=["put", "commit_directory", "put_document"],
    )
    def test_a_write_at_a_renamed_slug_replaces_the_record(self, jane_store, jane_doe_dir, write):
        """A former slug about the same rid follows its alias; the record keeps its live slug."""
        store, pid = jane_store
        store.rename(pid, "doe-jane")
        written = write(store, jane_doe_dir)
        assert (written.profile_id, written.slug) == (pid, "doe-jane")
        assert store.list_slugs() == ["doe-jane"]
        assert store.alias_slugs() == {"jane-doe"}

    @pytest.mark.parametrize(
        "write",
        [
            lambda s, d: s.commit_directory("jane-doe", d),
            lambda s, d: s.put_document("jane-doe", ResearcherProfile.from_files(d).metadata),
        ],
        ids=["commit_directory", "put_document"],
    )
    def test_a_write_at_a_renamed_slug_about_another_rid_is_refused(
        self, jane_store, fixture_profile, write
    ):
        from researcher_profiles.store import RetiredRidError

        store, pid = jane_store
        store.rename(pid, "doe-jane")
        with pytest.raises(RetiredRidError, match="former slug") as e:
            write(store, fixture_profile("john-smith"))
        assert e.value.successor_rid == store.get(pid).rid
        assert store.list_slugs() == ["doe-jane"]

    def test_delete_removes_every_child_row_and_slug_alias(self, jane_store):
        store, pid = jane_store
        store.rename(pid, "doe-jane")
        store.delete(pid)
        assert not store.exists(pid)
        assert not store.exists("jane-doe")
        with store.session() as s:
            for model in (PaperRow, GrantRow, ExpertiseTopicRow, ArtifactRow, BuildStateRow):
                assert not s.exec(select(model).where(model.profile_id == pid)).all()
            assert not s.exec(select(SlugAliasRow)).all()

    def test_from_db_is_bound_onto_the_base_class(self, empty_sql_store, jane_doe):
        empty_sql_store.put(jane_doe)
        prof = ResearcherProfile.from_db(empty_sql_store.engine, "jane-doe")
        assert prof.rid == jane_doe.rid


class TestMerge:
    """``merge_into`` by record: the survivor keeps its ``profile_id``; aliases cover the rest."""

    def _merge(self, store, tmp_path, retired, *, into, rid, slug, name="Lee"):
        return store.merge_into(
            retired,
            _staged(tmp_path, rid, name),
            survivor_ref=into,
            survivor_rid=rid,
            survivor_slug=slug,
            build_missing_index=False,
        )

    def test_a_regular_merge_keeps_the_survivor_record(self, empty_sql_store, tmp_path):
        store = empty_sql_store
        store.create(_doc(LEE_LOCAL, "Lee Local"), slug="lee")
        store.create(_doc(ADA_ORCID, "Lee Orcid"), slug="lee-orcid")
        retired, survivor = store.profile_id_for("lee"), store.profile_id_for("lee-orcid")
        result = self._merge(
            store, tmp_path, "lee", into="lee-orcid", rid=ADA_ORCID, slug="lee-orcid"
        )
        assert result.profile_id == survivor
        assert not store.exists(retired)
        assert store.get("lee").profile_id == survivor  # the retired slug redirects
        assert store.successor_rid(LEE_LOCAL) == ADA_ORCID
        with store.session() as s:
            assert {a.old_rid for a in s.exec(select(RidAliasRow)).all()} == {LEE_LOCAL}
            assert {(a.old_slug, a.profile_id) for a in s.exec(select(SlugAliasRow)).all()} == {
                ("lee", survivor)
            }

    def test_a_merge_between_records_about_one_person_writes_no_rid_alias(
        self, empty_sql_store, tmp_path
    ):
        store = SqlProfileStore(empty_sql_store.engine, one_profile_per_rid=False)
        store.create(_doc(ADA_ORCID, "Ada"), slug="ada")
        store.create(_doc(ADA_ORCID, "Ada old"), slug="ada-old")
        seen = []
        store.add_pre_commit_hook(
            lambda ctx: (
                seen.append((ctx.profile_id, ctx.retired_profile_id, ctx.retired_rid))
                if ctx.kind == "merge"
                else None
            )
        )
        survivor, retired = store.profile_id_for("ada"), store.profile_id_for("ada-old")
        self._merge(store, tmp_path, "ada-old", into="ada", rid=ADA_ORCID, slug="ada", name="Ada")
        assert seen == [(survivor, retired, None)]
        assert store.successor_rid(ADA_ORCID) is None
        with store.session() as s:
            assert not s.exec(select(RidAliasRow)).all()
        assert store.get("ada-old").profile_id == survivor

    def test_a_convert_keeps_the_record_and_changes_the_rid(self, empty_sql_store, tmp_path):
        store = empty_sql_store
        store.create(_doc(LEE_LOCAL, "Lee"), slug="lee")
        pid = store.profile_id_for("lee")
        seen = []
        store.add_pre_commit_hook(
            lambda ctx: (
                seen.append((ctx.profile_id, ctx.retired_profile_id, ctx.retired_rid))
                if ctx.kind == "merge"
                else None
            )
        )
        result = self._merge(store, tmp_path, "lee", into=None, rid=ADA_ORCID, slug="lee")
        assert seen == [(pid, None, LEE_LOCAL)]
        assert result.profile_id == pid
        assert store.get(pid).rid == ADA_ORCID
        assert store.successor_rid(LEE_LOCAL) == ADA_ORCID
        assert [r.profile_id for r in store.profiles_about(LEE_LOCAL)] == [pid]
        assert store.get(LEE_LOCAL).profile_id == pid  # a retired rid is a ref too


class TestCreateHooks:
    """A host writes its own rows about a new record in the create's transaction."""

    def test_a_failing_create_hook_leaves_no_row(self, empty_sql_store):
        """The row is inserted on the unit's own session, so it rolls back with the unit."""
        from researcher_profiles.errors import WriteHookError

        store = empty_sql_store

        def _explode(ctx):
            if ctx.kind == "create":
                assert ctx.profile_id is not None
                raise RuntimeError("no")

        store.add_pre_commit_hook(_explode)
        with pytest.raises(WriteHookError):
            store.create(_doc(ADA_ORCID, "Ada"), slug="ada")
        assert store.list_slugs() == []


class TestBuildState:
    """Build state lives outside the published set, and stays droppable."""

    @pytest.fixture
    def contaminated(self, jane_store):
        store, pid = jane_store
        paper_id = store.get(pid).papers[0].paper_id
        assert store.get(pid).set_paper_contaminated(paper_id, True)
        return store, pid, paper_id

    def test_it_lives_in_its_own_table_not_the_document(self, contaminated):
        store, pid, paper_id = contaminated
        assert store.get(pid).build_state.is_contaminated(paper_id)
        with store.session() as s:
            assert s.get(BuildStateRow, pid) is not None
            assert "contaminated" not in s.get(ProfileRow, pid).document
        urls = [p.content_url for p in store.get(pid).build_manifest()]
        assert not any("build_state" in u for u in urls)

    def test_export_omits_it_unless_asked(self, contaminated, tmp_path):
        store, pid, _ = contaminated
        plain = store.export_directory(pid, tmp_path / "plain" / "jane-doe")
        assert not (plain.parent / ".build").exists()

        with_build = store.export_directory(
            pid, tmp_path / "withbuild" / "jane-doe", with_build=True
        )
        assert (with_build.parent / ".build" / "jane-doe" / "meta").is_dir()

    def test_dropping_the_table_leaves_profiles_loadable(self, contaminated):
        store, pid, _ = contaminated
        BuildStateRow.__table__.drop(store.engine)
        prof = store.get(pid)
        assert prof.name and prof.papers
        assert prof.build_state.papers == {}


class TestPrivacy:
    """Tiers on the rows must match what ``privacy.effective_tiers`` computes."""

    def test_row_tiers_match_effective_tiers(self, empty_sql_store, deep_dir):
        prof = ResearcherProfile.from_files(deep_dir)
        pid = empty_sql_store.put(prof)
        expected = effective_tiers(prof.metadata)
        with empty_sql_store.session() as s:
            rows = s.exec(select(ArtifactRow).where(ArtifactRow.profile_id == pid)).all()
        for row in rows:
            assert row.visibility == expected[row.content_url], row.content_url
        roles = {p.content_url: p.role for p in prof.manifest()}
        public = [r.content_url for r in rows if r.visibility == "public"]
        assert not [u for u in public if roles.get(u) in ("cv", "web", "paper_fulltext")]


class TestStoreHelpers:
    """The store-level surface that is not about one profile."""

    def test_list_profiles_is_ordered_by_slug(self, empty_sql_store, fixture_profiles_root):
        store = empty_sql_store
        root = fixture_profiles_root("jane-doe", "john-smith")
        for slug in ("john-smith", "jane-doe"):
            store.import_directory(root / slug)
        assert [r.slug for r in store.list_profiles()] == ["jane-doe", "john-smith"]

    def test_content_hash_for_matches_the_filesystem_definition(self, jane_doe):
        document = json.loads((jane_doe.directory / "profile.jsonld").read_text())
        assert content_hash_for(document, jane_doe.soul) == jane_doe.content_hash()


class TestReadCost:
    """A profile handed out by ``get`` carries the document its row already held."""

    @staticmethod
    def _count(store):
        from sqlalchemy import event

        seen: list[str] = []

        def on_execute(conn, cursor, statement, *args):  # noqa: ARG001
            seen.append(statement)

        event.listen(store.engine, "before_cursor_execute", on_execute)
        return seen, lambda: event.remove(store.engine, "before_cursor_execute", on_execute)

    def test_get_then_metadata_reads_the_row_once(self, jane_store):
        store, pid = jane_store
        seen, stop = self._count(store)
        try:
            assert store.get(pid).metadata.name
        finally:
            stop()
        assert len(seen) == 1

    def test_an_edit_is_seen_by_a_reread_on_the_same_object(self, jane_store):
        store, pid = jane_store
        prof = store.get(pid)
        prof.edit.patch_metadata({"field": "Systems Biology"})
        assert prof.metadata.field == "Systems Biology"
        assert store.get(pid).metadata.field == "Systems Biology"

    def test_get_many_is_one_read_and_skips_missing_ids(
        self, empty_sql_store, fixture_profiles_root
    ):
        store = empty_sql_store
        root = fixture_profiles_root("jane-doe", "john-smith")
        pids = [store.import_directory(root / slug) for slug in ("jane-doe", "john-smith")]
        seen, stop = self._count(store)
        try:
            got = store.get_many([*pids, 999_999])
            names = {pid: prof.metadata.name for pid, prof in got.items()}
        finally:
            stop()
        assert sorted(got) == sorted(pids)
        assert names == {pid: store.get(pid).metadata.name for pid in pids}
        assert len(seen) == 1


class TestSoulOnlyVectors:
    """An interviewed trainee: a narrative, no papers. A refresh still indexes it."""

    def test_refresh_vectors_writes_chunks_and_a_centroid(
        self, tmp_path, fake_backend, empty_sql_store
    ):
        d = build_profile_dir(
            tmp_path / "student",
            rid="0000-0002-1825-0097",
            level="lite",
            papers=False,
            personality=False,
            summaries=False,
        )
        (d / "personality").mkdir()
        (d / "personality" / "SOUL.md").write_text(
            "> Self-reported. Written from an interview.\n\n"
            "## Who they are as a scientist\n\nA student who maps enhancers.\n\n"
            "## What drives the work\n\nWanting to know how chromatin folds.\n",
            encoding="utf-8",
        )
        from .factories import refresh_manifest

        refresh_manifest(d)
        pid = empty_sql_store.import_directory(d)
        assert empty_sql_store.refresh_vectors(pid) is True
        with empty_sql_store.session() as s:
            chunks = s.exec(select(ChunkVectorRow).where(ChunkVectorRow.profile_id == pid)).all()
            centroids = s.exec(
                select(ProfileVectorRow).where(ProfileVectorRow.profile_id == pid)
            ).all()
        assert chunks and {c.source_type for c in chunks} == {"soul"}
        assert [c.kind for c in centroids] == ["centroid"]
        assert empty_sql_store.centroid(pid).shape == (fake_backend.dim,)


class TestSeedOnlyVectors:
    """A student whose only chunks are private seeds still gets a centroid, and no chunk rows."""

    def _seed_only(self, tmp_path, fake_backend) -> Path:
        from researcher_profiles.embeddings import build_index
        from researcher_profiles.schema import SeedPapersDocument
        from researcher_profiles.schema.jsonld import canonical_dumps

        d = build_profile_dir(
            tmp_path / "student",
            rid="0000-0002-1825-0097",
            level="lite",
            personality=False,
            summaries=False,
            papers=[{"paper_id": "none2024", "title": "No abstract"}],
        )
        doc = SeedPapersDocument.model_validate(
            {
                "hasPart": [
                    {"title": "Seed A", "openalex_id": "W1", "abstract": "Chromatin."},
                    {"title": "Seed B", "openalex_id": "W2", "abstract": "Enhancers."},
                ]
            }
        )
        (d / "sources" / "seed_papers.jsonld").write_text(
            canonical_dumps(doc.model_dump(mode="json")), encoding="utf-8"
        )
        from .factories import refresh_manifest

        refresh_manifest(d)
        build_index(d, backend=fake_backend)
        return d

    def test_centroid_row_without_public_chunks(self, tmp_path, fake_backend, empty_sql_store):
        from researcher_profiles.store.sql._vectors import shred_profile

        d = self._seed_only(tmp_path, fake_backend)
        prof = ResearcherProfile.from_files(d)
        rows = shred_profile(1, prof)
        assert [type(r) for r in rows] == [ProfileVectorRow]

        pid = empty_sql_store.put(prof, include_binary=True)
        with empty_sql_store.session() as s:
            chunk_rows = s.exec(
                select(ChunkVectorRow).where(ChunkVectorRow.profile_id == pid)
            ).all()
        assert chunk_rows == []  # private seed vectors never leave the build
        assert empty_sql_store.centroid(pid).shape == (fake_backend.dim,)
