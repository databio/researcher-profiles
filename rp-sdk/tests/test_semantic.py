"""Hybrid search inside one profile over the store's own vectors (``api/_semantic.py``)."""

import shutil
import time
from pathlib import Path

import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.api import _semantic
from researcher_profiles.embeddings.cache import SqliteEmbeddingIndex

from .factories import (
    ConceptBackend,
    build_profile_dir,
    patch_embedding_backends,
    refresh_manifest,
)

SLUG = "ada-lovelace"
OTHER = "grace-hopper"
TOKEN = "op-token"
OP = {"Authorization": f"Bearer {TOKEN}"}


class _SlowBackend(ConceptBackend):
    def embed(self, texts):
        time.sleep(0.5)
        return super().embed(texts)


PAPERS = [
    {"paper_id": "leroy2020enh", "title": "Blood cell programs", "year": 2020},
    {"paper_id": "corces2018chr", "title": "Chromatin landscape of cancer", "year": 2018},
    {"paper_id": "doe2019nuc", "title": "Tumor maps", "year": 2019},
    {"paper_id": "smith2021gata", "title": "GATA1 binding sites", "year": 2021},
]
SUMMARIES = {
    "leroy2020enh": "Enhancers drive transcriptional regulation in blood cells.\n",
    "corces2018chr": "Chromatin accessibility maps across tumors.\n",
    "doe2019nuc": "Open nucleosome positions in tissue.\n",
    "smith2021gata": "Protein folding study.\n",
}
SOUL = "# Soul\n\n## Values\n\nI share reproducible metadata standards with everyone.\n"


def _profile(root: Path, slug: str, rid: str, *, backend, cv: str | None = None) -> Path:
    d = build_profile_dir(
        root / slug,
        name=slug.replace("-", " ").title(),
        rid=rid,
        level="deep" if cv else "full",
        papers=PAPERS if slug == SLUG else [{"paper_id": "hop1952", "title": "Compilers"}],
        summaries=SUMMARIES if slug == SLUG else {"hop1952": "Protein folding compilers.\n"},
        cv=cv or False,
    )
    (d / "personality" / "SOUL.md").write_text(SOUL, encoding="utf-8")
    refresh_manifest(d)
    SqliteEmbeddingIndex(d).build_index(backend=backend)
    return d


def _build_corpus(dest: Path) -> None:
    with pytest.MonkeyPatch.context() as mp:
        patch_embedding_backends(mp, ConceptBackend())
        cv = "# CV\n\n## Lab\n\nNuclei membrane lysis protocol.\n"
        _profile(dest, SLUG, "0000-0001-2345-6789", backend=ConceptBackend(), cv=cv)
        _profile(dest, OTHER, "0000-0002-1825-0097", backend=ConceptBackend())


@pytest.fixture
def sql_client(golden, tmp_path, concept_backend, empty_sql_store, make_api_client):
    """Two indexed profiles in an in-memory SQL store; the caller's tier from ``X-Tier``."""
    corpus = shutil.copytree(golden("semantic", _build_corpus), tmp_path / "corpus")
    for slug in (SLUG, OTHER):
        empty_sql_store.put(ResearcherProfile.from_files(corpus / slug))
    return make_api_client(empty_sql_store, token=TOKEN, tier_from_header=True)


def _rank(client, q, *, viewer="private", hide_summaries=()):
    """``hybrid_rank_papers`` as the paper list calls it, over the fixture papers."""
    store = client.app.state.store
    prof = store.get(SLUG)
    cands = [
        {
            "paper_id": p["paper_id"],
            "title": p["title"],
            "journal": None,
            "summary": None if p["paper_id"] in hide_summaries else SUMMARIES[p["paper_id"]],
            "abstract": None,
        }
        for p in PAPERS
    ]
    return _semantic.hybrid_rank_papers(store, prof, viewer, q, cands)


class TestHybridRanking:
    def test_paraphrase_ranks_through_the_semantic_side(self, sql_client):
        ranked, mode, note = _rank(sql_client, "how genes get switched on")
        assert (mode, note) == ("hybrid", None)
        assert ranked[0][0] == "leroy2020enh"

    def test_exact_symbol_ranks_through_the_keyword_side(self, sql_client):
        ranked, mode, _ = _rank(sql_client, "GATA1")
        assert mode == "hybrid"
        assert [(pid, by) for pid, _, by in ranked] == [("smith2021gata", ["keyword"])]

    def test_both_sides_beat_either(self, sql_client):
        ranked, _, _ = _rank(sql_client, "chromatin accessibility")
        order = [pid for pid, _, _ in ranked]
        both = dict((pid, by) for pid, _, by in ranked)
        assert both["corces2018chr"] == ["keyword", "semantic"]
        assert both["doe2019nuc"] == ["semantic"]
        assert order.index("corces2018chr") < order.index("doe2019nuc")

    def test_a_summary_the_caller_may_not_read_is_not_a_semantic_match(self, sql_client):
        ranked, _, _ = _rank(
            sql_client, "how genes get switched on", hide_summaries={"leroy2020enh"}
        )
        assert "leroy2020enh" not in [pid for pid, _, _ in ranked]


class TestFallbackIsReported:
    @pytest.mark.parametrize(
        "query_backend, note",
        [
            (None, _semantic.NOTE_NO_ENCODER),
            (_SlowBackend(), _semantic.NOTE_TIMEOUT),
            (ConceptBackend("fake:other"), _semantic.NOTE_MISMATCH),
        ],
        ids=["encoder-disabled", "encoder-timeout", "space-mismatch"],
    )
    def test_keyword_with_a_note(self, sql_client, monkeypatch, query_backend, note):
        monkeypatch.setattr(_semantic, "_query_backend", lambda store: query_backend)
        monkeypatch.setattr(_semantic, "EMBED_TIMEOUT_S", 0.05)
        ranked, mode, got = _rank(sql_client, "chromatin uncached query")
        assert (mode, got) == ("keyword", note)
        assert [pid for pid, _, _ in ranked] == ["corces2018chr"]

    def test_unindexed_profile_falls_back(
        self, tmp_path, concept_backend, empty_sql_store, make_api_client
    ):
        d = build_profile_dir(
            tmp_path / SLUG, rid="0000-0001-2345-6789", papers=PAPERS, summaries=SUMMARIES
        )
        empty_sql_store.put(ResearcherProfile.from_files(d))
        c = make_api_client(empty_sql_store, token=TOKEN)
        ranked, mode, note = _rank(c, "chromatin")
        assert (mode, note) == ("keyword", _semantic.NOTE_NO_VECTORS)
        assert ranked[0][0] == "corces2018chr"


class TestSameSpace:
    @pytest.mark.parametrize(
        "a, b, same",
        [
            ("st:all-MiniLM-L6-v2", "fastembed:all-MiniLM-L6-v2", True),
            ("fastembed:sentence-transformers/all-MiniLM-L6-v2", "st:all-MiniLM-L6-v2", True),
            ("openai:text-embedding-3-small", "st:all-MiniLM-L6-v2", False),
            ("st:all-mpnet-base-v2", "st:all-MiniLM-L6-v2", False),
        ],
        ids=["st-vs-fastembed", "hf-org-prefix", "different-provider", "different-model"],
    )
    def test_one_space(self, a, b, same):
        assert _semantic.same_space(a, b) is same


class TestSearchRouteOnSqlStore:
    def test_answers_with_text(self, sql_client):
        """The route used to 501 on the SQL store; it now ranks the stored rows."""
        r = sql_client.post(
            f"/api/v1/profiles/{SLUG}/search",
            json={"query": "how genes get switched on", "k": 99},
            headers={**OP, "X-Tier": "private"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["k_applied"] == 20
        top = body["hits"][0]
        assert (top["source_type"], top["source_id"]) == ("paper_summary", "leroy2020enh")
        assert top["text"].startswith("Enhancers drive")

    def test_no_hit_from_a_source_the_caller_may_not_read(self, sql_client):
        store = sql_client.app.state.store
        prof = store.get(SLUG)
        prof.edit.set_visibility(
            artifacts=[
                {
                    "content_url": "sources/summaries/leroy2020enh.summary.md",
                    "visibility": "private",
                }
            ]
        )
        store.evict(SLUG)
        r = sql_client.post(
            f"/api/v1/profiles/{SLUG}/search",
            json={"query": "how genes get switched on"},
            headers={**OP, "X-Tier": "public"},
        )
        assert r.status_code == 200, r.text
        assert "leroy2020enh" not in {h["source_id"] for h in r.json()["hits"]}

    def test_a_stale_chunk_is_dropped(self, sql_client):
        store = sql_client.app.state.store
        prof = store.get(SLUG)
        prof.save_summary("leroy2020enh", "Rewritten without the old words at all.\n")
        store.evict(SLUG)
        prof = store.get(SLUG)
        hits, note = _semantic.search_chunks(
            store,
            prof,
            "private",
            "how genes get switched on",
            source_types=["paper_summary"],
            k=5,
        )
        assert "leroy2020enh" not in {h.source_id for h in hits}
        assert note == _semantic.stale_note(1)


class TestSingleProfileOnly:
    def test_passages_never_reach_another_profile(self, sql_client):
        """``hop1952`` (the other profile) is the only text about compilers."""
        r = sql_client.post(
            f"/api/v1/profiles/{SLUG}/passages",
            json={"query": "compilers"},
            headers={"X-Tier": "private"},
        )
        assert r.status_code == 200, r.text
        assert all("ompiler" not in p["text"] for p in r.json()["passages"])

    def test_a_hidden_profile_is_the_same_404_as_a_missing_one(self, sql_client):
        store = sql_client.app.state.store
        store.get(SLUG).edit.set_visibility(profile_visibility="private")
        store.evict(SLUG)
        hidden = sql_client.post(f"/api/v1/profiles/{SLUG}/passages", json={"query": "x"})
        missing = sql_client.post("/api/v1/profiles/nobody-here/passages", json={"query": "x"})
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json()["detail"].replace(SLUG, "") == missing.json()["detail"].replace(
            "nobody-here", ""
        )


class TestSeedPapersAreRankOnly:
    """An interest seed's abstract is never returned as the researcher's own text."""

    def test_owner_search_never_returns_a_seed(self, tmp_path, concept_backend, make_api_client):
        from researcher_profiles.schema import SeedPapersDocument
        from researcher_profiles.schema.jsonld import canonical_dumps

        root = tmp_path / "profiles"
        d = build_profile_dir(
            root / SLUG,
            rid="0000-0001-2345-6789",
            papers=PAPERS,
            summaries=SUMMARIES,
        )
        doc = SeedPapersDocument.model_validate(
            {
                "hasPart": [
                    {
                        "title": "Protein folding atlas",
                        "openalex_id": "W5",
                        "abstract": "Protein folding protein folding.",
                    }
                ]
            }
        )
        (d / "sources" / "seed_papers.jsonld").write_text(
            canonical_dumps(doc.model_dump(mode="json")), encoding="utf-8"
        )
        refresh_manifest(d)
        SqliteEmbeddingIndex(d).build_index(backend=concept_backend)
        client = make_api_client(root, token=TOKEN, tier_from_header=True)
        r = client.post(
            f"/api/v1/profiles/{SLUG}/search",
            json={"query": "protein folding", "k": 20},
            headers={**OP, "X-Tier": "private"},
        )
        assert r.status_code == 200, r.text
        types = {h["source_type"] for h in r.json()["hits"]}
        assert types and "seed_abstract" not in types
