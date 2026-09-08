"""The real-profile integration suite: loading, embeddings search, and the
FastAPI surface (including ``/api/v1/match``) against actual on-disk profiles.

Gated by ``tests/integration/conftest.py``: every item here is auto-marked
``integration``, which ``pyproject.toml`` deselects by default; run with
``-m integration`` (or ``scripts/test-integration.sh``). The real profiles
are pointed at by ``RESEARCHER_PROFILES_TEST_*`` environment variables and
are never committed to this repo.

``fastapi`` is imported lazily, inside the tests that need it, so this module
still collects on a bare core install (no extras).
"""

import sqlite3

import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.client import ApiArtifactStorage
from researcher_profiles.utils.paths import cache_dir


@pytest.fixture(scope="session")
def indexed_api_root(tmp_path_factory, copy_real_profile, primary_slug, secondary_slug):
    from researcher_profiles import ResearcherProfile

    root = tmp_path_factory.mktemp("match_api_root")
    for slug in (primary_slug, secondary_slug):
        path = copy_real_profile(slug, root)
        prof = ResearcherProfile.from_files(path)
        prof.index.build(force=True)
    return root


@pytest.fixture(scope="session")
def indexed_api_app(indexed_api_root):
    from researcher_profiles.api.app import create_app

    return create_app(indexed_api_root, token=None)


class TestProfileLoad:
    """Smoke tests: real on-disk profiles load and self-validate."""

    def test_primary_profile_loads(self, real_profile, primary_slug):
        assert real_profile.slug == primary_slug
        assert real_profile.metadata.name
        # Expertise on disk may be markdown text; require only that it is present.
        assert real_profile.expertise and isinstance(real_profile.expertise, str)
        assert isinstance(real_profile.papers, list)
        assert len(real_profile.papers) > 0


class TestSearch:
    """End-to-end embeddings tests: build a real index and run real searches.

    These share one ``built_index_profile`` session fixture so the
    sentence-transformers model loads (and ~80MB downloads on first run)
    exactly once.
    """

    def test_build_index_creates_sqlite(self, built_index_profile):
        # Derive the location from paths.cache_dir rather than hardcoding it:
        # this test went stale and silently un-run through the meta/ -> .cache/
        # migration, and deriving it means the next move breaks the test loudly
        # instead of leaving it wrong-but-skipped.
        db_path = cache_dir(built_index_profile.directory) / "embeddings.sqlite"
        assert db_path.is_file(), f"no index at {db_path}"
        assert db_path.stat().st_size > 0
        conn = sqlite3.connect(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        finally:
            conn.close()
        assert n > 0, "expected at least one embedded chunk"

    def test_search_returns_relevant_hits(self, built_index_profile):
        hits = built_index_profile.index.search("epigenomics", k=5)
        assert len(hits) >= 1
        top = hits[0]
        assert top.text
        assert top.score > 0.3

    def test_search_similar(self, built_index_profile):
        hits = built_index_profile.index.search("chromatin accessibility", k=3)
        assert hits, "need a seed hit to test search_similar"
        seed = hits[0]
        similar = built_index_profile.index.search_similar(
            seed.source_type, seed.source_id, chunk_index=seed.chunk_index, k=3
        )
        assert len(similar) >= 1
        # search_similar passes an explicit `skip=` predicate that excludes the
        # seed chunk itself, so this is not a self-similarity check: either
        # another chunk of the same source document comes back, or some hit is
        # a genuinely close neighbour.
        assert any(h.source_id == seed.source_id or h.score > 0.5 for h in similar)


class TestApi:
    """FastAPI surface tested against real, on-disk profiles.

    Asserts that ``GET /api/v1/profiles/...`` routes return the same shape
    ``ResearcherProfile.from_files(...)`` exposes, and that a
    An ``ApiArtifactStorage``-backed profile constructed against the in-process TestClient
    sees the same fields as a direct ``from_files`` load.
    """

    def test_remote_profile_parity(self, api_app, api_root, primary_slug):
        from fastapi.testclient import TestClient

        local = ResearcherProfile.from_files(api_root / primary_slug)
        http = TestClient(api_app)
        remote = ResearcherProfile(
            ApiArtifactStorage(slug=primary_slug, base_url="http://testserver", client=http)
        )
        try:
            assert remote.metadata.name == local.metadata.name
            assert remote.metadata.affiliation == local.metadata.affiliation
            # expertise: remote returns expertise.md body (string); compare
            # at least that both are present and non-trivial.
            assert bool(remote.expertise) == bool(local.expertise)
            local_titles = [p.title for p in local.papers[:3]]
            remote_titles = [p.title for p in remote.papers[:3]]
            assert local_titles == remote_titles
        finally:
            remote.close()


class TestApiMatch:
    """End-to-end /api/v1/match against real, freshly-indexed profiles.

    Gated (like the rest of this directory) behind ``-m integration`` and
    the RESEARCHER_PROFILES_TEST_PROFILE_DIR real-profile pointers. Builds real
    sentence-transformers indexes, so it is slow and network-touching on first run.
    """

    def test_match_returns_ranked_results(self, indexed_api_app):
        from fastapi.testclient import TestClient

        with TestClient(indexed_api_app) as c:
            # diversify=True (the default) applies MMR reordering once the
            # candidate pool exceeds k, which can legitimately break descending
            # order. Turn it off so the assertion below is about ranking rather
            # than about how many profiles happen to be in the root.
            r = c.post(
                "/api/v1/match",
                json={"query": "genomics", "k": 5, "diversify": False},
            )
        assert r.status_code == 200
        matches = r.json()["matches"]
        assert isinstance(matches, list)
        assert matches, "expected at least one ranked profile"
        m = matches[0]
        assert m["slug"]
        assert m["name"]
        assert isinstance(m["score"], (int, float))
        # Default include_chunks=False -> no chunk evidence.
        assert m["evidence"]["top_chunks"] == []
        # Scores are sorted descending.
        scores = [x["score"] for x in matches]
        assert scores == sorted(scores, reverse=True)
