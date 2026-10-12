"""Fixes from the round-two bug review (2026-10-09).

One chunk tier rule for every path that serves, publishes or scores a chunk;
``/rank-works`` on a SQL store; OpenAlex filter escaping; a SQL merge that
cannot take a retired slug; and two query-cache bugs.
"""

from pathlib import Path

import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.privacy import chunk_source_tiers
from researcher_profiles.schema import ProfileDocument

from .factories import FakeBackend, build_profile_dir, copy_fixture

SUMMARY = "sources/summaries/doe2016example.summary.md"


def _profile(tmp_path: Path, *, sections=None, artifacts=None) -> ResearcherProfile:
    """A writable ``jane-doe`` with the given tiers set."""
    d = copy_fixture("jane-doe", tmp_path)
    if sections or artifacts:
        ResearcherProfile.from_files(d).edit.set_visibility(sections=sections, artifacts=artifacts)
    return ResearcherProfile.from_files(d)


def _build_index(d: Path) -> ResearcherProfile:
    from researcher_profiles.embeddings.cache import SqliteEmbeddingIndex

    SqliteEmbeddingIndex(d).build_index(backend=FakeBackend())
    return ResearcherProfile.from_files(d)


# ---------------------------------------------------------------------------
# Part A: one chunk tier rule
# ---------------------------------------------------------------------------


class TestChunkSourceTiers:
    def test_soul_section_set_private(self, tmp_path):
        prof = _profile(tmp_path, sections=[{"section": "soul", "visibility": "private"}])
        tiers = chunk_source_tiers(prof.metadata, [("soul", "soul"), ("cv", "cv")])
        assert tiers[("soul", "soul")] == "private"

    def test_one_summary_set_private(self, tmp_path):
        prof = _profile(tmp_path, artifacts=[{"content_url": SUMMARY, "visibility": "private"}])
        tiers = chunk_source_tiers(
            prof.metadata,
            [("paper_summary", "doe2016example"), ("paper_summary", "doe2019methods")],
        )
        assert tiers[("paper_summary", "doe2016example")] == "private"
        assert tiers[("paper_summary", "doe2019methods")] == "public"

    def test_summary_derived_from_a_private_source(self):
        doc = ProfileDocument.model_validate(
            {
                "name": "X",
                "rid": "local:x-abc123",
                "provenance": "synthetic",
                "hasPart": [
                    {"contentUrl": "sources/cv.md", "role": "cv", "visibility": "private"},
                    {
                        "contentUrl": "sources/summaries/p1.summary.md",
                        "role": "paper_summary",
                        "paperId": "p1",
                        "visibility": "public",
                        "derivedFrom": ["cv"],
                    },
                ],
            }
        )
        assert chunk_source_tiers(doc, [("paper_summary", "p1")])[("paper_summary", "p1")] == (
            "private"
        )

    def test_unknown_source_fails_closed(self, tmp_path):
        prof = _profile(tmp_path)
        assert chunk_source_tiers(prof.metadata, [("mystery", "m")])[("mystery", "m")] == "private"
        assert chunk_source_tiers(None, [("mystery", "m")])[("mystery", "m")] == "private"

    def test_no_profile_uses_role_defaults(self):
        tiers = chunk_source_tiers(None, [("paper_summary", "p1")])
        assert tiers[("paper_summary", "p1")] == "public"


@pytest.fixture
def soul_private_root(tmp_path, fake_backend):
    """A served root whose ``jane-doe`` has a private SOUL and one private summary, indexed."""
    root = tmp_path / "root"
    root.mkdir()
    d = copy_fixture("jane-doe", root)
    ResearcherProfile.from_files(d).edit.set_visibility(
        sections=[{"section": "soul", "visibility": "private"}],
        artifacts=[{"content_url": SUMMARY, "visibility": "private"}],
    )
    _build_index(d)
    return root


_HIDDEN = {("soul", "soul"), ("paper_summary", "doe2016example")}


class TestMatchEvidence:
    def test_public_caller_sees_no_private_chunk_and_scores_without_them(
        self, soul_private_root, make_api_client
    ):
        c = make_api_client(soul_private_root, tier_from_header=True)
        body = {"query": "chromatin accessibility", "include_chunks": True, "topk_chunks": 10}
        body["normalize"] = False
        public = c.post("/api/v1/match", json=body, headers={"X-Tier": "public"})
        owner = c.post("/api/v1/match", json=body, headers={"X-Tier": "private"})
        assert public.status_code == owner.status_code == 200, public.text
        pm, om = public.json()["matches"][0], owner.json()["matches"][0]

        chunks = pm["evidence"]["top_chunks"]
        assert chunks
        assert not {(h["source_type"], h["source_id"]) for h in chunks} & _HIDDEN
        assert "doe2016example" not in pm["evidence"]["top_papers"]

        # The owner's view of the same query does include hidden chunks, so
        # the public score is computed over a strictly different chunk set.
        assert {(h["source_type"], h["source_id"]) for h in om["evidence"]["top_chunks"]} & _HIDDEN
        expected = 0.5 * pm["evidence"]["centroid_score"] + 0.5 * (
            sum(h["score"] for h in chunks) / len(chunks)
        )
        assert pm["score"] == pytest.approx(expected, abs=1e-4)

    def test_rank_viewer_equals_owner_hits_minus_hidden(self, soul_private_root, fake_backend):
        from researcher_profiles.store import FilesystemProfileStore

        store = FilesystemProfileStore(soul_private_root)
        everything = store.match.rank("chromatin", k=1, topk_chunks=1000, normalize=False)
        public = store.match.rank(
            "chromatin", k=1, topk_chunks=1000, normalize=False, viewer=lambda p: "public"
        )
        all_hits = everything[0].evidence.top_chunks
        kept = [h for h in all_hits if (h.source_type, h.source_id) not in _HIDDEN]
        assert len(kept) < len(all_hits)
        assert [
            (h.source_type, h.source_id, h.chunk_index) for h in public[0].evidence.top_chunks
        ] == [(h.source_type, h.source_id, h.chunk_index) for h in kept]


class TestPublishedVectors:
    def test_flat_export_drops_private_sources(self, soul_private_root, fake_backend, tmp_path):
        from researcher_profiles.embeddings.flat import public_chunk_keys, write_flat_export

        d = soul_private_root / "jane-doe"
        md = ResearcherProfile.from_files(d).metadata
        assert write_flat_export(d, profile_document=md, out_dir=tmp_path / "e")
        keys = {(st, sid) for st, sid, _ in public_chunk_keys(d, profile_document=md)}
        assert keys and not keys & _HIDDEN

    def test_sql_public_subset_drops_private_sources(self, soul_private_root, make_sql_store):
        store = make_sql_store()
        store.import_directory(soul_private_root / "jane-doe")
        index = store.vector_index("jane-doe")
        keys = {(c["source_type"], c["source_id"]) for c in index.chunks}
        assert keys and not keys & _HIDDEN


class TestPaperSearchCredit:
    def test_private_summary_vector_gives_no_credit_despite_inline_summary(
        self, tmp_path, monkeypatch
    ):
        import researcher_profiles.api._semantic as sem
        from researcher_profiles.embeddings.flat import FlatHit

        prof = _profile(tmp_path, artifacts=[{"content_url": SUMMARY, "visibility": "private"}])
        hit = FlatHit(
            source_type="paper_summary",
            source_id="doe2016example",
            chunk_index=0,
            section=None,
            cosine=0.9,
            score=0.95,
        )
        monkeypatch.setattr(sem, "semantic_hits", lambda *a, **kw: ([hit], None))
        candidates = [
            {
                "paper_id": "doe2016example",
                "title": "t",
                "journal": None,
                # The inline record summary a public caller may read.
                "summary": "inline summary text",
                "abstract": None,
            }
        ]
        ranked, mode, _ = sem.hybrid_rank_papers(None, prof, "public", "zzqx", candidates)
        assert mode == "hybrid"
        assert ranked == []
        ranked, _, _ = sem.hybrid_rank_papers(None, prof, "private", "zzqx", candidates)
        assert [r[0] for r in ranked] == ["doe2016example"]


# ---------------------------------------------------------------------------
# Part B: /rank-works
# ---------------------------------------------------------------------------


@pytest.fixture
def rank_sql_store(tmp_path, fake_backend, make_sql_store):
    prof = _build_index(copy_fixture("jane-doe", tmp_path))
    store = make_sql_store()
    store.put(prof)
    return store


_WORKS = [
    {"title": "Chromatin accessibility in development", "year": 2025},
    {"title": "Protein folding dynamics", "year": 2025},
]


class TestRankWorks:
    def test_sql_store_ranks(self, rank_sql_store, make_api_client):
        c = make_api_client(rank_sql_store)
        r = c.post("/api/v1/profiles/jane-doe/rank-works", json={"works": _WORKS})
        assert r.status_code == 200, r.text
        assert {w["title"] for w in r.json()["works"]} == {w["title"] for w in _WORKS}

    def test_summary_kind_is_refused(self, rank_sql_store, make_api_client):
        c = make_api_client(rank_sql_store)
        r = c.post(
            "/api/v1/profiles/jane-doe/rank-works", json={"works": _WORKS, "kind": "summary"}
        )
        assert r.status_code == 422

    def test_no_centroid_is_503_before_any_openalex_call(
        self, tmp_path, fake_backend, make_sql_store, make_api_client, monkeypatch
    ):
        import researcher_profiles.openalex_client as oc

        def _never(*a, **kw):
            raise AssertionError("OpenAlex was called")

        monkeypatch.setattr(oc, "OpenAlexClient", _never)
        store = make_sql_store()
        store.import_directory(copy_fixture("jane-doe", tmp_path, drop_index=True))
        c = make_api_client(store)
        r = c.post("/api/v1/profiles/jane-doe/rank-works", json={"use_openalex": True})
        assert r.status_code == 503, r.text


# ---------------------------------------------------------------------------
# Part C: OpenAlex filter escaping
# ---------------------------------------------------------------------------


def test_topic_filters_escape_filter_syntax():
    from researcher_profiles.openalex import _topic_filters

    out = _topic_filters(["DNA methylation, aging", "a|b", "x:y", " , "])
    assert out == ["title_and_abstract.search:DNA methylation aging|a b|x y"]


# ---------------------------------------------------------------------------
# Part D: SQL merge
# ---------------------------------------------------------------------------


def _doc(rid: str, name: str) -> ProfileDocument:
    return ProfileDocument(name=name, rid=rid, provenance="synthetic")


def _stage(tmp_path: Path, rid: str, name: str) -> Path:
    return build_profile_dir(
        tmp_path / f"stage-{rid.replace(':', '-')}",
        name=name,
        rid=rid,
        level="lite",
        papers=False,
        personality=False,
        summaries=False,
        provenance="synthetic",
    )


X, Y, A, B = (f"local:{n}-{n * 6}" for n in "abcd")


@pytest.fixture
def merged_x_into_y(make_sql_store, tmp_path):
    store = make_sql_store()
    for rid, slug in ((X, "x"), (Y, "y"), (A, "a"), (B, "b")):
        store.create(_doc(rid, slug), slug=slug)
    store.merge_into(
        "x", _stage(tmp_path, Y, "y"), survivor_rid=Y, survivor_slug="y", build_missing_index=False
    )
    return store


class TestMergeSlugs:
    def test_survivor_cannot_take_a_slug_that_redirects_elsewhere(self, merged_x_into_y, tmp_path):
        from researcher_profiles.store import RetiredRidError

        with pytest.raises(RetiredRidError):
            merged_x_into_y.merge_into(
                "a",
                _stage(tmp_path, B, "b"),
                survivor_rid=B,
                survivor_slug="x",
                build_missing_index=False,
            )
        assert merged_x_into_y.rid_for("x") == Y
        assert merged_x_into_y.get("b").rid == B

    def test_survivor_old_slug_stays_an_alias(self, merged_x_into_y, tmp_path):
        store = merged_x_into_y
        store.merge_into(
            "a",
            _stage(tmp_path, B, "b"),
            survivor_rid=B,
            survivor_slug="c",
            build_missing_index=False,
        )
        assert store.get("c").rid == B
        assert store.rid_for("b") == B
        assert store.rid_for("a") == B
        assert store.rid_for("x") == Y

    def test_survivor_may_take_back_its_own_old_slug(self, merged_x_into_y, tmp_path):
        store = merged_x_into_y
        store.merge_into(
            "a",
            _stage(tmp_path, B, "b"),
            survivor_rid=B,
            survivor_slug="c",
            build_missing_index=False,
        )
        store.create(_doc("local:e-eeeeee", "d"), slug="d")
        store.merge_into(
            "d",
            _stage(tmp_path, B, "b"),
            survivor_rid=B,
            survivor_slug="b",
            build_missing_index=False,
        )
        assert store.get("b").rid == B
        assert store.rid_for("c") == B


# ---------------------------------------------------------------------------
# Part F: query caches
# ---------------------------------------------------------------------------


class _Recorder:
    name = "fake:cased"

    def __init__(self):
        self.seen: list[str] = []

    def embed(self, texts):
        self.seen.extend(texts)
        return [[1.0 if t.isupper() else 0.0, 1.0] for t in texts]


def test_encode_caches_by_the_text_it_embeds(monkeypatch):
    import researcher_profiles.api._semantic as sem

    rec = _Recorder()
    monkeypatch.setattr(sem, "_query_backend", lambda store: rec)
    monkeypatch.setattr(sem, "_query_cache", {})
    upper, _, _ = sem.encode(None, "  BRCA1 ")
    lower, _, _ = sem.encode(None, "brca1")
    again, _, _ = sem.encode(None, "BRCA1")
    assert rec.seen == ["BRCA1", "brca1"]
    assert list(upper) != list(lower)
    assert list(again) == list(upper)


def test_profile_vectors_serves_a_cached_none_without_reloading():
    import researcher_profiles.api._semantic as sem

    calls = []

    class _Store:
        generation = 1

        def vector_index(self, ref):
            calls.append(ref)
            raise FileNotFoundError(ref)

    store = _Store()
    assert sem.profile_vectors(store, "p") is None
    assert sem.profile_vectors(store, "p") is None
    assert calls == ["p"]
