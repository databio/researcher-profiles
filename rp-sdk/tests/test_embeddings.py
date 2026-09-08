"""Everything under ``embeddings/``: chunking, the sqlite index, the vectors a
profile derives from it, and the public flat export a consumer actually sees.

Text becomes chunks, chunks become a searchable index, and the profile-level
vectors (centroid, topics, relevance) are built on top of it. ``embeddings/``
is the only part of any of it a consumer ever sees: no ``meta/``, no sqlite,
no chunk text.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.build_state import BuildState
from researcher_profiles.embeddings import (
    FlatEmbeddingIndex,
    IndexBackendMismatchError,
    SqliteEmbeddingIndex,
    build_index,
    chunk_expertise,
    chunk_summary,
    index_backend_name,
    rebuild_sqlite_from_flat,
    write_flat_export,
)
from researcher_profiles.embeddings._sqlite import connect_vec, read_index_meta_path
from researcher_profiles.embeddings.flat import slugify_backend
from researcher_profiles.models.results import Topic
from researcher_profiles.profile import topics as topics_mod
from researcher_profiles.utils.paths import cache_dir

from .factories import FakeBackend, build_profile_dir, write_profile

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def synthetic_profile(tmp_path: Path) -> Path:
    """The standard built profile: document, papers, persona, two summaries.

    Manifest refreshed, no embedding index.
    """
    return build_profile_dir(tmp_path / "tester-one", name="Tester One")


@pytest.fixture
def indexed_profile(synthetic_profile: Path) -> Path:
    """``synthetic_profile`` with a real sqlite index built by ``FakeBackend``."""
    from researcher_profiles.embeddings import SqliteEmbeddingIndex

    SqliteEmbeddingIndex(synthetic_profile).build_index(backend=FakeBackend())
    return synthetic_profile


@pytest.fixture
def flat_indexed_profile(indexed_profile: Path) -> Path:
    """``indexed_profile`` plus its public flat export under ``embeddings/``."""
    from researcher_profiles.embeddings import write_flat_export

    write_flat_export(indexed_profile)
    return indexed_profile


# --------------------------------------------------------------------------
# Chunking and the sqlite index
# --------------------------------------------------------------------------


# ---- Chunking ----


def test_chunk_expertise_splits_on_h2():
    text = "## A\n\nbody one [cite2020a]\n\n## B\n\nbody two [cite2020b]\n"
    chunks = chunk_expertise(text)
    assert len(chunks) == 2
    assert chunks[0].section == "A"
    assert chunks[1].section == "B"
    # citations stripped from embed_text but kept in text
    assert "[cite2020a]" in chunks[0].text
    assert "[cite2020a]" not in chunks[0].embed_text


def test_chunk_summary_handles_abstract_only():
    chunks = chunk_summary("[abstract-only]\nA tiny review.\n", "paperX")
    assert len(chunks) == 1
    assert chunks[0].meta.get("abstract_only") is True
    assert "[abstract-only]" not in chunks[0].embed_text


# ---- Schema bootstrap ----


def test_build_index_creates_db(synthetic_profile):
    report = build_index(synthetic_profile, backend=FakeBackend())
    db = cache_dir(synthetic_profile) / "embeddings.sqlite"
    assert db.exists()
    assert report.added > 0
    assert report.backend_name == "fake:tiny"

    # Check tables exist
    import sqlite3

    import sqlite_vec

    conn = sqlite3.connect(str(db))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','virtual')")
    }
    assert "chunks" in names
    assert "index_meta" in names
    # vec0 table is a virtual table
    assert any("chunk_vec" in n for n in names)
    conn.close()


# ---- Idempotency, change and deletion detection ----


def _leave_unchanged(profile_dir):
    """Touch nothing: the second build must be a pure no-op."""


def _edit_summary(profile_dir):
    sumfile = profile_dir / "sources" / "summaries" / "paperA.summary.md"
    sumfile.write_text("Brand new content describing region universes.\n", encoding="utf-8")


def _delete_summary(profile_dir):
    (profile_dir / "sources" / "summaries" / "paperA.summary.md").unlink()


@pytest.mark.parametrize(
    "mutate, expected_delta",
    [
        (_leave_unchanged, {"added": 0, "updated": 0, "removed": 0}),
        (_edit_summary, {"added": 0, "updated": 1, "removed": 0}),
        (_delete_summary, {"added": 0, "updated": 0, "removed": 1}),
    ],
    ids=["idempotent", "detects-changes", "detects-deletions"],
)
def test_build_index_change_detection(synthetic_profile, mutate, expected_delta):
    """A rebuild reports only the source that moved; everything else is skipped."""
    build_index(synthetic_profile, backend=FakeBackend())
    mutate(synthetic_profile)
    report = build_index(synthetic_profile, backend=FakeBackend())
    assert {k: getattr(report, k) for k in expected_delta} == expected_delta
    assert report.skipped > 0


# ---- Search filter ----


def test_search_filter_source_type(synthetic_profile):
    idx = SqliteEmbeddingIndex(synthetic_profile)
    idx.build_index(backend=FakeBackend())
    hits = idx.search("anything", k=10, filter={"source_type": "paper_summary"})
    assert hits
    assert all(h.source_type == "paper_summary" for h in hits)


# ---- Few-chunk search returns all rows (no silent drops on NULL distance) ----


def test_search_returns_all_chunks_when_k_exceeds_count(tmp_path):
    """With fewer chunks than k, search should return all of them.

    Regression: rows where vec0 returns a NULL distance must not be silently
    dropped (which would yield fewer than k results with no indication why).
    NULL-distance rows are recomputed in Python.
    """
    p = tmp_path / "tiny-profile"
    (p / "personality").mkdir(parents=True)
    (p / "sources" / "summaries").mkdir(parents=True)
    write_profile(p, name="Tiny", rid="0000-0002-1825-0097")
    # Two chunks total: one expertise section, one summary.
    (p / "personality" / "expertise.md").write_text(
        "# Expertise\n\n## Topic\n\nA short body about ATAC-seq.\n",
        encoding="utf-8",
    )
    (p / "sources" / "summaries" / "paperA.summary.md").write_text(
        "A summary of paper A on region sets.\n",
        encoding="utf-8",
    )

    idx = SqliteEmbeddingIndex(p)
    report = idx.build_index(backend=FakeBackend())
    n_chunks = report.added
    assert 2 <= n_chunks <= 3  # tiny but >1

    # Ask for more than we have; we should still get all rows back.
    hits = idx.search("anything at all", k=10)
    assert len(hits) == n_chunks


# ---- Backend mismatch ----


def test_backend_mismatch_raises(synthetic_profile):
    build_index(synthetic_profile, backend=FakeBackend(name="fake:a", dim=16))
    with pytest.raises(IndexBackendMismatchError):
        build_index(synthetic_profile, backend=FakeBackend(name="fake:b", dim=8))


def test_backend_mismatch_force_rebuild(synthetic_profile):
    build_index(synthetic_profile, backend=FakeBackend(name="fake:a", dim=16))
    report = build_index(synthetic_profile, backend=FakeBackend(name="fake:b", dim=8), force=True)
    assert report.added > 0
    assert report.backend_name == "fake:b"

    # Confirm dim is now 8 by reading meta
    meta = read_index_meta_path(cache_dir(synthetic_profile) / "embeddings.sqlite")
    assert meta["backend_name"] == "fake:b"
    assert meta["embedding_dim"] == "8"


def test_disable_env(synthetic_profile, monkeypatch):
    monkeypatch.setenv("RESEARCHER_PROFILES_DISABLE_INDEX", "1")
    report = build_index(synthetic_profile, backend=FakeBackend())
    assert report.backend_name == "disabled"
    assert not (cache_dir(synthetic_profile) / "embeddings.sqlite").exists()


# --------------------------------------------------------------------------
# get_backend("fastembed:...") / FastEmbedBackend
#
# Construction and spec-parsing must not require fastembed to be installed.
# CI does not have it, since FastEmbedBackend._load() imports it lazily.
# --------------------------------------------------------------------------


class TestFastEmbedBackend:
    def test_string_spec_returns_backend(self):
        from researcher_profiles.embeddings.backends import FastEmbedBackend, get_backend

        backend = get_backend("fastembed:all-MiniLM-L6-v2")
        assert isinstance(backend, FastEmbedBackend)
        assert backend.name == "fastembed:all-MiniLM-L6-v2"
        assert backend.dim == 384

    def test_dict_spec_returns_backend_and_allows_dim_override(self):
        from researcher_profiles.embeddings.backends import FastEmbedBackend, get_backend

        backend = get_backend({"backend": "fastembed", "model": "all-MiniLM-L6-v2"})
        assert isinstance(backend, FastEmbedBackend)
        assert backend.name == "fastembed:all-MiniLM-L6-v2"
        assert backend.dim == 384

        overridden = get_backend({"backend": "fastembed", "model": "all-MiniLM-L6-v2", "dim": 999})
        assert overridden.dim == 999

    def test_bare_kind_with_no_colon_raises(self):
        from researcher_profiles.embeddings.backends import get_backend

        with pytest.raises(ValueError, match="kind:model"):
            get_backend("fastembed")

    def test_dict_spec_defaults_to_minilm(self):
        from researcher_profiles.embeddings.backends import get_backend

        backend = get_backend({"backend": "fastembed"})
        assert backend.model_name == "all-MiniLM-L6-v2"

    def test_model_map_translates_short_names_and_passes_through_unknown(self):
        from researcher_profiles.embeddings.backends import FastEmbedBackend

        assert (
            FastEmbedBackend._MODEL_MAP["all-MiniLM-L6-v2"]
            == "sentence-transformers/all-MiniLM-L6-v2"
        )
        backend = FastEmbedBackend("some/custom-model")
        assert backend._MODEL_MAP.get(backend.model_name, backend.model_name) == "some/custom-model"

    def test_missing_fastembed_raises_with_extra_name(self, monkeypatch):
        import builtins

        from researcher_profiles.embeddings.backends import (
            FastEmbedBackend,
            MissingEmbeddingBackendError,
        )

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "fastembed":
                raise ImportError("no module named fastembed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        backend = FastEmbedBackend()
        with pytest.raises(MissingEmbeddingBackendError, match=r"'fastembed' extra"):
            backend._load()

    @pytest.mark.integration
    def test_real_embed_roundtrip(self):
        pytest.importorskip("fastembed")
        from researcher_profiles.embeddings.backends import FastEmbedBackend

        backend = FastEmbedBackend()
        vectors = backend.embed(["chromatin accessibility", "region set enrichment"])
        assert len(vectors) == 2
        assert all(len(v) == 384 for v in vectors)
        assert backend.dim == 384


# --------------------------------------------------------------------------
# ResearcherProfile.profile_embedding()
# --------------------------------------------------------------------------


class TestProfileCentroid:
    """Tests for ResearcherProfile.profile_embedding()."""

    @pytest.mark.parametrize("kind", ["centroid", "expertise"], ids=["centroid", "expertise"])
    def test_centroid_basic(self, monkeypatch, synthetic_profile, kind):
        build_index(synthetic_profile, backend=FakeBackend())
        p = ResearcherProfile.from_files(synthetic_profile)

        # Force backend resolution to FakeBackend regardless of config
        import researcher_profiles.embeddings.profile_vec as pv

        monkeypatch.setattr(pv, "_resolve_backend", lambda profile: FakeBackend())
        vec = p.index.embedding(kind)
        assert isinstance(vec, np.ndarray)
        assert vec.shape == (16,)
        # unit-normalized
        assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-4

    def test_centroid_cached_on_disk(self, synthetic_profile):
        build_index(synthetic_profile, backend=FakeBackend())
        p = ResearcherProfile.from_files(synthetic_profile)
        _ = p.index.embedding("centroid")
        cache = cache_dir(synthetic_profile) / "profile_vec.npz"
        assert cache.exists()
        # Second call returns same vector without recomputing
        p2 = ResearcherProfile.from_files(synthetic_profile)
        v2 = p2.index.embedding("centroid")
        assert v2.shape == (16,)

    def test_backend_mismatch_invalidates(self, synthetic_profile):
        build_index(synthetic_profile, backend=FakeBackend(name="fake:a", dim=16))
        p = ResearcherProfile.from_files(synthetic_profile)
        v1 = p.index.embedding("centroid")
        assert v1.shape == (16,)
        # Now rebuild with different backend
        build_index(synthetic_profile, backend=FakeBackend(name="fake:b", dim=8), force=True)
        p2 = ResearcherProfile.from_files(synthetic_profile)
        v2 = p2.index.embedding("centroid")
        assert v2.shape == (8,)


# --------------------------------------------------------------------------
# ResearcherProfile.topics() and .relevance()
# --------------------------------------------------------------------------


class TestTopicsAndRelevance:
    """Tests for ResearcherProfile.topics() and .relevance()."""

    def test_topics_cluster(self, monkeypatch, synthetic_profile):
        pytest.importorskip("sklearn")  # clustering needs the [topics] extra
        build_index(synthetic_profile, backend=FakeBackend())
        p = ResearcherProfile.from_files(synthetic_profile)

        topics = p.topics.get(n=3, method="cluster")
        assert isinstance(topics, list)
        # We have 4 chunks (2 expertise + 1 soul + 2 summaries minus stripping)
        # KMeans yields up to n clusters
        assert 1 <= len(topics) <= 3
        for t in topics:
            assert t.label
            assert 0.0 <= t.weight <= 1.0

        # Cluster topics are a precomputation: they land in the cache, and
        # never in the content tree.
        assert (cache_dir(synthetic_profile) / "topics.json").is_file()
        assert not (synthetic_profile / "personality" / "topics.json").exists()

    def test_topics_cached_roundtrip(self, synthetic_profile):
        pytest.importorskip("sklearn")  # clustering needs the [topics] extra
        build_index(synthetic_profile, backend=FakeBackend())
        p = ResearcherProfile.from_files(synthetic_profile)
        first = p.topics.get(n=3, method="cluster")
        second = p.topics.get(n=3, method="cached")
        assert [t.label for t in first] == [t.label for t in second]

    def test_llm_topics_are_content_not_cache(self, monkeypatch, synthetic_profile):
        """LLM-labeled topics are generated content, so they go in ``personality/``.

        Rerunning gives different topics; deleting the file loses information.
        That makes it the same kind of artifact as ``personality/expertise.md``,
        and it must not sit in a directory advertised as safe to delete.
        """
        build_index(synthetic_profile, backend=FakeBackend())
        p = ResearcherProfile.from_files(synthetic_profile)

        monkeypatch.setattr(
            topics_mod,
            "_topics_llm",
            lambda profile, n: ("llm", [Topic(label="Quantum widgets", weight=0.9)]),
        )
        got = p.topics.get(n=3, method="llm")
        assert [t.label for t in got] == ["Quantum widgets"]

        content = synthetic_profile / "personality" / "topics.json"
        assert content.is_file()
        assert json.loads(content.read_text())["method"] == "llm"
        assert not (cache_dir(synthetic_profile) / "topics.json").exists()

        # ``cached`` prefers the content copy over any cache copy.
        assert [t.label for t in p.topics.get(n=3, method="cached")] == ["Quantum widgets"]

    def test_llm_fallback_to_cluster_persists_as_a_cache(self, monkeypatch, synthetic_profile):
        """A failed LLM call downgrades to clustering, and the result is a cache.

        The downgrade is the whole reason ``_topics_llm`` reports the method it
        actually used: persisting cluster output as ``personality/topics.json``
        would file a precomputation as generated content.
        """
        pytest.importorskip("sklearn")
        build_index(synthetic_profile, backend=FakeBackend())
        p = ResearcherProfile.from_files(synthetic_profile)

        monkeypatch.setattr(
            topics_mod,
            "_topics_llm",
            lambda profile, n: ("cluster", topics_mod._topics_cluster(profile, n)),
        )
        p.topics.get(n=3, method="llm")

        assert (cache_dir(synthetic_profile) / "topics.json").is_file()
        assert not (synthetic_profile / "personality" / "topics.json").exists()


# --------------------------------------------------------------------------
# The public flat form: what the exporter writes, what a reader reconstructs
# --------------------------------------------------------------------------


class TestFlatExport:
    """The flat embedding exporter: sqlite in, public flat form out.

    The load-bearing property: rows drop out of the public export by the GENERAL
    derivation rule (a chunk's tier is its source's tier), not a hand-rolled
    ``PUBLISHED_SOURCE_TYPES`` allowlist. cv/web/grant chunks are ``restricted``
    and never reach the blob or the chunks file.
    """

    @pytest.fixture
    def deep_profile(self, tmp_path: Path) -> Path:
        """A deep profile whose sqlite mixes public and restricted (cv/web/grant) chunks."""
        return build_profile_dir(
            tmp_path / "tester-deep",
            name="Tester Deep",
            level="deep",
            grants=True,
            cv=True,
            web=True,
        )

    # ----------------------------------------------------------------------
    # Basic shape / spec conformance
    # ----------------------------------------------------------------------

    def test_export_writes_three_files_and_matches_spec(self, synthetic_profile: Path):
        idx = SqliteEmbeddingIndex(synthetic_profile)
        report = idx.build_index(backend=FakeBackend())
        assert report.added > 0

        result = write_flat_export(synthetic_profile)
        assert result is not None

        emb = synthetic_profile / "embeddings"
        index_path = emb / "index.json"
        assert index_path.is_file()
        index = json.loads(index_path.read_text())

        # Spec section 3 fields.
        assert index["backend_spec"] == "fake:tiny"
        assert index["dtype"] == "float32"
        assert index["byte_order"] == "little"
        assert index["layout"] == "row_major"
        assert index["metric"] == "cosine"
        assert index["normalized"] is False
        assert index["row_key"] == "chunk_index"
        assert index["dim"] == 16
        assert index["rows"] == list(range(index["count"]))

        blob_path = emb / index["file"]
        assert blob_path.is_file()
        raw = blob_path.read_bytes()
        assert len(raw) == index["count"] * index["dim"] * 4

        import hashlib

        assert hashlib.sha256(raw).hexdigest() == index["sha256"]

        chunks_path = emb / (Path(index["file"]).stem + ".chunks.json")
        chunks = json.loads(chunks_path.read_text())
        assert len(chunks) == index["count"]
        # No chunk text is ever exposed.
        for c in chunks:
            assert "text" not in c
            assert "embed_text" not in c
            assert set(c) == {"source_type", "source_id", "chunk_index", "section", "char_count"}

    def test_backend_filename_is_colon_free(self, tmp_path: Path):
        p = build_profile_dir(tmp_path / "tester-slug", name="Tester Slug")
        idx = SqliteEmbeddingIndex(p)
        # A realistic model name with a colon.
        idx.build_index(backend=FakeBackend(name="st:all-MiniLM-L6-v2"))
        result = write_flat_export(p)
        assert result is not None

        index = json.loads((p / "embeddings" / "index.json").read_text())
        assert index["backend_spec"] == "st:all-MiniLM-L6-v2"
        assert ":" not in index["file"]
        assert index["file"] == slugify_backend("st:all-MiniLM-L6-v2") + ".bin"
        # The recorded filename is the actual file on disk.
        assert (p / "embeddings" / index["file"]).is_file()

    # ----------------------------------------------------------------------
    # Privacy filtering: the derivation rule, not an allowlist
    # ----------------------------------------------------------------------

    def test_restricted_source_chunks_are_dropped(self, deep_profile: Path):
        idx = SqliteEmbeddingIndex(deep_profile)
        idx.build_index(backend=FakeBackend())

        # The sqlite contains restricted chunk types.
        conn = connect_vec(idx.db_path)
        sqlite_types = {r[0] for r in conn.execute("SELECT DISTINCT source_type FROM chunks")}
        conn.close()
        assert {"cv", "web", "grant"} <= sqlite_types

        result = write_flat_export(deep_profile)
        assert result is not None
        assert result.dropped > 0

        chunks = json.loads(
            (
                deep_profile / "embeddings" / (Path(result.blob_path.name).stem + ".chunks.json")
            ).read_text()
        )
        exported_types = {c["source_type"] for c in chunks}
        # Only public source types survive.
        assert exported_types <= {"expertise", "soul", "paper_summary", "paper_abstract"}
        assert not ({"cv", "web", "grant"} & exported_types)
        # Count matches surviving rows.
        index = json.loads((deep_profile / "embeddings" / "index.json").read_text())
        assert index["count"] == len(chunks)

    def test_internal_profile_exports_nothing(self, tmp_path: Path):
        p = build_profile_dir(
            tmp_path / "tester-internal", name="Tester Internal", visibility="internal"
        )
        SqliteEmbeddingIndex(p).build_index(backend=FakeBackend())

        result = write_flat_export(p)
        assert result is None
        # No flat form advertised.
        assert not (p / "embeddings" / "index.json").is_file()

    def test_contaminated_summary_rows_are_dropped(self, synthetic_profile: Path):
        SqliteEmbeddingIndex(synthetic_profile).build_index(backend=FakeBackend())

        # Mark paperA contaminated in build state.
        bs = BuildState.load(synthetic_profile)
        bs.paper("paperA").contaminated = True
        bs.save(synthetic_profile)

        result = write_flat_export(synthetic_profile)
        assert result is not None
        chunks = json.loads(
            (
                synthetic_profile
                / "embeddings"
                / (Path(result.blob_path.name).stem + ".chunks.json")
            ).read_text()
        )
        summary_ids = {c["source_id"] for c in chunks if c["source_type"] == "paper_summary"}
        assert "paperA" not in summary_ids

    def test_export_is_content_addressed_and_idempotent(self, synthetic_profile: Path):
        SqliteEmbeddingIndex(synthetic_profile).build_index(backend=FakeBackend())
        r1 = write_flat_export(synthetic_profile)
        assert r1 is not None
        r2 = write_flat_export(synthetic_profile)
        assert r2 is not None
        # An unchanged sqlite yields an identical blob digest.
        assert r1.sha256 == r2.sha256

    def test_no_sqlite_means_no_flat_form(self, synthetic_profile: Path):
        # No build_index call: no meta/embeddings.sqlite.
        assert write_flat_export(synthetic_profile) is None
        assert not (synthetic_profile / "embeddings" / "index.json").is_file()


class TestFlatReader:
    """Readers that reconstruct a usable index from the public flat form.

    ``FlatEmbeddingIndex`` is what an external consumer has: ``embeddings/`` but no
    ``meta/``, pure numpy, no sqlite. ``rebuild_sqlite_from_flat`` is the offline
    optimization that avoids re-embedding.
    """

    # ----------------------------------------------------------------------
    # Round-trip
    # ----------------------------------------------------------------------

    def test_load_roundtrips_vectors(self, flat_indexed_profile: Path):
        emb = flat_indexed_profile / "embeddings"
        index = json.loads((emb / "index.json").read_text())

        flat = FlatEmbeddingIndex.load(emb)
        assert flat.backend_spec == "fake:tiny"
        assert flat.dim == 16
        assert flat.count == index["count"]
        assert flat.normalized is False
        assert flat.vectors.shape == (index["count"], 16)

        # Vectors equal the on-disk blob within float32 tolerance.
        raw = (emb / index["file"]).read_bytes()
        expected = np.frombuffer(raw, dtype="<f4").reshape(index["count"], 16)
        assert np.allclose(flat.vectors, expected, atol=1e-6)

    def test_search_returns_ordered_hits_without_text(self, flat_indexed_profile: Path):
        flat = FlatEmbeddingIndex.load(flat_indexed_profile / "embeddings")
        q = flat.vectors[0]
        hits = flat.search_vector(q, k=3)
        assert hits
        # Descending score order.
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)
        # Top hit is the vector we queried with.
        assert hits[0].score >= scores[-1]
        # FlatHit carries no text.
        for h in hits:
            assert not hasattr(h, "text")

    def test_search_text_refuses_mismatched_backend(self, flat_indexed_profile: Path):
        flat = FlatEmbeddingIndex.load(flat_indexed_profile / "embeddings")
        with pytest.raises(ValueError, match="backend"):
            flat.search_text("anything", FakeBackend(name="other:model"))
        # A matching backend works.
        hits = flat.search_text("ATAC-seq", FakeBackend(name="fake:tiny"), k=2)
        assert hits

    # ----------------------------------------------------------------------
    # Corruption detection
    # ----------------------------------------------------------------------

    def test_truncated_blob_raises(self, flat_indexed_profile: Path):
        emb = flat_indexed_profile / "embeddings"
        index = json.loads((emb / "index.json").read_text())
        blob_path = emb / index["file"]
        raw = blob_path.read_bytes()
        blob_path.write_bytes(raw[:-4])  # drop one float
        with pytest.raises(ValueError, match="byte length"):
            FlatEmbeddingIndex.load(emb)

    def test_tampered_byte_fails_sha256(self, flat_indexed_profile: Path):
        emb = flat_indexed_profile / "embeddings"
        index = json.loads((emb / "index.json").read_text())
        blob_path = emb / index["file"]
        raw = bytearray(blob_path.read_bytes())
        raw[0] ^= 0xFF  # flip a byte, same length
        blob_path.write_bytes(bytes(raw))
        with pytest.raises(ValueError, match="sha256"):
            FlatEmbeddingIndex.load(emb)

    # ----------------------------------------------------------------------
    # Local sqlite rebuild
    # ----------------------------------------------------------------------

    def test_rebuild_sqlite_from_flat_matches_original_top_hit(self, flat_indexed_profile: Path):
        # Original top hit for a fixed query.
        original = SqliteEmbeddingIndex(flat_indexed_profile)
        original._cached_backend = FakeBackend()
        orig_hits = original.search("region universes", k=1)
        assert orig_hits
        orig_top = (orig_hits[0].source_type, orig_hits[0].source_id, orig_hits[0].chunk_index)

        # Blow away the sqlite; rebuild from the flat form (no re-embedding).
        (cache_dir(flat_indexed_profile) / "embeddings.sqlite").unlink()
        report = rebuild_sqlite_from_flat(flat_indexed_profile)
        assert report.added > 0
        assert (cache_dir(flat_indexed_profile) / "embeddings.sqlite").is_file()

        rebuilt = SqliteEmbeddingIndex(flat_indexed_profile)
        rebuilt._cached_backend = FakeBackend()
        new_hits = rebuilt.search("region universes", k=1)
        assert new_hits
        new_top = (new_hits[0].source_type, new_hits[0].source_id, new_hits[0].chunk_index)
        assert new_top == orig_top
        # Recovered text is present (needed to display a hit).
        assert new_hits[0].text


# --------------------------------------------------------------------------
# rank_works_against_profile: candidate works against one profile
# --------------------------------------------------------------------------


def _candidate(title, abstract=None, year=2026):
    from researcher_profiles.schema import PaperRecord

    return PaperRecord(title=title, year=year, abstract=abstract)


class TestRankWorksAgainstProfile:
    """The inverse ranking direction: works scored against a profile vector."""

    @pytest.fixture
    def profile(self, indexed_profile: Path) -> ResearcherProfile:
        return ResearcherProfile.from_files(indexed_profile)

    @pytest.fixture
    def candidates(self):
        return [
            _candidate("Chromatin accessibility in synthetic regions", "ATAC-seq maps."),
            _candidate("Deep learning for protein folding", "A structure predictor."),
            _candidate("Region set enrichment methods", "Testing genomic interval overlaps."),
            _candidate("A survey of medieval bread prices"),
        ]

    def test_orders_by_score_and_is_deterministic(self, profile, candidates):
        from researcher_profiles.embeddings.rank import rank_works_against_profile

        ranked = rank_works_against_profile(
            profile, candidates, k=10, backend=FakeBackend(), diversify=False
        )
        assert len(ranked) == len(candidates)
        scores = [r.score for r in ranked]
        assert scores == sorted(scores, reverse=True)
        again = rank_works_against_profile(
            profile, candidates, k=10, backend=FakeBackend(), diversify=False
        )
        assert [(r.work.title, r.score) for r in ranked] == [(r.work.title, r.score) for r in again]

    def test_k_and_threshold_cut_the_list(self, profile, candidates):
        from researcher_profiles.embeddings.rank import rank_works_against_profile

        full = rank_works_against_profile(
            profile, candidates, k=10, backend=FakeBackend(), diversify=False
        )
        top2 = rank_works_against_profile(
            profile, candidates, k=2, backend=FakeBackend(), diversify=False
        )
        assert [r.work.title for r in top2] == [r.work.title for r in full[:2]]
        # A threshold just above the weakest score drops exactly that work.
        cut = (full[-1].score + full[-2].score) / 2
        trimmed = rank_works_against_profile(
            profile, candidates, k=10, backend=FakeBackend(), diversify=False, threshold=cut
        )
        assert [r.work.title for r in trimmed] == [r.work.title for r in full[:-1]]

    def test_empty_candidates_return_empty(self, profile):
        from researcher_profiles.embeddings.rank import rank_works_against_profile

        assert rank_works_against_profile(profile, [], backend=FakeBackend()) == []


class TestMMRIndices:
    def test_pure_relevance_without_vectors(self):
        from researcher_profiles.embeddings.rank import mmr_indices

        assert mmr_indices([0.1, 0.9, 0.5], None, k=2) == [1, 2]

    def test_diversification_penalizes_near_duplicates(self):
        from researcher_profiles.embeddings.rank import mmr_indices

        # Candidates 0 and 1 are identical vectors; 2 is orthogonal with a
        # slightly lower score. MMR must pick the orthogonal one second.
        vecs = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        assert mmr_indices([0.9, 0.85, 0.8], vecs, k=2, lambda_=0.5) == [0, 2]


# --------------------------------------------------------------------------
# recompute_centroid: the relevance-feedback path
# --------------------------------------------------------------------------


class TestRecomputeCentroid:
    def test_recompute_matches_and_is_stable(self, indexed_profile: Path):
        from researcher_profiles.embeddings.profile_vec import recompute_centroid

        prof = ResearcherProfile.from_files(indexed_profile)
        baseline = prof.index.embedding("centroid")
        first = recompute_centroid(prof)
        second = recompute_centroid(prof)
        assert np.array_equal(first, second)
        assert np.array_equal(first, baseline)

    def test_recompute_discards_a_stale_cached_centroid(self, indexed_profile: Path):
        from researcher_profiles.embeddings.profile_vec import (
            _load_cache,
            _save_cache,
            recompute_centroid,
        )

        prof = ResearcherProfile.from_files(indexed_profile)
        truth = prof.index.embedding("centroid").copy()
        cache = _load_cache(prof)
        cache["centroid"] = np.zeros_like(truth)
        _save_cache(prof, cache)
        # The poisoned cache is what profile_embedding now serves...
        assert not np.array_equal(prof.index.embedding("centroid"), truth)
        # ...and recompute restores the true index-derived centroid.
        assert np.array_equal(recompute_centroid(prof), truth)


# --------------------------------------------------------------------------
# Probe presence and schema conformance
# --------------------------------------------------------------------------


class TestProbeAndSchema:
    """A freshly written index.json must carry a probe and validate against
    the committed embedding_index schema."""

    def test_flat_export_includes_probe(self, flat_indexed_profile: Path):
        index = json.loads((flat_indexed_profile / "embeddings" / "index.json").read_text())
        assert "probe" in index, "index.json must include a probe object"
        probe = index["probe"]
        assert isinstance(probe["text"], str) and len(probe["text"]) > 0
        assert isinstance(probe["vector"], list) and len(probe["vector"]) > 0
        assert all(isinstance(v, float) for v in probe["vector"])

    def test_flat_export_validates_against_schema(self, flat_indexed_profile: Path):
        import jsonschema

        # rp-sdk/schemas/ is the one tracked copy, generated by `rp schema
        # export` from this package's own models. It ships in the wheel, so its
        # absence is a packaging failure, not a reason to skip.
        schema_path = (
            Path(__file__).resolve().parents[1] / "schemas" / "embedding_index.schema.json"
        )
        assert schema_path.is_file(), f"missing generated schema: {schema_path}"

        schema = json.loads(schema_path.read_text())
        index = json.loads((flat_indexed_profile / "embeddings" / "index.json").read_text())
        jsonschema.validate(index, schema)


# --------------------------------------------------------------------------
# Index facts: SqliteEmbeddingIndex.stats() and friends
# --------------------------------------------------------------------------


def _write_bare_index(
    profile_dir: Path,
    rows: list[tuple[str, str]],
    *,
    meta: dict[str, str] | None = None,
    with_meta_table: bool = True,
) -> Path:
    """A minimal ``embeddings.sqlite`` holding ``(source_type, source_id)`` chunks.

    Hand-built with plain sqlite3 so these tests need neither an embedding
    backend nor the sqlite-vec extension, the same contract the accessors
    themselves promise.
    """
    import sqlite3

    cache = cache_dir(profile_dir)
    cache.mkdir(parents=True, exist_ok=True)
    db = cache / "embeddings.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE chunks (source_type TEXT, source_id TEXT, text TEXT)")
        conn.executemany(
            "INSERT INTO chunks (source_type, source_id, text) VALUES (?, ?, 'x')",
            rows,
        )
        if with_meta_table:
            conn.execute("CREATE TABLE index_meta (key TEXT, value TEXT)")
            for k, v in (meta or {}).items():
                conn.execute("INSERT INTO index_meta (key, value) VALUES (?, ?)", (k, v))
        conn.commit()
    finally:
        conn.close()
    return db


class TestProfileIndexStats:
    """``SqliteEmbeddingIndex`` answers questions about its own file.

    These accessors replace raw ``sqlite3.connect`` calls in the registry.
    They must never require sqlite-vec, and a missing
    file or a missing table degrades to zeros rather than raising.
    """

    def test_stats_on_a_real_index(self, tmp_path: Path):
        prof = tmp_path / "someone"
        prof.mkdir()
        write_profile(prof, name="Someone")
        db = _write_bare_index(
            prof,
            [
                ("paper_summary", "paperA"),
                ("paper_abstract", "paperA"),
                ("paper_abstract", "paperB"),
                ("soul", "SOUL"),
            ],
            meta={"backend_name": "fake:tiny", "last_built_at": "2026-01-01T00:00:00Z"},
        )
        idx = SqliteEmbeddingIndex(prof)
        stats = idx.stats()
        assert stats.exists is True
        assert stats.n_chunks == 4
        assert stats.n_papers == 2  # paperA counted once; soul never counts
        assert stats.backend_name == "fake:tiny"
        assert stats.last_built_at == "2026-01-01T00:00:00Z"
        assert stats.mtime == pytest.approx(db.stat().st_mtime)
        assert idx.exists() is True
        assert idx.counts() == (4, 2)
        assert idx.index_meta()["backend_name"] == "fake:tiny"
        assert stats.to_dict()["n_papers"] == 2

    def test_stats_when_the_file_is_absent(self, tmp_path: Path):
        prof = tmp_path / "someone"
        prof.mkdir()
        write_profile(prof, name="Someone")
        idx = SqliteEmbeddingIndex(prof)
        assert idx.exists() is False
        assert idx.mtime() is None
        assert idx.index_meta() == {}
        assert idx.counts() == (0, 0)
        stats = idx.stats()
        assert stats.exists is False
        assert stats.n_chunks == 0
        assert stats.n_papers == 0
        assert stats.backend_name == ""
        assert stats.last_built_at is None

    def test_stats_when_index_meta_table_is_missing(self, tmp_path: Path):
        prof = tmp_path / "someone"
        prof.mkdir()
        write_profile(prof, name="Someone")
        _write_bare_index(prof, [("paper_summary", "paperA")], with_meta_table=False)
        idx = SqliteEmbeddingIndex(prof)
        assert idx.index_meta() == {}
        stats = idx.stats()
        assert stats.exists is True
        assert stats.n_chunks == 1
        assert stats.backend_name == ""
        assert stats.last_built_at is None

    def test_lite_index_reports_papers(self, tmp_path: Path):
        """Abstract-only chunks are papers: a lite profile is not paperless."""
        prof = tmp_path / "lite-one"
        prof.mkdir()
        write_profile(prof, name="Lite One", level="lite")
        _write_bare_index(
            prof,
            [
                ("paper_abstract", "paper2020a"),
                ("paper_abstract", "paper2021b"),
                ("paper_abstract", "paper2021b"),
            ],
        )
        assert SqliteEmbeddingIndex(prof).stats().n_papers == 2

    def test_index_backend_name_from_a_path(self, tmp_path: Path):
        prof = tmp_path / "someone"
        prof.mkdir()
        write_profile(prof, name="Someone")
        assert index_backend_name(prof) == ""
        _write_bare_index(prof, [("soul", "SOUL")], meta={"backend_name": "fake:tiny"})
        assert index_backend_name(prof) == "fake:tiny"
