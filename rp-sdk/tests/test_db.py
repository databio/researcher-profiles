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
from researcher_profiles.privacy import effective_tiers
from researcher_profiles.profile import edit
from researcher_profiles.schema import PaperRecord
from researcher_profiles.store import ProfileNotFoundError
from researcher_profiles.store.db import (
    ArtifactRow,
    BuildStateRow,
    ChunkVectorRow,
    ExpertiseTopicRow,
    GrantRow,
    PaperRow,
    ProfileRow,
    ProfileVectorRow,
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


@pytest.fixture
def jane_store(empty_sql_store, jane_doe) -> tuple[SqlProfileStore, str]:
    """``jane-doe`` pushed into a store; returns ``(store, rid)``."""
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
        assert frozenset({"profile_rid", "paper_id"}) not in constrained
        assert frozenset({"profile_rid", "ordinal"}) in constrained

    def test_date_modified_is_nullable_and_never_defaulted(self, jane_store):
        """Most reference profiles have no ``dateModified``. Inventing one lies."""
        column = ProfileRow.metadata.tables["rp_profiles"].columns["date_modified"]
        assert column.nullable
        assert column.default is None and column.server_default is None
        store, rid = jane_store
        with store.session() as s:
            assert s.get(ProfileRow, rid).date_modified is None


class TestRoundTrip:
    """put() -> export_directory() must lose nothing. The primary correctness test."""

    @pytest.mark.parametrize("which", ["jane", "deep"])
    def test_a_profile_round_trips_byte_for_byte(self, request, empty_sql_store, tmp_path, which):
        src = request.getfixturevalue("jane_doe_dir" if which == "jane" else "deep_dir")
        rid = empty_sql_store.put(ResearcherProfile.from_files(src))
        out = empty_sql_store.export_directory(rid, tmp_path / "out")
        _assert_directory_round_trip(src, out)

    def test_a_second_put_replaces_rather_than_duplicates(self, empty_sql_store, jane_doe):
        rid = empty_sql_store.put(jane_doe)
        empty_sql_store.put(jane_doe)
        with empty_sql_store.session() as s:
            papers = s.exec(select(PaperRow).where(PaperRow.profile_rid == rid)).all()
        assert len(papers) == len(jane_doe.papers)

    def test_import_directory_is_sugar_for_put(self, empty_sql_store, jane_doe_dir, jane_doe):
        assert empty_sql_store.import_directory(jane_doe_dir) == jane_doe.rid

    def test_the_recorded_manifest_survives_ingest(self, empty_sql_store, jane_doe):
        """The store must not silently rewrite a published document's manifest."""
        rid = empty_sql_store.put(jane_doe)
        parts, subjects = empty_sql_store.manifest_from_rows(rid)
        assert [p.content_url for p in parts] == [p.content_url for p in jane_doe.metadata.has_part]
        assert [p.content_url for p in subjects] == [
            p.content_url for p in jane_doe.metadata.subject_of
        ]

    def test_binary_bodies_are_opt_in(self, empty_sql_store, tmp_path):
        store = empty_sql_store
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
        store, rid = jane_store
        db = store.get(rid)
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
        store, rid = jane_store
        db = store.get(rid)
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
            profile_visibility="limited",
            artifacts=[{"role": "soul", "visibility": "private"}],
        )
        with store.session() as s:
            row = s.get(ProfileRow, rid)
            soul = s.exec(
                select(ArtifactRow)
                .where(ArtifactRow.profile_rid == rid)
                .where(ArtifactRow.content_url == "personality/SOUL.md")
            ).first()
        assert row.visibility == "limited"
        assert soul.visibility == "private"
        # And the document's manifest, regenerated from the rows, agrees.
        assert store.get(rid).metadata.subject_of[0].visibility == "private"

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

    def test_duplicate_paper_ids_survive_ingest(self, empty_sql_store, tmp_path):
        """The real corpus has them; a store that refuses them is broken."""
        rows = [
            {"name": "First", "datePublished": "2020", "paper_id": "dup2020"},
            {"name": "Second", "datePublished": "2020", "paper_id": "dup2020"},
        ]
        src = build_profile_dir(tmp_path / "dupes", papers=rows, summaries=False)
        rid = empty_sql_store.put(ResearcherProfile.from_files(src))
        assert [p.title for p in empty_sql_store.get(rid).papers] == ["First", "Second"]


class TestIdentity:
    """``rid`` is identity; ``slug`` is a unique, renameable handle."""

    def test_rename_moves_only_the_slug(self, jane_store):
        """The slug/rid lookup before a rename is covered by test_store's any-handle table."""
        store, rid = jane_store
        store.rename(rid, "doe-jane")
        with store.session() as s:
            assert s.get(ProfileRow, rid).slug == "doe-jane"
            for model in (PaperRow, ExpertiseTopicRow, ArtifactRow):
                rows = s.exec(select(model).where(model.profile_rid == rid)).all()
                assert rows and all(r.profile_rid == rid for r in rows)
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

    def test_from_db_is_bound_onto_the_base_class(self, empty_sql_store, jane_doe):
        empty_sql_store.put(jane_doe)
        prof = ResearcherProfile.from_db(empty_sql_store.engine, "jane-doe")
        assert prof.rid == jane_doe.rid


class TestBuildState:
    """Build state lives outside the published set, and stays droppable."""

    @pytest.fixture
    def contaminated(self, jane_store):
        store, rid = jane_store
        paper_id = store.get(rid).papers[0].paper_id
        assert store.get(rid).set_paper_contaminated(paper_id, True)
        return store, rid, paper_id

    def test_it_lives_in_its_own_table_not_the_document(self, contaminated):
        store, rid, paper_id = contaminated
        assert store.get(rid).build_state.is_contaminated(paper_id)
        with store.session() as s:
            assert s.get(BuildStateRow, rid) is not None
            assert "contaminated" not in s.get(ProfileRow, rid).document
        urls = [p.content_url for p in store.get(rid).build_manifest()]
        assert not any("build_state" in u for u in urls)

    def test_export_omits_it_unless_asked(self, contaminated, tmp_path):
        store, rid, _ = contaminated
        plain = store.export_directory(rid, tmp_path / "plain" / "jane-doe")
        assert not (plain.parent / ".build").exists()

        with_build = store.export_directory(
            rid, tmp_path / "withbuild" / "jane-doe", with_build=True
        )
        assert (with_build.parent / ".build" / "jane-doe" / "meta").is_dir()

    def test_dropping_the_table_leaves_profiles_loadable(self, contaminated):
        store, rid, _ = contaminated
        BuildStateRow.__table__.drop(store.engine)
        prof = store.get(rid)
        assert prof.name and prof.papers
        assert prof.build_state.papers == {}


class TestPrivacy:
    """Tiers on the rows must match what ``privacy.effective_tiers`` computes."""

    def test_row_tiers_match_effective_tiers(self, empty_sql_store, deep_dir):
        prof = ResearcherProfile.from_files(deep_dir)
        rid = empty_sql_store.put(prof)
        expected = effective_tiers(prof.metadata)
        with empty_sql_store.session() as s:
            rows = s.exec(select(ArtifactRow).where(ArtifactRow.profile_rid == rid)).all()
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
        rid = empty_sql_store.import_directory(d)
        assert empty_sql_store.refresh_vectors(rid) is True
        with empty_sql_store.session() as s:
            chunks = s.exec(select(ChunkVectorRow).where(ChunkVectorRow.profile_rid == rid)).all()
            centroids = s.exec(
                select(ProfileVectorRow).where(ProfileVectorRow.profile_rid == rid)
            ).all()
        assert chunks and {c.source_type for c in chunks} == {"soul"}
        assert [c.kind for c in centroids] == ["centroid"]
        assert empty_sql_store.centroid(rid).shape == (fake_backend.dim,)


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
        rows = shred_profile(prof.rid, prof)
        assert [type(r) for r in rows] == [ProfileVectorRow]

        rid = empty_sql_store.put(prof, include_binary=True)
        with empty_sql_store.session() as s:
            chunk_rows = s.exec(
                select(ChunkVectorRow).where(ChunkVectorRow.profile_rid == rid)
            ).all()
        assert chunk_rows == []  # private seed vectors never leave the build
        assert empty_sql_store.centroid(rid).shape == (fake_backend.dim,)
