"""The SQL profile store against a real Postgres.

SQLite cannot answer two questions the store depends on, and both are silent
failures rather than loud ones:

* **Type portability.** ``sqlmodel.JSON`` and ``LargeBinary`` map to different
  native types per backend. A column that only ever met SQLite has never been
  proven to exist on Postgres at all.
* **UNIQUE with NULL.** ``UNIQUE(profile_rid, grant_id)`` and the ``slug``
  constraint behave differently across backends once a NULL is involved.

Set ``RESEARCHER_PROFILES_TEST_DATABASE_URL`` to a Postgres URL to run these; without it they
skip. The tables are created and dropped inside the test, so the target
database must be one you are willing to have ``rp_*`` tables in.
"""

import hashlib
import os
from pathlib import Path

import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.store.db import ProfileRow
from researcher_profiles.store.sql import SqlProfileStore

TEST_DATABASE_URL = os.environ.get("RESEARCHER_PROFILES_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="set RESEARCHER_PROFILES_TEST_DATABASE_URL to a Postgres URL to run the store integration tests",
)


@pytest.fixture
def pg_store():
    store = SqlProfileStore(TEST_DATABASE_URL)
    store.create_all()
    yield store
    for row in store.list_profiles():
        store.delete(row.rid)


def _relative_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestPostgresStore:
    def test_create_all_works_on_postgres(self, pg_store):
        from sqlalchemy import inspect

        names = set(inspect(pg_store.engine).get_table_names())
        assert {"rp_profiles", "rp_papers", "rp_grants", "rp_artifacts"} <= names

    def test_the_full_round_trip_holds(self, pg_store, jane_doe, jane_doe_dir, tmp_path):
        rid = pg_store.put(jane_doe)
        out = pg_store.export_directory(rid, tmp_path / "out")
        assert _relative_files(jane_doe_dir) == _relative_files(out)
        for rel in _relative_files(jane_doe_dir):
            assert _sha256(jane_doe_dir / rel) == _sha256(out / rel), rel

    def test_json_columns_survive_the_round_trip(self, pg_store, jane_doe):
        rid = pg_store.put(jane_doe)
        with pg_store.session() as s:
            row = s.get(ProfileRow, rid)
            assert row.document == jane_doe.persisted_document()
            assert isinstance(row.subfields, list)

    def test_duplicate_paper_ids_are_accepted(self, pg_store, tmp_path):
        from ..factories import build_profile_dir

        rows = [
            {"name": "First", "datePublished": "2020", "paper_id": "dup2020"},
            {"name": "Second", "datePublished": "2021", "paper_id": "dup2020"},
        ]
        src = build_profile_dir(tmp_path / "dupes", papers=rows, summaries=False)
        rid = pg_store.put(ResearcherProfile.from_files(src))
        assert len(pg_store.get(rid).papers) == 2
