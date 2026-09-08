"""Cross-profile ranking off a store, and the score calibration under it.

``store.match`` / ``store.centroids`` / ``store.indexes`` answer questions
across every profile in a store; ``calibration`` is what makes a raw
``match.rank`` score comparable between peers. They live together because a
calibration bug only ever shows up as a bad ranking.

Identity resolution (``resolve_slug`` / ``rid_for`` / ``path_for`` /
``write_lookup_index`` and duplicate-rid detection) is the store's own job and
is tested in ``test_store.py``.
"""

import json
import logging
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from researcher_profiles.analytics.centroids import CentroidManager
from researcher_profiles.embeddings.cache import SearchHit
from researcher_profiles.generative import calibration as cal
from researcher_profiles.store import FilesystemProfileStore, ProfileNotFoundError
from researcher_profiles.utils.paths import cache_dir

from .factories import write_papers, write_profile

# --------------------------------------------------------------------------
# The roster the analytics compute over
# --------------------------------------------------------------------------


@pytest.fixture
def registry_root(fixture_profiles_root):
    """A profiles root containing the two synthetic fixture profiles."""
    return fixture_profiles_root("jane-doe", "john-smith")


def test_the_roster_holds_every_profile(registry_root):
    store = FilesystemProfileStore(registry_root)
    roster = store._rostered()
    assert len(roster) == 2
    assert set(roster.slugs) == {"jane-doe", "john-smith"}


def test_the_roster_is_slug_sorted(registry_root):
    roster = FilesystemProfileStore(registry_root)._rostered()
    assert roster.slugs == sorted(roster.slugs)


def test_the_roster_skips_non_profile_and_dot_dirs(registry_root):
    # A dir without profile.jsonld and a dotdir must both be ignored.
    (registry_root / "not-a-profile").mkdir()
    (registry_root / ".hidden").mkdir()
    (registry_root / ".hidden" / "profile.jsonld").write_text("{}")
    roster = FilesystemProfileStore(registry_root)._rostered()
    assert set(roster.slugs) == {"jane-doe", "john-smith"}


def test_the_roster_keeps_unindexed_profiles(registry_root):
    """An unindexed profile stays in the roster and gets a zero centroid row.

    Dropping it here is what the dissolved registry's ``skip_unindexed`` did,
    and it made "this deployment has no indexes" arrive as "nobody matched".
    A zero row can never rank, and the serving layer asks the question
    explicitly instead (``api.deps.get_match_store``).
    """
    store = FilesystemProfileStore(registry_root)
    assert not any(store.has_vector_index(s) for s in store.list_slugs())
    assert len(store._rostered()) == 2


# --------------------------------------------------------------------------
# Lite profiles: abstracts, not summaries
# --------------------------------------------------------------------------


def _write_lite_profile(root: Path, slug: str = "lite-researcher") -> Path:
    """A lite profile bundle: papers.jsonld with abstracts, no summaries/persona."""
    p = root / slug
    (p / "sources").mkdir(parents=True)
    write_profile(p, name="Lite Researcher", rid="0000-0001-2345-6789", level="lite")
    write_papers(
        p,
        [
            {
                "paper_id": "paper2020a",
                "title": "A method paper",
                "abstract": "We develop a new ATAC-seq peak caller with better recall.",
            },
            {
                "paper_id": "paper2021b",
                "title": "Another paper",
                "abstract": "A single-cell chromatin accessibility atlas of the cortex.",
            },
        ],
    )
    return p


def _write_index(profile_dir: Path, rows: list[tuple[str, str]]) -> None:
    """Write a minimal embeddings.sqlite holding (source_type, source_id) chunks."""
    cache = cache_dir(profile_dir)
    cache.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cache / "embeddings.sqlite"))
    try:
        conn.execute("CREATE TABLE chunks (source_type TEXT, source_id TEXT, text TEXT)")
        conn.executemany(
            "INSERT INTO chunks (source_type, source_id, text) VALUES (?, ?, 'x')",
            rows,
        )
        conn.execute("CREATE TABLE index_meta (key TEXT, value TEXT)")
        conn.execute("INSERT INTO index_meta (key, value) VALUES ('backend_name', 'fake:tiny')")
        conn.commit()
    finally:
        conn.close()


# A lite index yields paper_abstract hits, never paper_summary.
_ABSTRACT_HITS = [
    SearchHit(
        text="We develop a new ATAC-seq peak caller with better recall.",
        source_type="paper_abstract",
        source_id="paper2020a",
        chunk_index=0,
        section=None,
        cosine=0.64,
        score=0.82,
    ),
    SearchHit(
        text="A single-cell chromatin accessibility atlas of the cortex.",
        source_type="paper_abstract",
        source_id="paper2021b",
        chunk_index=0,
        section=None,
        cosine=0.42,
        score=0.71,
    ),
]

# Two chunks of the same paper: the paper id must not be reported twice.
_DUPE_CHUNK_HITS = [
    SearchHit(
        text="chunk one",
        source_type="paper_abstract",
        source_id="paper2020a",
        chunk_index=0,
        section=None,
        cosine=0.8,
        score=0.9,
    ),
    SearchHit(
        text="chunk two",
        source_type="paper_abstract",
        source_id="paper2020a",
        chunk_index=1,
        section=None,
        cosine=0.6,
        score=0.8,
    ),
]

# The full/deep shape: a paper_summary hit alongside a non-paper chunk type.
_MIXED_HITS = [
    SearchHit(
        text="summary",
        source_type="paper_summary",
        source_id="paper2019z",
        chunk_index=0,
        section=None,
        cosine=0.8,
        score=0.9,
    ),
    SearchHit(
        text="soul",
        source_type="soul",
        source_id="SOUL",
        chunk_index=0,
        section=None,
        cosine=0.0,
        score=0.5,
    ),
]


class TestLitePapers:
    """Regression tests for lite-profile handling in the cross-profile analytics.

    A ``lite`` profile indexes paper *abstracts* (``paper_abstract`` chunks) and
    has no LLM-written summaries, so any code that keys on ``paper_summary`` alone
    degrades silently for it. These tests pin the three places where that
    can happen:

    1. ``match.rank`` building ``top_papers`` from ``paper_summary`` only, so a
       lite profile arrives on the wire with empty paper evidence.
    2. ``indexes.coverage`` counting ``n_papers`` the same way, so a lite profile
       reports 0 papers.
    3. A calibration failure swallowed by a bare ``except``, silently ranking
       an uncalibrated raw score against normalized peers.

    These use hand-built profiles and monkeypatched embedding/search so they need
    no model, no GPU, and no network.
    """

    @pytest.fixture
    def lite_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FilesystemProfileStore:
        """A single-lite-profile store with embedding + search stubbed out."""
        root = tmp_path / "profiles"
        root.mkdir()
        _write_lite_profile(root)
        store = FilesystemProfileStore(root)

        # Stub the embedding surface: no model, no GPU, no network. ``snapshot``
        # is what ranking reads, because the roster and the matrix have to be
        # handed over together or a row could name the wrong profile.
        unit = np.array([1.0, 0.0], dtype=np.float32)
        _stub_matrix(monkeypatch, lambda: np.array([unit], dtype=np.float32))
        monkeypatch.setattr(CentroidManager, "embed_query", lambda self, text: unit)

        _stub_search(store, monkeypatch, _ABSTRACT_HITS)
        return store

    # ----------------------------------------------------------------------
    # Bug 1: match.rank built top_papers from paper_summary only
    # ----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "hits, expected",
        [
            (_ABSTRACT_HITS, ["paper2020a", "paper2021b"]),
            (_DUPE_CHUNK_HITS, ["paper2020a"]),
            (_MIXED_HITS, ["paper2019z"]),
        ],
        ids=["lite-abstracts", "dedupes-chunks", "paper-summary-evidence"],
    )
    def test_rank_top_papers(
        self,
        lite_store: FilesystemProfileStore,
        monkeypatch: pytest.MonkeyPatch,
        hits: list[SearchHit],
        expected: list[str],
    ) -> None:
        """Match evidence collects every paper chunk type, once per paper.

        Abstract-derived hits must populate match evidence rather than being
        dropped; multiple chunks of one paper collapse to a single paper id;
        and the full/deep path is unchanged, so ``paper_summary`` hits still
        count while non-paper chunk types (soul/expertise) stay excluded.
        """
        _stub_search(lite_store, monkeypatch, hits)
        matches = lite_store.match.rank(
            "chromatin accessibility ATAC-seq", k=3, normalize=False, diversify=False
        )
        assert len(matches) == 1
        assert matches[0].evidence.top_papers == expected

    # ----------------------------------------------------------------------
    # Bug 2: indexes.coverage counted n_papers from paper_summary only
    # ----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "slug, rows, n_chunks, n_papers",
        [
            (
                "lite-researcher",
                [
                    ("paper_abstract", "paper2020a"),
                    ("paper_abstract", "paper2021b"),
                    ("paper_abstract", "paper2021b"),  # second chunk, same paper
                ],
                3,
                2,
            ),
            (
                "mixed-researcher",
                [
                    ("paper_summary", "paperA"),
                    ("paper_abstract", "paperA"),  # same paper, both forms
                    ("paper_abstract", "paperB"),
                    ("soul", "SOUL"),  # not a paper
                ],
                4,
                2,
            ),
        ],
        ids=["lite-abstracts-only", "mixed-chunk-types"],
    )
    def test_coverage_paper_counts(
        self,
        tmp_path: Path,
        slug: str,
        rows: list[tuple[str, str]],
        n_chunks: int,
        n_papers: int,
    ) -> None:
        """A lite profile reports its real paper count, not 0.

        Summaries and abstracts both count as papers, a source_id shared by
        both forms counts once, and non-paper chunk types never count.
        """
        root = tmp_path / "profiles"
        root.mkdir()
        prof_dir = _write_lite_profile(root, slug=slug)
        _write_index(prof_dir, rows)

        store = FilesystemProfileStore(root)
        (report,) = store.indexes.coverage()
        assert report["has_index"] is True
        assert report["n_chunks"] == n_chunks
        assert report["n_papers"] == n_papers
        # The typed form a caller can use without re-parsing the dicts.
        stats = store.indexes.stats()
        assert stats[slug].n_chunks == n_chunks
        assert stats[slug].n_papers == n_papers
        assert stats[slug].backend_name == "fake:tiny"

    # ----------------------------------------------------------------------
    # Bug 3: calibration failure was swallowed silently
    # ----------------------------------------------------------------------

    def test_calibration_failure_is_logged_not_swallowed(
        self,
        lite_store: FilesystemProfileStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed calibration warns loudly that the score is uncalibrated."""

        def _boom(prof):
            raise RuntimeError("calibration exploded on an abstract-only index")

        monkeypatch.setattr("researcher_profiles.analytics.match.ensure_calibration", _boom)

        with caplog.at_level(logging.WARNING, logger="researcher_profiles.analytics.match"):
            matches = lite_store.match.rank("chromatin", k=3, normalize=True, diversify=False)

        assert len(matches) == 1
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "calibration failure was swallowed with no signal"
        assert any("UNCALIBRATED" in r.getMessage() for r in warnings)
        assert any("lite-researcher" in r.getMessage() for r in warnings)

    def test_calibration_failure_still_returns_a_match(
        self, lite_store: FilesystemProfileStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback stays fail-soft: a broken calibration must not raise."""

        def _boom(prof):
            raise RuntimeError("nope")

        monkeypatch.setattr("researcher_profiles.analytics.match.ensure_calibration", _boom)

        raw = lite_store.match.rank("chromatin", k=3, normalize=False, diversify=False)
        fallback = lite_store.match.rank("chromatin", k=3, normalize=True, diversify=False)
        # normalize=True degraded to exactly the raw score.
        assert fallback[0].score == pytest.approx(raw[0].score)

    def test_successful_calibration_emits_no_warning(
        self,
        lite_store: FilesystemProfileStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The calibration warning fires only on failure, not on every normalized rank."""
        monkeypatch.setattr(
            "researcher_profiles.analytics.match.ensure_calibration", lambda prof: object()
        )
        monkeypatch.setattr(
            "researcher_profiles.analytics.match.normalize_score", lambda raw, cal: 0.5
        )

        with caplog.at_level(logging.WARNING, logger="researcher_profiles.analytics.match"):
            matches = lite_store.match.rank("chromatin", k=3, normalize=True, diversify=False)

        assert matches[0].score == pytest.approx(0.5)
        assert not [r for r in caplog.records if "UNCALIBRATED" in r.getMessage()]


# --------------------------------------------------------------------------
# Construction purity and the centroid cache
# --------------------------------------------------------------------------


def _write_indexed_profile(root: Path, slug: str, rid: str) -> Path:
    """A profile with a minimal hand-built ``embeddings.sqlite``."""
    prof_dir = root / slug
    prof_dir.mkdir(parents=True)
    write_profile(prof_dir, name=slug.replace("-", " ").title(), rid=rid)
    _write_index(prof_dir, [("paper_summary", f"{slug}-paper")])
    return prof_dir


_RIDS = [
    "0000-0001-2345-6789",
    "0000-0002-1825-0097",
    "0000-0004-1415-9266",
]


class _StubVectorIndex:
    """A ``VectorIndex`` whose ``search`` returns fixed hits. No model, no GPU."""

    backend_spec = "fake:tiny"

    def __init__(self, hits):
        self._hits = hits

    def search(self, query: str, k: int = 5):  # noqa: ARG002 - fixed hits
        return self._hits

    def centroid(self):  # pragma: no cover - the matrix is stubbed separately
        raise AssertionError("the centroid matrix should be stubbed, not computed")


def _stub_search(store, monkeypatch: pytest.MonkeyPatch, hits) -> None:
    """Make every profile's chunk search return ``hits``, through the store."""
    monkeypatch.setattr(store, "vector_index", lambda ref: _StubVectorIndex(hits))


def _stub_matrix(monkeypatch: pytest.MonkeyPatch, make_matrix) -> None:
    """Hand ranking a fixed matrix, paired with the live roster.

    ``snapshot`` and not ``matrix``: ranking reads the pair so a row can never
    name the wrong profile, and a stub that patched only ``matrix`` would leave
    the real (unstubbed) one behind whatever ``snapshot`` returned.
    """
    monkeypatch.setattr(
        CentroidManager,
        "snapshot",
        lambda self: (self._rostered(), make_matrix()),
    )


def _stub_topics(monkeypatch: pytest.MonkeyPatch) -> None:
    """No topic evidence. These fixtures' hand-built indexes hold no vectors."""
    from researcher_profiles.analytics.match import MatchManager

    monkeypatch.setattr(
        MatchManager, "_overlapping_topics", lambda self, prof, qvec, label_vecs, **kw: []
    )


def _stub_centroids(store, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every profile's centroid a cheap unit vector; record who was asked.

    Stubs the *store*, which is where the analytics read vectors from. Stubbing
    ``prof.index`` would no longer be observed, which is the point of the
    capability: the analytics do not reach into a profile for its vectors.
    """
    asked: list[str] = []

    def _centroid(ref: str):
        # Re-derived per call, not captured at stub time: a profile created
        # after the stub is installed still has to get a row.
        asked.append(ref)
        v = np.zeros(4, dtype=np.float32)
        v[store.list_slugs().index(ref) % 4] = 1.0
        return v

    monkeypatch.setattr(store, "centroid", _centroid)
    return asked


class TestConstructionIsPure:
    """Building a store, or a roster over it, reads; it does not write.

    Writing ``.cache/index.json`` as a side effect of loading would let
    unrelated consumers depend on a file nobody owns refreshing.
    """

    def test_loading_writes_no_lookup_index(self, tmp_path: Path):
        root = tmp_path / "profiles"
        root.mkdir()
        _write_indexed_profile(root, "one", _RIDS[0])
        store = FilesystemProfileStore(root)
        assert store.list_slugs() == ["one"]
        assert len(store._rostered()) == 1
        assert not (root / ".cache" / "index.json").exists()
        path = store.write_lookup_index()
        assert path.is_file()
        assert json.loads(path.read_text())["by_rid"] == {_RIDS[0]: "one"}

    def test_backend_spec_comes_from_the_indexes(self, tmp_path: Path):
        root = tmp_path / "profiles"
        root.mkdir()
        _write_indexed_profile(root, "one", _RIDS[0])
        assert FilesystemProfileStore(root).backend_spec == "fake:tiny"

    def test_backend_spec_is_none_without_an_index(self, registry_root):
        assert FilesystemProfileStore(registry_root).backend_spec is None


class TestCentroidCache:
    """``store.centroids`` memoizes in process and caches on disk.

    Recomputing means reading every profile's embedding index, so the cache is
    what makes ranking a large root affordable. A cache that goes stale
    without noticing is worse than no cache at all.
    """

    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        root = tmp_path / "profiles"
        root.mkdir()
        _write_indexed_profile(root, "one", _RIDS[0])
        _write_indexed_profile(root, "two", _RIDS[1])
        return root

    def test_cold_computes_and_writes_the_npz(self, root: Path, monkeypatch):
        store = FilesystemProfileStore(root)
        asked = _stub_centroids(store, monkeypatch)
        assert not store.centroids.cache_path.exists()
        mat = store.centroids.matrix
        assert mat.shape == (2, 4)
        assert asked == ["one", "two"]
        assert store.centroids.cache_path.is_file()

    def test_warm_in_process_rereads_nothing(self, root: Path, monkeypatch):
        store = FilesystemProfileStore(root)
        _stub_centroids(store, monkeypatch)
        first = store.centroids.matrix

        def _boom(*a, **kw):
            raise AssertionError("the in-process memo was bypassed")

        monkeypatch.setattr(np, "load", _boom)
        assert store.centroids.matrix is first

    def test_warm_cross_process_reads_the_npz(self, root: Path, monkeypatch):
        store = FilesystemProfileStore(root)
        _stub_centroids(store, monkeypatch)
        expected = store.centroids.matrix

        fresh = FilesystemProfileStore(root)
        monkeypatch.setattr(
            fresh,
            "centroid",
            lambda ref: (_ for _ in ()).throw(
                AssertionError("recomputed instead of reading the cache")
            ),
        )
        assert np.allclose(fresh.centroids.matrix, expected)

    def test_an_index_newer_than_the_cache_forces_a_recompute(self, root: Path, monkeypatch):
        store = FilesystemProfileStore(root)
        _stub_centroids(store, monkeypatch)
        _ = store.centroids.matrix
        newer = store.centroids.cache_path.stat().st_mtime + 60
        db = cache_dir(root / "one") / "embeddings.sqlite"
        import os

        os.utime(db, (newer, newer))

        fresh = FilesystemProfileStore(root)
        asked = _stub_centroids(fresh, monkeypatch)
        _ = fresh.centroids.matrix
        assert asked == ["one", "two"]

    def test_a_new_profile_forces_a_recompute(self, root: Path, monkeypatch):
        store = FilesystemProfileStore(root)
        _stub_centroids(store, monkeypatch)
        _ = store.centroids.matrix

        _write_indexed_profile(root, "three", _RIDS[2])
        fresh = FilesystemProfileStore(root)
        asked = _stub_centroids(fresh, monkeypatch)
        assert fresh.centroids.matrix.shape == (3, 4)
        assert asked == ["one", "three", "two"]

    def test_a_profile_with_no_index_makes_the_cache_stale(self, root: Path, monkeypatch):
        store = FilesystemProfileStore(root)
        _stub_centroids(store, monkeypatch)
        _ = store.centroids.matrix
        (cache_dir(root / "one") / "embeddings.sqlite").unlink()

        fresh = FilesystemProfileStore(root)
        asked = _stub_centroids(fresh, monkeypatch)
        _ = fresh.centroids.matrix
        assert asked == ["one", "two"]

    def test_an_unbuilt_index_gets_a_zero_row_not_a_crash(self, root: Path, monkeypatch):
        from researcher_profiles.embeddings import IndexNotBuiltError

        store = FilesystemProfileStore(root)
        _stub_centroids(store, monkeypatch)
        original = store.centroid

        def _one_boom(ref: str):
            if ref == "one":
                raise IndexNotBuiltError("no vectors")
            return original(ref)

        monkeypatch.setattr(store, "centroid", _one_boom)
        roster, mat = store.centroids.snapshot()
        assert mat.shape == (2, 4)
        assert not mat[roster.slugs.index("one")].any()
        assert mat[roster.slugs.index("two")].any()

    def test_invalidate_drops_the_memo_and_the_file(self, root: Path, monkeypatch):
        store = FilesystemProfileStore(root)
        asked = _stub_centroids(store, monkeypatch)
        _ = store.centroids.matrix
        assert len(asked) == 2

        store.centroids.invalidate()
        assert not store.centroids.cache_path.exists()
        _ = store.centroids.matrix
        assert len(asked) == 4


class TestWritesInvalidateTheSnapshot:
    """The whole point of dissolving the registry: no manual invalidation.

    The analytics live on a live, mutable store, so a write has to reach them
    with nobody calling anything. The store's write generation is what does it,
    and it moves whether the write went through a store method or through a
    profile the store handed out.
    """

    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        root = tmp_path / "profiles"
        root.mkdir()
        _write_indexed_profile(root, "one", _RIDS[0])
        _write_indexed_profile(root, "two", _RIDS[1])
        return root

    def test_a_create_through_the_store_widens_the_matrix(self, root: Path, monkeypatch):
        from researcher_profiles.schema import ProfileDocument

        store = FilesystemProfileStore(root)
        _stub_centroids(store, monkeypatch)
        assert store.centroids.matrix.shape == (2, 4)
        assert store.rid_for("one") == _RIDS[0]

        store.create(
            ProfileDocument(name="Three", rid=_RIDS[2], provenance="self_published"),
            slug="three",
        )

        # No invalidate() call anywhere: the generation moved, so the roster
        # and the matrix rebuilt themselves.
        assert store.centroids.matrix.shape == (3, 4)
        assert store._rostered().slugs == ["one", "three", "two"]
        assert store.rid_for("three") == _RIDS[2]

    def test_a_delete_through_the_store_narrows_the_matrix(self, root: Path, monkeypatch):
        store = FilesystemProfileStore(root)
        _stub_centroids(store, monkeypatch)
        assert store.centroids.matrix.shape == (2, 4)

        store.delete("one")

        assert store.centroids.matrix.shape == (1, 4)
        assert store._rostered().slugs == ["two"]
        with pytest.raises(ProfileNotFoundError):
            store.rid_for("one")

    def test_a_write_through_a_handed_out_profile_moves_the_generation(self, root: Path):
        """No store method is called here, so only the post-commit hook can see it."""
        store = FilesystemProfileStore(root)
        before = store.generation
        roster = store._rostered()

        store.get("one").save_soul("# rewritten with no store method anywhere\n")

        assert store.generation > before
        assert store._rostered() is not roster

    def test_ranking_reflects_a_write_with_no_invalidation(self, root: Path, monkeypatch):
        store = FilesystemProfileStore(root)
        _stub_centroids(store, monkeypatch)
        _stub_search(store, monkeypatch, _ABSTRACT_HITS)
        _stub_topics(monkeypatch)
        unit = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        monkeypatch.setattr(CentroidManager, "embed_query", lambda self, text: unit)

        before = {m.profile.slug for m in store.match.rank("x", k=5, diversify=False)}
        assert before == {"one", "two"}

        store.delete("two")

        after = {m.profile.slug for m in store.match.rank("x", k=5, diversify=False)}
        assert after == {"one"}


class TestSqlStoreInvalidation:
    """The same contract on a store that mutates rows rather than directories."""

    def test_a_create_and_a_delete_are_both_seen(self, monkeypatch):
        from researcher_profiles.schema import ProfileDocument
        from researcher_profiles.store.sql import SqlProfileStore

        store = SqlProfileStore("sqlite://")
        store.create_all()
        assert store._rostered().slugs == []
        gen = store.generation

        store.create(
            ProfileDocument(name="One", rid=_RIDS[0], provenance="self_published"), slug="one"
        )
        assert store.generation > gen
        assert store._rostered().slugs == ["one"]
        assert store.rid_for("one") == _RIDS[0]

        store.delete("one")
        assert store._rostered().slugs == []

    def test_a_write_through_a_handed_out_profile_moves_the_generation(self):
        from researcher_profiles.schema import ProfileDocument
        from researcher_profiles.store.sql import SqlProfileStore

        store = SqlProfileStore("sqlite://")
        store.create_all()
        store.create(
            ProfileDocument(name="One", rid=_RIDS[0], provenance="self_published"), slug="one"
        )
        before = store.generation
        store.get("one").save_soul("# edited\n")
        assert store.generation > before


# --------------------------------------------------------------------------
# Score calibration
# --------------------------------------------------------------------------


class _FakeProfile:
    """The minimum a calibration read needs: somewhere to keep its cache."""

    def __init__(self, path):
        self.directory = path

    def require_directory(self, what):
        return self.directory


class TestCalibration:
    """Tests for the calibration module's pure (non-embedding) functions."""

    @pytest.mark.parametrize(
        "raw, stats, expected",
        [
            (float("nan"), {"mean": 0.0, "std": 1.0}, 0.0),
            (None, {"mean": 0.0, "std": 1.0}, 0.0),
            (1.0, {"mean": float("nan"), "std": 1.0}, 0.5),
        ],
        ids=[
            "nan_raw_scores_zero",
            "none_raw_scores_zero",
            "nan_stats_returns_half",
        ],
    )
    def test_normalize_score_degenerate_inputs(self, raw, stats, expected):
        assert cal.normalize_score(raw, stats) == expected  # type: ignore[arg-type]

    def test_normalize_score_is_bounded_0_1(self):
        c = {"mean": 0.0, "std": 0.1}
        for raw in (-5.0, -0.1, 0.0, 0.1, 5.0):
            s = cal.normalize_score(raw, c)
            assert 0.0 <= s <= 1.0

    def test_normalize_score_monotonic(self):
        c = {"mean": 0.0, "std": 0.1}
        lo = cal.normalize_score(-0.2, c)
        mid = cal.normalize_score(0.0, c)
        hi = cal.normalize_score(0.2, c)
        assert lo < mid < hi
        # score at the mean is 0.5
        assert abs(mid - 0.5) < 1e-9

    def test_load_calibration_missing_returns_none(self, tmp_path):
        p = _FakeProfile(tmp_path)
        assert cal.load_calibration(p) is None

    def test_load_calibration_roundtrip(self, tmp_path):
        p = _FakeProfile(tmp_path)
        meta = cache_dir(tmp_path)
        meta.mkdir()
        payload = {
            "mean": 0.1,
            "std": 0.05,
            "scorer_version": cal.SCORER_VERSION,
        }
        (meta / "calibration.json").write_text(json.dumps(payload), encoding="utf-8")
        loaded = cal.load_calibration(p)
        assert loaded is not None
        assert loaded["mean"] == 0.1

    def test_load_calibration_wrong_version_returns_none(self, tmp_path):
        p = _FakeProfile(tmp_path)
        meta = cache_dir(tmp_path)
        meta.mkdir()
        payload = {"mean": 0.1, "std": 0.05, "scorer_version": cal.SCORER_VERSION + 1}
        (meta / "calibration.json").write_text(json.dumps(payload), encoding="utf-8")
        assert cal.load_calibration(p) is None
