"""Passages inside one paper or one profile (``api/_passages.py``, ``POST .../passages``).

Pins what a caller builds on: each passage's ``offset`` (with ``section``)
points at that exact text in the stored source, so a text read from there
starts at the passage; full text is searched by keyword only and says so;
sources the caller may not read are not searched; ``k`` is clamped and
reported; every passage is at most ``SNIPPET_CHARS``.
"""

from pathlib import Path

import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.api import _semantic
from researcher_profiles.api._limits import SNIPPET_CHARS
from researcher_profiles.api.caller import Caller
from researcher_profiles.embeddings.cache import SqliteEmbeddingIndex

from .factories import ConceptBackend, build_profile_dir, refresh_manifest

SLUG = "ada-lovelace"
PID = "corces2018chr"
FILLER = (
    "This paragraph describes background material on cancer biology and cohort "
    "selection, with enough words to make a realistic paragraph of moderate size. "
) * 3
FULL_TEXT = (
    "# Chromatin landscape of cancer\n\n"
    + "\n\n".join([FILLER] * 4)
    + "\n\n## Methods\n\n"
    + FILLER
    + "\n\nWe performed the ATAC-seq protocol with Tn5 tagmentation on frozen tumors.\n\n"
    + "\n\n".join([FILLER] * 3)
    + "\n\n## Results\n\n"
    + "\n\n".join([FILLER] * 3)
    + "\n"
)
SOUL = "# Soul\n\n## Values\n\nI share reproducible metadata standards with everyone.\n"
CV = "# CV\n\n## Lab\n\nNuclei membrane lysis protocol development.\n"


@pytest.fixture
def client(tmp_path: Path, monkeypatch, make_api_client):
    """One indexed profile in an in-memory SQL store; the caller's tier from ``X-Tier``."""
    import researcher_profiles.embeddings.cache as cache
    from researcher_profiles.store.sql import SqlProfileStore

    backend = ConceptBackend()
    monkeypatch.setattr(cache, "get_backend", lambda spec=None: backend)
    monkeypatch.setattr(_semantic, "_query_backend", lambda store: backend)
    d = build_profile_dir(
        tmp_path / SLUG,
        rid="0000-0001-2345-6789",
        level="deep",
        papers=[
            {
                "paper_id": PID,
                "title": "Chromatin landscape of cancer",
                "year": 2018,
                "abstract": "Chromatin accessibility of primary tumors.",
            }
        ],
        summaries={PID: "Maps of open nucleosome positions across tumors.\n"},
        cv=CV,
    )
    (d / "personality" / "SOUL.md").write_text(SOUL, encoding="utf-8")
    (d / "sources" / "papers").mkdir(parents=True, exist_ok=True)
    (d / "sources" / "papers" / f"{PID}.md").write_text(FULL_TEXT, encoding="utf-8")
    refresh_manifest(d)
    SqliteEmbeddingIndex(d).build_index(backend=backend)
    store = SqlProfileStore("sqlite://")
    store.create_all()
    store.put(ResearcherProfile.from_files(d))
    c = make_api_client(store)
    c.app.state.hooks.caller_resolver = lambda request: Caller(
        tier=request.headers.get("X-Tier", "public")
    )
    return c


def _paper(client, query, *, tier="private", **body):
    return client.post(
        f"/api/v1/profiles/{SLUG}/papers/{PID}/passages",
        json={"query": query, **body},
        headers={"X-Tier": tier},
    )


def _profile(client, query, *, tier="private"):
    return client.post(
        f"/api/v1/profiles/{SLUG}/passages", json={"query": query}, headers={"X-Tier": tier}
    )


class TestPaperPassages:
    def test_full_text_passage_points_at_its_text(self, client):
        r = _paper(client, "ATAC-seq protocol Tn5 tagmentation")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["search_mode_used"] == "hybrid+keyword_fulltext"
        assert body["searched"] == ["full_text", "summary", "abstract"]
        top = next(p for p in body["passages"] if p["source"] == "full_text")
        assert (top["source"], top["section"], top["matched_by"]) == (
            "full_text",
            "Methods",
            ["keyword"],
        )
        assert FULL_TEXT[top["offset"] : top["offset"] + len(top["text"])] == top["text"]
        assert "Tn5 tagmentation" in top["text"]

    def test_every_passage_is_bounded(self, client):
        body = _paper(client, "cancer biology cohort", k=10).json()
        assert body["passages"]
        assert all(len(p["text"]) <= SNIPPET_CHARS for p in body["passages"])
        for p in body["passages"]:
            if p["source"] == "full_text":
                assert FULL_TEXT[p["offset"] : p["offset"] + len(p["text"])] == p["text"]

    def test_summary_found_by_meaning(self, client):
        """No word of the query is in the summary; the semantic side finds it."""
        body = _paper(client, "chromatin accessibility").json()
        summary = [p for p in body["passages"] if p["source"] == "summary"]
        assert summary and "semantic" in summary[0]["matched_by"]

    def test_full_text_the_caller_may_not_read_is_not_searched(self, client):
        body = _paper(client, "ATAC-seq protocol", tier="public").json()
        assert "full_text" not in body["searched"]
        assert all(p["source"] != "full_text" for p in body["passages"])
        assert "Full text not available to you." in body["note"]
        assert body["search_mode_used"] == "hybrid"

    def test_k_is_clamped_and_reported(self, client):
        assert _paper(client, "cancer", k=99).json()["k_applied"] == 10
        assert _paper(client, "cancer").json()["k_applied"] == 3

    def test_unknown_paper_is_404(self, client):
        r = client.post(
            f"/api/v1/profiles/{SLUG}/papers/nope2000/passages",
            json={"query": "x"},
            headers={"X-Tier": "private"},
        )
        assert r.status_code == 404


class TestProfilePassages:
    def test_soul_passage_points_into_the_narrative(self, client):
        body = _profile(client, "reproducible metadata standards").json()
        assert body["search_mode_used"] == "hybrid"
        top = body["passages"][0]
        assert (top["source"], top["section"]) == ("soul", "soul")
        assert SOUL[top["offset"] : top["offset"] + len(top["text"])] == top["text"]

    def test_private_source_is_keyword_only_for_the_owner(self, client):
        body = _profile(client, "lysis protocol").json()
        cv = [p for p in body["passages"] if p["source"] == "cv"]
        assert cv and cv[0]["matched_by"] == ["keyword"]
        assert "cv searched by keyword only: no stored vectors for private sources." in body["note"]

    def test_private_source_is_not_searched_for_a_stranger(self, client):
        body = _profile(client, "lysis protocol", tier="public").json()
        assert "cv" not in body["searched"]
        assert all(p["source"] != "cv" for p in body["passages"])
