"""The SQL profile store: ``researcher_profiles.db`` + ``store.sql``.

One domain file for both modules, because they are one domain: ``db.py`` is the
schema and ``store/sql/`` is the backend written against it, and a test that
pins one without the other pins nothing useful.

Everything here runs on SQLite (the ``dev`` extra installs ``sql``). The two
things SQLite cannot answer, type portability and UNIQUE-with-NULL semantics,
are covered by ``tests/integration/test_db_postgres.py``, which needs
``RESEARCHER_PROFILES_TEST_DATABASE_URL``.
"""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlmodel import select

from researcher_profiles import ResearcherProfile, build_export_bundle
from researcher_profiles.build_state import BUILD_STATE_PAPER_FIELDS
from researcher_profiles.errors import WriteHookError
from researcher_profiles.privacy import effective_tiers
from researcher_profiles.profile import edit
from researcher_profiles.schema import PaperRecord
from researcher_profiles.store import ProfileNotFoundError
from researcher_profiles.store.db import (
    ArtifactRow,
    BuildStateRow,
    ExpertiseTopicRow,
    GrantRow,
    PaperRow,
    ProfileRow,
    content_hash_for,
)
from researcher_profiles.store.sql import SqlProfileStore

from .factories import build_profile_dir

RP_TABLES = {
    "rp_profiles",
    "rp_papers",
    "rp_grants",
    "rp_expertise_topics",
    "rp_artifacts",
    "rp_build_state",
}


# ---------------------------------------------------------------------------
# Local helpers (one consumer each; see AGENTS.md's one-consumer rule)
# ---------------------------------------------------------------------------


@pytest.fixture
def store() -> SqlProfileStore:
    """An empty in-memory store with the tables created."""
    s = SqlProfileStore("sqlite://")
    s.create_all()
    return s


@pytest.fixture
def jane_store(store, jane_doe) -> tuple[SqlProfileStore, str]:
    """``jane-doe`` pushed into a store; returns ``(store, rid)``."""
    return store, store.put(jane_doe)


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

    def test_create_all_creates_exactly_the_rp_tables(self, store):
        assert RP_TABLES <= set(inspect(store.engine).get_table_names())

    def test_no_table_has_an_unprefixed_name(self, store):
        # Every table carries an `rp_` prefix so it cannot collide in a shared
        # Postgres. No view or alias exists under an unprefixed name.
        names = set(inspect(store.engine).get_table_names())
        assert not ({"profiles", "papers", "expertise_topics"} & names)

    def test_papers_carry_no_build_field(self):
        # The published record is the bibliographic record, not the build's
        # dirty laundry. Build state lives in rp_build_state.
        for field in BUILD_STATE_PAPER_FIELDS:
            assert field not in PaperRow.model_fields

    def test_no_column_uses_a_postgres_only_type(self, store):
        """Portable ``sqlmodel.JSON`` only: the same tables must load on SQLite."""
        for table in ProfileRow.metadata.tables.values():
            if not table.name.startswith("rp_"):
                continue
            for column in table.columns:
                assert "JSONB" not in type(column.type).__name__.upper(), (
                    f"{table.name}.{column.name} uses {column.type!r}"
                )

    def test_papers_are_keyed_by_position_not_paper_id(self, store):
        """``paper_id`` is not unique within a profile in the real corpus.

        13 of 47 reference profiles carry duplicate generated citekeys (one has
        17 distinct works that collided), so a UNIQUE(profile_rid, paper_id)
        would refuse to ingest a quarter of the corpus.
        """
        constrained = {
            frozenset(c.columns.keys())
            for c in ProfileRow.metadata.tables["rp_papers"].constraints
            if c.__class__.__name__ == "UniqueConstraint"
        }
        assert frozenset({"profile_rid", "paper_id"}) not in constrained
        assert frozenset({"profile_rid", "ordinal"}) in constrained

    def test_date_modified_is_nullable_and_never_defaulted(self, store):
        """45 of 47 reference profiles have no ``dateModified``. Inventing one lies."""
        column = ProfileRow.metadata.tables["rp_profiles"].columns["date_modified"]
        assert column.nullable
        assert column.default is None and column.server_default is None

    def test_a_profile_with_no_date_modified_stores_null(self, jane_store):
        store, rid = jane_store
        with store.session() as s:
            assert s.get(ProfileRow, rid).date_modified is None


class TestRoundTrip:
    """put() -> export_directory() must lose nothing. The primary correctness test."""

    def test_jane_doe_round_trips_byte_for_byte(self, store, jane_doe, jane_doe_dir, tmp_path):
        rid = store.put(jane_doe)
        out = store.export_directory(rid, tmp_path / "out")
        _assert_directory_round_trip(jane_doe_dir, out)

    def test_the_profile_document_is_byte_identical(self, store, jane_doe, jane_doe_dir, tmp_path):
        rid = store.put(jane_doe)
        out = store.export_directory(rid, tmp_path / "out")
        assert (out / "profile.jsonld").read_text() == (jane_doe_dir / "profile.jsonld").read_text()

    def test_a_deep_profile_round_trips(self, store, deep_dir, tmp_path):
        """Grants, a CV and web sources are artifacts like any other."""
        prof = ResearcherProfile.from_files(deep_dir)
        rid = store.put(prof)
        out = store.export_directory(rid, tmp_path / "out")
        _assert_directory_round_trip(deep_dir, out)

    def test_a_second_put_replaces_rather_than_duplicates(self, store, jane_doe):
        rid = store.put(jane_doe)
        store.put(jane_doe)
        with store.session() as s:
            papers = s.exec(select(PaperRow).where(PaperRow.profile_rid == rid)).all()
        assert len(papers) == len(jane_doe.papers)

    def test_import_directory_is_sugar_for_put(self, store, jane_doe_dir, jane_doe):
        assert store.import_directory(jane_doe_dir) == jane_doe.rid

    def test_the_recorded_manifest_survives_ingest(self, store, jane_doe):
        """The store must not silently rewrite a published document's manifest."""
        rid = store.put(jane_doe)
        parts, subjects = store.manifest_from_rows(rid)
        assert [p.content_url for p in parts] == [p.content_url for p in jane_doe.metadata.has_part]
        assert [p.content_url for p in subjects] == [
            p.content_url for p in jane_doe.metadata.subject_of
        ]

    def test_a_lone_carriage_return_survives_verbatim(self, store, tmp_path):
        """Text mode is not verbatim, and extracted PDF text is where it shows.

        ``Path.read_text`` opens with universal newlines, which silently
        rewrites every lone ``\r`` and every ``\r\n`` to ``\n``. Three papers in
        the 47-profile reference corpus carry lone ``\r`` inside extracted PDF
        text; before this was fixed they came back the same LENGTH and a
        different sha256, which is exactly the kind of edit nobody notices.
        """
        src = build_profile_dir(tmp_path / "crlf")
        body = b"line one\r\rline two\r\nline three\n"
        paper = src / "sources" / "papers" / "crlf2020paper.md"
        paper.parent.mkdir(parents=True, exist_ok=True)
        paper.write_bytes(body)
        ResearcherProfile.from_files(src).build_manifest(write=True)

        rid = store.put(ResearcherProfile.from_files(src))
        assert store.artifact_bytes(rid, "sources/papers/crlf2020paper.md") == body
        out = store.export_directory(rid, tmp_path / "out")
        assert (out / "sources" / "papers" / "crlf2020paper.md").read_bytes() == body

    def test_binary_bodies_are_opt_in(self, store, tmp_path):
        src = build_profile_dir(tmp_path / "indexed", index="sqlite")
        prof = ResearcherProfile.from_files(src)
        rid = store.put(prof)
        with store.session() as s:
            rows = {r.content_url: r for r in s.exec(select(ArtifactRow)).all()}
        sqlite_row = rows.get(".cache/embeddings.sqlite")
        assert sqlite_row is not None, "the manifest entry must survive regardless"
        assert sqlite_row.data is None

        store.put(prof, include_binary=True)
        with store.session() as s:
            row = s.exec(
                select(ArtifactRow)
                .where(ArtifactRow.profile_rid == rid)
                .where(ArtifactRow.content_url == ".cache/embeddings.sqlite")
            ).first()
        assert row.data == (src / ".cache" / "embeddings.sqlite").read_bytes()


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
            "summary_ids": sorted(prof.summaries),
            "has_persona": prof.has_persona,
            "agent_seed": prof.to_agent_seed(),
            "metadata": prof.metadata.model_dump(mode="json"),
        }

    def test_the_two_backends_agree(self, store, jane_doe):
        rid = store.put(jane_doe)
        db = store.get(rid)
        assert self._snapshot(db) == self._snapshot(jane_doe)

    def test_a_summary_body_matches(self, store, jane_doe):
        db = store.get(store.put(jane_doe))
        key = sorted(jane_doe.summaries)[0]
        assert db.summaries[key] == jane_doe.summaries[key]

    def test_content_hash_matches_the_filesystem_digest(self, store, jane_doe):
        db = store.get(store.put(jane_doe))
        assert db.content_hash() == jane_doe.content_hash()

    def test_to_dict_differs_only_in_path(self, store, jane_doe):
        db = store.get(store.put(jane_doe))
        a, b = db.to_dict(), jane_doe.to_dict()
        assert a.pop("path").startswith("db://")
        b.pop("path")
        assert a == b

    def test_capabilities_needing_a_directory_are_refused(self, store, jane_doe):
        db = store.get(store.put(jane_doe))
        for call in (
            lambda: db.validate(),
            lambda: db.index.search("x"),
            lambda: db.index.search_similar("paper_summary", "x"),
            lambda: db.index.build(),
        ):
            with pytest.raises(NotImplementedError, match="export_directory"):
                call()

    def test_build_manifest_reads_the_rows_not_a_path(self, store, jane_doe):
        db = store.get(store.put(jane_doe))
        assert [p.content_url for p in db.build_manifest()] == [
            p.content_url for p in jane_doe.manifest()
        ]


class TestExportParity:
    """The strongest guard that the backends are interchangeable downstream."""

    def test_the_export_bundle_is_identical(self, store, jane_doe):
        db = store.get(store.put(jane_doe))
        a = build_export_bundle(jane_doe, now="2026-01-01T00:00:00+00:00")
        b = build_export_bundle(db, now="2026-01-01T00:00:00+00:00")
        assert a.model_dump() == b.model_dump()
        assert a.content_hash == b.content_hash

    def test_the_bundle_carries_no_build_state(self, store, jane_doe):
        db = store.get(store.put(jane_doe))
        assert "build_state" not in build_export_bundle(db).model_dump()


class TestWrites:
    """The public write surface, through the storage layer, against SQL."""

    def test_a_metadata_patch_updates_rows_and_stamps(self, jane_store):
        store, rid = jane_store
        db = store.get(rid)
        before = db.content_hash()

        db.edit.patch_metadata({"field": "Systems Biology"})

        with store.session() as s:
            row = s.get(ProfileRow, rid)
        assert row.field == "Systems Biology"
        assert row.document["field"] == "Systems Biology"
        assert row.date_modified, "a content change must stamp dateModified"
        assert row.date_modified == row.document["dateModified"]
        assert row.content_hash != before

    def test_set_soul_refreshes_the_content_hash(self, jane_store):
        """The digest spans document AND soul, so a soul-only write moves it."""
        store, rid = jane_store
        before = store.get(rid).content_hash()
        store.get(rid).edit.set_soul("# A different soul\n")
        after = store.get(rid)
        assert after.soul == "# A different soul\n"
        assert after.content_hash() != before
        with store.session() as s:
            assert s.get(ProfileRow, rid).content_hash == after.content_hash()

    def test_set_visibility_reaches_the_artifact_rows(self, jane_store):
        store, rid = jane_store
        db = store.get(rid)
        db.edit.set_visibility(
            profile_visibility="internal",
            artifacts=[{"role": "soul", "visibility": "restricted"}],
        )
        with store.session() as s:
            row = s.get(ProfileRow, rid)
            soul = s.exec(
                select(ArtifactRow)
                .where(ArtifactRow.profile_rid == rid)
                .where(ArtifactRow.content_url == "personality/SOUL.md")
            ).first()
        assert row.visibility == "internal"
        assert soul.visibility == "restricted"
        # And the document's manifest, regenerated from the rows, agrees.
        assert store.get(rid).metadata.subject_of[0].visibility == "restricted"

    @pytest.mark.parametrize(
        "patch",
        [{"rid": "0000-0002-1825-0097"}, {"name": ""}],
        ids=["out-of-scope-field", "invalid-document"],
    )
    def test_a_rejected_patch_leaves_every_row_untouched(self, jane_store, patch):
        store, rid = jane_store
        before = store.get(rid).content_hash()
        with pytest.raises(edit.EditError):
            store.get(rid).edit.patch_metadata(patch)
        assert store.get(rid).content_hash() == before

    def test_save_papers_replaces_the_collection(self, jane_store):
        store, rid = jane_store
        db = store.get(rid)
        db.save_papers([PaperRecord(title="Only One", year=2030, paper_id="only2030")])
        reloaded = store.get(rid)
        assert [p.title for p in reloaded.papers] == ["Only One"]

    def test_duplicate_paper_ids_survive_ingest(self, store, tmp_path):
        """The real corpus has them; a store that refuses them is broken."""
        rows = [
            {"name": "First", "datePublished": "2020", "paper_id": "dup2020"},
            {"name": "Second", "datePublished": "2020", "paper_id": "dup2020"},
        ]
        src = build_profile_dir(tmp_path / "dupes", papers=rows, summaries=False)
        rid = store.put(ResearcherProfile.from_files(src))
        assert [p.title for p in store.get(rid).papers] == ["First", "Second"]

    def test_a_write_runs_in_a_real_transaction(self, jane_store):
        seen = []
        store, rid = jane_store
        db = store.get(rid)
        db.add_pre_commit_hook(lambda ctx: seen.append((ctx.kind, ctx.atomic, ctx.session)))
        db.edit.set_soul("hooked")
        kind, atomic, session = seen[0]
        assert (kind, atomic) == ("soul", True)
        assert session is not None

    def test_a_raising_hook_rolls_the_whole_unit_back(self, jane_store):
        store, rid = jane_store
        original = store.get(rid).soul
        before = store.get(rid).content_hash()

        db = store.get(rid)

        def boom(ctx):
            raise RuntimeError("dependent state is unreachable")

        db.add_pre_commit_hook(boom)
        with pytest.raises(WriteHookError):
            db.edit.set_soul("must not land")

        assert store.get(rid).soul == original
        assert store.get(rid).content_hash() == before

    def test_saving_citations_as_none_removes_the_artifact(self, jane_store):
        store, rid = jane_store
        db = store.get(rid)
        db.save_citations(None)
        assert store.get(rid).citations is None


class TestIdentity:
    """``rid`` is identity; ``slug`` is a unique, renameable handle."""

    def test_rename_touches_only_the_profile_row(self, jane_store):
        store, rid = jane_store
        store.rename(rid, "doe-jane")
        with store.session() as s:
            assert s.get(ProfileRow, rid).slug == "doe-jane"
            for model in (PaperRow, ExpertiseTopicRow, ArtifactRow):
                rows = s.exec(select(model).where(model.profile_rid == rid)).all()
                assert rows and all(r.profile_rid == rid for r in rows)

    def test_rid_for_accepts_a_rid_or_a_slug(self, jane_store):
        store, rid = jane_store
        assert store.rid_for(rid) == rid
        assert store.rid_for("jane-doe") == rid
        store.rename(rid, "doe-jane")
        assert store.rid_for("doe-jane") == rid
        assert store.rid_for(rid) == rid
        with pytest.raises(ProfileNotFoundError, match="jane-doe"):
            store.rid_for("jane-doe")

    def test_two_profiles_cannot_share_a_slug(self, jane_store, fixture_profile):
        from sqlalchemy.exc import IntegrityError

        store, _ = jane_store
        other = ResearcherProfile.from_files(fixture_profile("john-smith"))
        with pytest.raises(IntegrityError):
            store.put(other, slug="jane-doe")

    def test_delete_removes_every_child_row(self, jane_store):
        store, rid = jane_store
        store.delete(rid)
        assert not store.exists(rid)
        with store.session() as s:
            for model in (PaperRow, GrantRow, ExpertiseTopicRow, ArtifactRow, BuildStateRow):
                assert not s.exec(select(model).where(model.profile_rid == rid)).all()

    def test_from_db_is_bound_onto_the_base_class(self, store, jane_doe):
        store.put(jane_doe)
        prof = ResearcherProfile.from_db(store.engine, "jane-doe")
        assert prof.rid == jane_doe.rid


class TestBuildState:
    """Build state lives outside the published set, and stays droppable."""

    def test_it_lands_in_its_own_table(self, jane_store):
        store, rid = jane_store
        db = store.get(rid)
        paper_id = db.papers[0].paper_id
        assert db.set_paper_contaminated(paper_id, True)

        assert store.get(rid).build_state.is_contaminated(paper_id)
        with store.session() as s:
            assert s.get(BuildStateRow, rid) is not None
            assert "contaminated" not in s.get(ProfileRow, rid).document

    def test_export_omits_it_unless_asked(self, jane_store, tmp_path):
        store, rid = jane_store
        store.get(rid).set_paper_contaminated(store.get(rid).papers[0].paper_id, True)

        plain = store.export_directory(rid, tmp_path / "plain" / "jane-doe")
        assert not (plain.parent / ".build").exists()

        with_build = store.export_directory(
            rid, tmp_path / "withbuild" / "jane-doe", with_build=True
        )
        assert (with_build.parent / ".build" / "jane-doe" / "meta").is_dir()

    def test_it_is_never_in_the_manifest(self, jane_store):
        store, rid = jane_store
        store.get(rid).set_paper_contaminated(store.get(rid).papers[0].paper_id, True)
        urls = [p.content_url for p in store.get(rid).build_manifest()]
        assert not any("build_state" in u for u in urls)

    def test_dropping_the_table_leaves_profiles_loadable(self, jane_store):
        store, rid = jane_store
        store.get(rid).set_paper_contaminated(store.get(rid).papers[0].paper_id, True)
        BuildStateRow.__table__.drop(store.engine)
        prof = store.get(rid)
        assert prof.name and prof.papers
        assert prof.build_state.papers == {}


class TestPrivacy:
    """Tiers on the rows must match what ``privacy.effective_tiers`` computes."""

    def test_restricted_artifacts_are_filterable_in_sql(self, store, deep_dir):
        prof = ResearcherProfile.from_files(deep_dir)
        rid = store.put(prof)
        with store.session() as s:
            public = {
                r.content_url
                for r in s.exec(
                    select(ArtifactRow)
                    .where(ArtifactRow.profile_rid == rid)
                    .where(ArtifactRow.visibility == "public")
                ).all()
            }
        roles = {p.content_url: p.role for p in prof.manifest()}
        assert not [u for u in public if roles.get(u) in ("cv", "web", "paper_fulltext")]

    def test_the_row_tiers_match_the_directory(self, store, deep_dir):
        prof = ResearcherProfile.from_files(deep_dir)
        rid = store.put(prof)
        expected = effective_tiers(prof.metadata)
        with store.session() as s:
            rows = s.exec(select(ArtifactRow).where(ArtifactRow.profile_rid == rid)).all()
        for row in rows:
            assert row.visibility == expected[row.content_url], row.content_url


class TestStoreHelpers:
    """The store-level surface that is not about one profile."""

    def test_list_profiles_is_ordered_by_slug(self, store, fixture_profiles_root):
        root = fixture_profiles_root("jane-doe", "john-smith")
        for slug in ("john-smith", "jane-doe"):
            store.import_directory(root / slug)
        assert [r.slug for r in store.list_profiles()] == ["jane-doe", "john-smith"]

    def test_content_hash_for_matches_the_filesystem_definition(self, jane_doe):
        document = json.loads((jane_doe.directory / "profile.jsonld").read_text())
        assert content_hash_for(document, jane_doe.soul) == jane_doe.content_hash()
