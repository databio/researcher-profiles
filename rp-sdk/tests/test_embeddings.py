"""Everything under ``embeddings/``: chunking, the sqlite index, profile-level vectors, flat export.

``embeddings/`` is all a consumer ever sees: no ``meta/``, no sqlite, no chunk text.
"""

import hashlib
import json
import shutil
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

from .factories import (
    TEI_URL,
    FakeBackend,
    build_profile_dir,
    tei_answer,
    use_tei_env,
    write_profile,
)


@pytest.fixture
def synthetic_profile(tmp_path: Path) -> Path:
    """Document, papers, persona, two summaries; manifest refreshed, no embedding index."""
    return build_profile_dir(tmp_path / "tester-one", name="Tester One")


@pytest.fixture(scope="module")
def _built_tree(tmp_path_factory) -> Path:
    """One indexed + flat-exported profile; never handed to a test directly."""
    p = build_profile_dir(tmp_path_factory.mktemp("built") / "tester-one", name="Tester One")
    SqliteEmbeddingIndex(p).build_index(backend=FakeBackend())
    write_flat_export(p)
    return p


@pytest.fixture(scope="module")
def shared_indexed(_built_tree) -> Path:
    """Read-only: tests using this must not write to the profile tree."""
    return _built_tree


@pytest.fixture
def indexed_profile(_built_tree, tmp_path) -> Path:
    """A private, writable copy of the built tree (cheap copytree, no re-embed)."""
    dest = tmp_path / "tester-one"
    # Readers of the shared tree cache the centroid there; a copy starts without it.
    shutil.copytree(_built_tree, dest, ignore=shutil.ignore_patterns("profile_vec.npz"))
    return dest


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


def test_build_index_creates_db(synthetic_profile):
    report = build_index(synthetic_profile, backend=FakeBackend())
    db = cache_dir(synthetic_profile) / "embeddings.sqlite"
    assert db.exists()
    assert report.added > 0
    assert report.backend_name == "fake:tiny"

    conn = connect_vec(db)
    names = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','virtual')")
    }
    conn.close()
    assert {"chunks", "index_meta"} <= names
    assert any("chunk_vec" in n for n in names)  # vec0 is a virtual table


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


def test_search_filter_source_type(shared_indexed):
    idx = SqliteEmbeddingIndex(shared_indexed)
    idx._cached_backend = FakeBackend()
    hits = idx.search("anything", k=10, filter={"source_type": "paper_summary"})
    assert hits
    assert all(h.source_type == "paper_summary" for h in hits)


def test_search_returns_all_chunks_when_k_exceeds_count(tmp_path):
    """Regression: vec0 rows with a NULL distance must not be silently dropped."""
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


@pytest.mark.parametrize("force", [False, True], ids=["mismatch-raises", "force-rebuild"])
def test_backend_mismatch(synthetic_profile, force):
    build_index(synthetic_profile, backend=FakeBackend(name="fake:a", dim=16))
    # A cached centroid exists before the second build, so the rebuild must invalidate it.
    assert ResearcherProfile.from_files(synthetic_profile).index.embedding("centroid").shape == (
        16,
    )
    second = FakeBackend(name="fake:b", dim=8)
    if not force:
        with pytest.raises(IndexBackendMismatchError):
            build_index(synthetic_profile, backend=second)
        return
    report = build_index(synthetic_profile, backend=second, force=True)
    assert report.added > 0
    assert report.backend_name == "fake:b"
    meta = read_index_meta_path(cache_dir(synthetic_profile) / "embeddings.sqlite")
    assert (meta["backend_name"], meta["embedding_dim"]) == ("fake:b", "8")
    fresh = ResearcherProfile.from_files(synthetic_profile)
    assert fresh.index.embedding("centroid").shape == (8,)


def test_disable_env(synthetic_profile, monkeypatch):
    monkeypatch.setenv("RESEARCHER_PROFILES_DISABLE_INDEX", "1")
    report = build_index(synthetic_profile, backend=FakeBackend())
    assert report.backend_name == "disabled"
    assert not (cache_dir(synthetic_profile) / "embeddings.sqlite").exists()


class _RecordingModel:
    """A fastembed model double that records the kwargs ``embed`` receives."""

    def __init__(self):
        self.kwargs = []

    def embed(self, texts, **kw):
        self.kwargs.append(kw)
        return [[0.0] * 384 for _ in texts]


class TestFastEmbedBackend:
    """Spec parsing must not need fastembed installed: ``_load()`` imports it lazily."""

    @pytest.mark.parametrize(
        ("spec", "name", "dim"),
        [
            ("fastembed:all-MiniLM-L6-v2", "fastembed:all-MiniLM-L6-v2", 384),
            (
                {"backend": "fastembed", "model": "all-MiniLM-L6-v2"},
                "fastembed:all-MiniLM-L6-v2",
                384,
            ),
            (
                {"backend": "fastembed", "model": "all-MiniLM-L6-v2", "dim": 999},
                "fastembed:all-MiniLM-L6-v2",
                999,
            ),
            ({"backend": "fastembed"}, "fastembed:all-MiniLM-L6-v2", 384),
        ],
        ids=["string", "dict", "dict-dim-override", "dict-defaults-to-minilm"],
    )
    def test_spec_resolves(self, spec, name, dim):
        from researcher_profiles.embeddings.backends import get_backend

        backend = get_backend(spec)
        assert (backend.name, backend.dim) == (name, dim)
        assert backend.model_name == "all-MiniLM-L6-v2"

    @pytest.mark.parametrize(
        ("spec", "kwargs", "match"),
        [
            ("fastembed", {}, "kind:model"),
            (
                "openai:text-embedding-3-small",
                {"batch_size": 16},
                "only supported by the fastembed",
            ),
        ],
        ids=["bare-kind-no-colon", "batch-size-for-other-backend"],
    )
    def test_spec_refused(self, spec, kwargs, match):
        from researcher_profiles.embeddings.backends import get_backend

        with pytest.raises(ValueError, match=match):
            get_backend(spec, **kwargs)

    def test_process_wide_batch_size_must_be_positive(self, monkeypatch):
        from researcher_profiles.embeddings import backends

        monkeypatch.setattr(backends, "_FASTEMBED_BATCH_SIZE", None)
        with pytest.raises(ValueError, match="positive"):
            backends.set_fastembed_batch_size(0)

    @pytest.mark.parametrize(
        ("process_wide", "spec", "kwargs", "expected"),
        [
            (None, "fastembed:all-MiniLM-L6-v2", {}, {}),
            (None, "fastembed:all-MiniLM-L6-v2", {"batch_size": 16}, {"batch_size": 16}),
            (None, {"backend": "fastembed", "batch_size": 8}, {}, {"batch_size": 8}),
            (16, "fastembed:all-MiniLM-L6-v2", {}, {"batch_size": 16}),
            (16, "fastembed:all-MiniLM-L6-v2", {"batch_size": 4}, {"batch_size": 4}),
        ],
        ids=["unset", "explicit", "from-dict", "process-wide", "explicit-beats-process-wide"],
    )
    def test_batch_size_reaches_embed_only_when_set(
        self, monkeypatch, process_wide, spec, kwargs, expected
    ):
        from researcher_profiles.embeddings import backends

        monkeypatch.setattr(backends, "_FASTEMBED_BATCH_SIZE", None)
        if process_wide:
            backends.set_fastembed_batch_size(process_wide)
        backend = backends.get_backend(spec, **kwargs)
        backend._model = _RecordingModel()
        backend.embed(["a"])
        assert backend._model.kwargs == [expected]

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("all-MiniLM-L6-v2", "sentence-transformers/all-MiniLM-L6-v2"),
            ("some/custom-model", "some/custom-model"),
        ],
        ids=["short-name-mapped-to-hf-path", "unknown-name-passed-through"],
    )
    def test_fastembed_receives_the_full_model_path(self, monkeypatch, model, expected):
        import sys
        import types

        from researcher_profiles.embeddings.backends import FastEmbedBackend, get_backend

        seen = []

        class TextEmbedding:
            def __init__(self, model_name):
                seen.append(model_name)

            def embed(self, texts, **kw):
                return [[0.0] * 384 for _ in texts]

        monkeypatch.setitem(
            sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=TextEmbedding)
        )
        monkeypatch.setattr(FastEmbedBackend, "_MODEL_CACHE", {})  # isolate the session cache
        get_backend(f"fastembed:{model}").embed(["a"])
        assert seen == [expected]

    @pytest.mark.parametrize(
        ("arena", "expected"),
        [(None, {}), (False, {"enable_cpu_mem_arena": False})],
        ids=["unset-keeps-onnxruntime-default", "off"],
    )
    def test_mem_arena_reaches_the_model_only_when_set(self, monkeypatch, arena, expected):
        import sys
        import types

        from researcher_profiles.embeddings import backends

        seen = []

        class TextEmbedding:
            def __init__(self, model_name, **kw):
                seen.append(kw)

            def embed(self, texts, **kw):
                return [[0.0] * 384 for _ in texts]

        monkeypatch.setitem(
            sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=TextEmbedding)
        )
        monkeypatch.setattr(backends.FastEmbedBackend, "_MODEL_CACHE", {})
        monkeypatch.setattr(backends, "_FASTEMBED_MEM_ARENA", None)
        backends.set_fastembed_mem_arena(arena)
        backends.get_backend("fastembed:all-MiniLM-L6-v2").embed(["a"])
        assert seen == [expected]

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
            backend.embed(["a"])

    @pytest.mark.integration
    def test_real_embed_roundtrip(self):
        pytest.importorskip("fastembed")
        from researcher_profiles.embeddings.backends import FastEmbedBackend

        backend = FastEmbedBackend()
        vectors = backend.embed(["chromatin accessibility", "region set enrichment"])
        assert len(vectors) == 2
        assert all(len(v) == 384 for v in vectors)
        assert backend.dim == 384


_TEI_MODEL = "all-MiniLM-L6-v2"
_FASTEMBED_LABEL = "fastembed:all-MiniLM-L6-v2"
_SECRET = "ems_SECRET123"


def _tei(**kw):
    """A ``TEIBackend`` for all-MiniLM-L6-v2 at the emulator's URL."""
    from researcher_profiles.embeddings import TEIBackend

    return TEIBackend(_TEI_MODEL, **{"url": TEI_URL, **kw})


class TestTEIBackend:
    """``tei:``: a remote encoder over the TEI API. Every request goes to ``tei_server``."""

    @pytest.mark.parametrize(
        ("spec", "batch_size", "timeout"),
        [
            ("tei:all-MiniLM-L6-v2", 64, 60.0),
            ({"backend": "tei", "model": _TEI_MODEL, "batch_size": 16, "timeout": 30}, 16, 30.0),
        ],
        ids=["string", "dict"],
    )
    def test_spec_resolves(self, tei_server, monkeypatch, spec, batch_size, timeout):
        from researcher_profiles.embeddings import get_backend, stored_name_of

        use_tei_env(monkeypatch)
        backend = get_backend(spec)
        assert (backend.name, backend.stored_name, backend.dim) == (
            "tei:all-MiniLM-L6-v2",
            _FASTEMBED_LABEL,
            384,
        )
        assert (backend.url, backend.batch_size, backend.timeout) == (TEI_URL, batch_size, timeout)
        # The label written next to its vectors is fastembed's; others keep their own.
        assert stored_name_of(backend) == _FASTEMBED_LABEL
        assert stored_name_of(FakeBackend()) == "fake:tiny"

    @pytest.mark.parametrize(
        ("url", "env", "spec", "kwargs", "error", "match"),
        [
            (None, {}, "tei:all-MiniLM-L6-v2", {}, "config", "EMBEDDING_DENSE_URL"),
            ("embed.test/tei/x", {}, "tei:all-MiniLM-L6-v2", {}, "config", "http"),
            (
                TEI_URL,
                {"RESEARCHER_PROFILES_EMBEDDING_TIMEOUT": "abc"},
                "tei:all-MiniLM-L6-v2",
                {},
                "config",
                "EMBEDDING_TIMEOUT",
            ),
            (TEI_URL, {}, {"backend": "tei", "url": TEI_URL}, {}, ValueError, "url or api_key"),
            (TEI_URL, {}, {"backend": "tei", "api_key": "k"}, {}, ValueError, "url or api_key"),
            (TEI_URL, {}, "tei:all-MiniLM-L6-v2", {"batch_size": 100}, ValueError, "1 to 64"),
        ],
        ids=[
            "no-url",
            "url-without-scheme",
            "bad-env-timeout",
            "url-in-spec-dict",
            "key-in-spec-dict",
            "batch-over-64",
        ],
    )
    def test_spec_refused(self, tei_server, monkeypatch, url, env, spec, kwargs, error, match):
        """No request is sent, and a spec dict (it can come from a profile) never sets the URL or key."""
        from researcher_profiles.embeddings import EmbeddingBackendConfigError, get_backend

        if url:
            monkeypatch.setenv("RESEARCHER_PROFILES_EMBEDDING_DENSE_URL", url)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        expected = EmbeddingBackendConfigError if error == "config" else error
        with pytest.raises(expected, match=match):
            get_backend(spec, **kwargs)
        assert tei_server.requests == []

    @pytest.mark.parametrize(
        ("env_key", "arg_key", "sent"),
        [
            ("ems_env", None, "Bearer ems_env"),
            ("ems_env", "ems_arg", "Bearer ems_arg"),
            (None, None, None),
        ],
        ids=["env-key", "argument-wins", "no-key-no-header"],
    )
    def test_request_shape(self, tei_server, monkeypatch, env_key, arg_key, sent):
        """``POST <base>/embed`` with ``{"inputs", "truncate": true}``; never ``normalize``."""
        from researcher_profiles.embeddings import TEIBackend

        use_tei_env(monkeypatch, key=env_key, url="https://elsewhere.test/tei/all-MiniLM-L6-v2")
        backend = TEIBackend(_TEI_MODEL, url=TEI_URL, api_key=arg_key)
        assert backend.embed(["a", "b"]) == FakeBackend(dim=384).embed(["a", "b"])
        info, embed = tei_server.requests
        assert (info.method, info.url) == ("GET", f"{TEI_URL}/info")
        assert (embed.method, embed.url) == ("POST", f"{TEI_URL}/embed")
        assert embed.body == {"inputs": ["a", "b"], "truncate": True}
        assert embed.headers.get("authorization") == sent

    def test_batches_of_at_most_64_in_order(self, tei_server, monkeypatch):
        from researcher_profiles.embeddings import backends

        texts = [f"text {i}" for i in range(150)]
        monkeypatch.setattr(backends, "_FASTEMBED_BATCH_SIZE", None)
        backends.set_fastembed_batch_size(8)  # fastembed's knob never reaches tei
        out = _tei().embed(texts)
        assert out == FakeBackend(dim=384).embed(texts)
        sent = [r.inputs for r in tei_server.requests if r.route == "embed"]
        assert [len(b) for b in sent] == [64, 64, 22]
        assert [t for b in sent for t in b] == texts

    @pytest.mark.parametrize(
        ("env_timeout", "texts", "read_timeout"),
        [
            (None, ["q"], 5.0),
            (None, ["a", "b"], 60.0),
            ("12", ["a", "b"], 12.0),
            ("2", ["q"], 2.0),
        ],
        ids=["query-5s", "batch-60s", "batch-from-env", "query-capped-by-env"],
    )
    def test_timeouts(self, tei_server, monkeypatch, env_timeout, texts, read_timeout):
        if env_timeout:
            monkeypatch.setenv("RESEARCHER_PROFILES_EMBEDDING_TIMEOUT", env_timeout)
        _tei().embed(texts)
        (embed,) = [r for r in tei_server.requests if r.route == "embed"]
        assert embed.timeout["read"] == read_timeout

    def test_one_shared_client_per_process(self, tei_server, monkeypatch):
        """Instances share one client and one ``/info`` check; a forked child builds its own."""
        from researcher_profiles.embeddings import backends, get_backend

        use_tei_env(monkeypatch)
        get_backend("tei:all-MiniLM-L6-v2").embed(["a"])
        get_backend("tei:all-MiniLM-L6-v2").embed(["b"])
        assert tei_server.routes() == ["info", "embed", "embed"]
        parent = backends._tei_connection(TEI_URL, None)
        assert backends._tei_connection(TEI_URL, None) is parent
        monkeypatch.setattr(backends.os, "getpid", lambda: parent.pid + 1)
        child = backends._tei_connection(TEI_URL, None)
        assert child is not parent and child.http is not parent.http

    @pytest.mark.parametrize(
        ("answers", "sleeps"),
        [
            ([tei_answer(429, headers={"Retry-After": "1"})], [1.0]),
            ([tei_answer(503, headers={"Retry-After": "1"})], [1.0]),
            ([tei_answer(429)], [1.0]),
        ],
        ids=["busy-429", "unhealthy-503", "no-retry-after-waits-1s"],
    )
    def test_retries_after_the_wait_the_server_asks(self, tei_server, answers, sleeps):
        tei_server.embed_queue = list(answers)
        assert _tei().embed(["q"]) == FakeBackend(dim=384).embed(["q"])
        assert tei_server.sleeps == sleeps

    @pytest.mark.parametrize(
        ("retry_after", "texts", "match", "max_slept"),
        [
            ("3600", ["q"], "quota", 0.0),
            ("1", ["q"], "busy", 5.0),
            ("1", ["a", "b"], "busy", 60.0),
            ("6", ["q"], "busy", 0.0),
        ],
        ids=["daily-quota-fails-at-once", "query-gives-up", "batch-gives-up", "wait-over-budget"],
    )
    def test_gives_up_within_the_budget(self, tei_server, retry_after, texts, match, max_slept):
        from researcher_profiles.embeddings import EmbeddingBackendUnavailableError

        tei_server.embed_default = tei_answer(429, headers={"Retry-After": retry_after})
        with pytest.raises(EmbeddingBackendUnavailableError, match=match):
            _tei().embed(texts)
        assert sum(tei_server.sleeps) <= max_slept
        if max_slept == 0.0:
            assert tei_server.sleeps == []

    @pytest.mark.parametrize("status", [401, 403], ids=["bad-key", "key-may-not-use-model"])
    def test_a_rejected_key_is_remembered(self, tei_server, status):
        """After a 401/403 the process sends nothing more with that key."""
        from researcher_profiles.embeddings import EmbeddingBackendConfigError

        tei_server.embed_default = tei_answer(
            status, json_body={"error": "This key cannot use model", "error_type": "Validation"}
        )
        backend = _tei(api_key="ems_bad")
        with pytest.raises(
            EmbeddingBackendConfigError, match="RESEARCHER_PROFILES_EMBEDDING_API_KEY"
        ):
            backend.embed(["q"])
        sent = len(tei_server.requests)
        with pytest.raises(EmbeddingBackendConfigError):
            backend.embed(["q"])
        with pytest.raises(EmbeddingBackendConfigError):
            _tei(api_key="ems_bad").check()
        assert len(tei_server.requests) == sent

    @pytest.mark.parametrize(
        ("answer", "error"),
        [
            (
                tei_answer(404, json_body={"error": "no model", "error_type": "Validation"}),
                "config",
            ),
            (
                tei_answer(422, json_body={"error": "batch too big", "error_type": "Validation"}),
                "config",
            ),
            (
                tei_answer(424, json_body={"error": "Inference failed", "error_type": "Backend"}),
                "down",
            ),
            (tei_answer(500), "down"),
            ("ConnectError", "down"),
            ("ReadTimeout", "down"),
            (tei_answer(200, json_body={"vectors": []}), "down"),
            (tei_answer(200, json_body=[[0.1] * 384, [0.2] * 384]), "down"),
        ],
        ids=[
            "404-no-model",
            "422-refused",
            "424-backend",
            "500",
            "connect-error",
            "read-timeout",
            "not-a-list",
            "wrong-row-count",
        ],
    )
    def test_error_mapping(self, tei_server, answer, error):
        import httpx

        from researcher_profiles.embeddings import (
            EmbeddingBackendConfigError,
            EmbeddingBackendUnavailableError,
            MissingEmbeddingBackendError,
        )

        tei_server.embed_default = getattr(httpx, answer) if isinstance(answer, str) else answer
        expected = (
            EmbeddingBackendConfigError if error == "config" else EmbeddingBackendUnavailableError
        )
        with pytest.raises(expected) as e:
            _tei().embed(["q"])
        # Both families are still "the encoder is not usable" to existing handlers.
        assert isinstance(e.value, MissingEmbeddingBackendError)
        assert TEI_URL in str(e.value)

    def test_check_reports_the_server(self, tei_server, caplog):
        import logging

        from researcher_profiles.embeddings import check_backend

        backend = _tei()
        with caplog.at_level(logging.INFO, logger="researcher_profiles.embeddings.backends"):
            info = backend.check()
        assert (info.model_id, info.dtype, info.max_input_length, info.dim) == (
            "sentence-transformers/all-MiniLM-L6-v2",
            "float32",
            256,
            384,
        )
        assert "sentence-transformers/all-MiniLM-L6-v2" in caplog.text
        assert tei_server.routes() == ["info", "embed"]  # the model check, then a probe
        assert check_backend(FakeBackend()) is True

    @pytest.mark.parametrize(
        ("info", "encoder"),
        [
            ({"served_model_name": "all-mpnet-base-v2", "model_id": "x/all-mpnet-base-v2"}, None),
            ({"model_type": {"embedding": {"pooling": "splade"}}}, None),
            ({"model_type": {"classifier": {}}}, None),
            ({"model_dtype": "int8"}, None),
            ({"max_input_length": 512}, None),
            ({}, FakeBackend(dim=768)),
        ],
        ids=["other-model", "splade", "not-embedding", "int8", "truncates-at-512", "768-d-probe"],
    )
    def test_check_refuses_another_model_and_remembers(self, tei_server, info, encoder):
        from researcher_profiles.embeddings import EmbeddingModelMismatchError

        tei_server.info.update(info)
        if encoder is not None:
            tei_server.encoder = encoder
        backend = _tei()
        with pytest.raises(EmbeddingModelMismatchError):
            backend.check()
        sent = len(tei_server.requests)
        with pytest.raises(EmbeddingModelMismatchError):
            backend.embed(["q"])
        assert len(tei_server.requests) == sent

    def test_check_raises_on_a_rejected_key(self, tei_server):
        from researcher_profiles.embeddings import EmbeddingBackendConfigError

        tei_server.info_answer = tei_answer(401)
        with pytest.raises(EmbeddingBackendConfigError, match="API_KEY"):
            _tei().check()

    @pytest.mark.parametrize(
        "answer", [tei_answer(503), "ConnectError"], ids=["503", "connect-error"]
    )
    def test_check_is_none_while_the_server_is_down(self, tei_server, caplog, answer):
        import logging

        import httpx

        from researcher_profiles.embeddings import check_backend

        tei_server.info_answer = getattr(httpx, answer) if isinstance(answer, str) else answer
        with caplog.at_level(logging.WARNING, logger="researcher_profiles.embeddings.backends"):
            assert _tei().check() is None
            assert check_backend(_tei()) is False
        assert "unreachable at startup" in caplog.text

    def test_check_lowers_the_batch_to_the_keys_limit(self, tei_server):
        tei_server.info["max_client_batch_size"] = 32
        backend = _tei()
        backend.check()
        assert backend.batch_size == 32
        backend.embed([f"t{i}" for i in range(40)])
        assert [len(r.inputs) for r in tei_server.requests if r.route == "embed"][1:] == [32, 8]

    def test_a_process_that_never_checks_sends_info_once(self, tei_server):
        """The model is checked before any vector is used, once per process and URL."""
        backend = _tei()
        for text in ("a", "b", "c"):
            backend.embed([text])
        _tei().embed(["d", "e"])
        assert tei_server.routes() == ["info", "embed", "embed", "embed", "embed"]

    def test_the_key_never_leaks(self, tei_server, caplog):
        import logging

        import httpx

        from researcher_profiles.embeddings import backends

        backend = _tei(api_key=_SECRET)
        messages = [repr(backend)]
        with caplog.at_level(logging.DEBUG):
            backend.embed(["q"])
            for answer in (tei_answer(401), tei_answer(404), httpx.ConnectError):
                backends.reset_tei_connections()
                tei_server.embed_default = answer
                try:
                    _tei(api_key=_SECRET).embed(["q"])
                except Exception as e:  # noqa: BLE001 - every failure's message is checked
                    messages.append(str(e))
        assert len(messages) == 4
        assert tei_server.requests[-1].headers["authorization"] == f"Bearer {_SECRET}"
        assert not [m for m in messages + [caplog.text] if _SECRET in m]


class TestTeiOverFastembedLabels:
    """A ``tei:`` encoder reads and writes ``fastembed:`` indexes; nothing stored says ``tei:``."""

    @staticmethod
    def _flat(texts):
        vecs = np.asarray(FakeBackend(dim=384).embed(texts), dtype="<f4")
        blob = vecs.tobytes()
        index = {
            "backend_spec": _FASTEMBED_LABEL,
            "file": "fastembed-all-minilm-l6-v2.bin",
            "dtype": "float32",
            "byte_order": "little",
            "dim": 384,
            "count": len(texts),
            "layout": "row_major",
            "normalized": False,
            "metric": "cosine",
            "row_key": "chunk_index",
            "rows": list(range(len(texts))),
            "sha256": hashlib.sha256(blob).hexdigest(),
        }
        chunks = [
            {"source_type": "paper_summary", "source_id": f"p{i}", "chunk_index": 0}
            for i in range(len(texts))
        ]
        return FlatEmbeddingIndex.from_bytes(
            json.dumps(index).encode(), blob, json.dumps(chunks).encode()
        )

    def test_flat_index_searched_with_tei(self, tei_server, monkeypatch):
        texts = ["region sets", "chromatin maps", "protein folding"]
        hits = self._flat(texts).search_text("chromatin maps", _tei(), k=1)
        assert (hits[0].source_id, hits[0].cosine) == ("p1", 1.0)

        use_tei_env(monkeypatch)
        before = len(tei_server.requests)
        hits = self._flat(texts).search("protein folding", k=1)
        assert (hits[0].source_id, hits[0].cosine) == ("p2", 1.0)
        assert tei_server.requests[before:][-1].inputs == ["protein folding"]

    def test_sqlite_index_searched_with_tei(self, tei_server, monkeypatch, synthetic_profile):
        SqliteEmbeddingIndex(synthetic_profile).build_index(
            backend=FakeBackend(name=_FASTEMBED_LABEL, dim=384)
        )
        use_tei_env(monkeypatch)
        hits = SqliteEmbeddingIndex(synthetic_profile).search("region universes", k=3)
        assert hits and all(h.text for h in hits)
        assert [r.inputs for r in tei_server.requests if r.route == "embed"] == [
            ["region universes"]
        ]

    def test_tei_updates_a_fastembed_index_in_place(self, tei_server, synthetic_profile):
        """No ``--force``, nothing re-embedded, and the label stays fastembed's."""
        first = build_index(synthetic_profile, backend=FakeBackend(name=_FASTEMBED_LABEL, dim=384))
        report = build_index(synthetic_profile, backend=_tei())
        assert (report.added, report.updated, report.skipped) == (0, 0, first.added)
        assert report.backend_name == _FASTEMBED_LABEL
        assert tei_server.requests == []
        meta = read_index_meta_path(cache_dir(synthetic_profile) / "embeddings.sqlite")
        assert meta["backend_name"] == _FASTEMBED_LABEL

    def test_a_tei_built_index_and_export_say_fastembed(self, tei_server, synthetic_profile):
        report = build_index(synthetic_profile, backend=_tei())
        assert report.added > 0 and report.backend_name == _FASTEMBED_LABEL
        result = write_flat_export(synthetic_profile)
        meta = read_index_meta_path(cache_dir(synthetic_profile) / "embeddings.sqlite")
        index = json.loads(result.index_path.read_text())
        assert (meta["backend_name"], index["backend_spec"], result.blob_path.name) == (
            _FASTEMBED_LABEL,
            _FASTEMBED_LABEL,
            "fastembed-all-minilm-l6-v2.bin",
        )
        emb = synthetic_profile / "embeddings"
        assert not [f.name for f in emb.iterdir() if "tei" in f.name]
        assert "tei" not in (emb / "index.json").read_text()
        assert not [k for k, v in meta.items() if "tei:" in v]
        assert max(len(r.inputs) for r in tei_server.requests if r.route == "embed") <= 64


class TestProfileCentroid:
    @pytest.mark.parametrize("kind", ["centroid", "expertise"])
    def test_centroid_basic(self, monkeypatch, indexed_profile, kind):
        # A private copy: the first-ever read (no cache yet) is the branch under test.
        p = ResearcherProfile.from_files(indexed_profile)

        # Force backend resolution to FakeBackend regardless of config
        import researcher_profiles.embeddings.profile_vec as pv

        monkeypatch.setattr(pv, "_resolve_backend", lambda profile: FakeBackend())
        vec = p.index.embedding(kind)
        assert vec.shape == (16,)
        assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-4  # unit-normalized

    def test_centroid_cached_on_disk(self, indexed_profile):
        p = ResearcherProfile.from_files(indexed_profile)
        _ = p.index.embedding("centroid")
        assert (cache_dir(indexed_profile) / "profile_vec.npz").exists()
        # A second profile reads the cached vector
        v2 = ResearcherProfile.from_files(indexed_profile).index.embedding("centroid")
        assert v2.shape == (16,)


class TestTopicsAndRelevance:
    def test_topics_cluster(self, indexed_profile):
        pytest.importorskip("sklearn")  # clustering needs the [topics] extra
        p = ResearcherProfile.from_files(indexed_profile)

        topics = p.topics.get(n=3, method="cluster")
        assert 1 <= len(topics) <= 3  # KMeans yields up to n clusters
        for t in topics:
            assert t.label
            assert 0.0 <= t.weight <= 1.0

        # Cluster topics are a precomputation: they land in the cache, never in the content tree.
        assert (cache_dir(indexed_profile) / "topics.json").is_file()
        assert not (indexed_profile / "personality" / "topics.json").exists()

    def test_topics_cached_roundtrip(self, indexed_profile):
        pytest.importorskip("sklearn")  # clustering needs the [topics] extra
        p = ResearcherProfile.from_files(indexed_profile)
        first = p.topics.get(n=3, method="cluster")
        second = p.topics.get(n=3, method="cached")
        assert [t.label for t in first] == [t.label for t in second]

    def test_llm_topics_are_content_not_cache(self, monkeypatch, indexed_profile):
        """LLM topics are generated content (``personality/``): deleting them loses information."""
        p = ResearcherProfile.from_files(indexed_profile)

        monkeypatch.setattr(
            topics_mod,
            "_topics_llm",
            lambda profile, n: ("llm", [Topic(label="Quantum widgets", weight=0.9)]),
        )
        got = p.topics.get(n=3, method="llm")
        assert [t.label for t in got] == ["Quantum widgets"]

        content = indexed_profile / "personality" / "topics.json"
        assert content.is_file()
        assert json.loads(content.read_text())["method"] == "llm"
        assert not (cache_dir(indexed_profile) / "topics.json").exists()

        # ``cached`` prefers the content copy over any cache copy.
        assert [t.label for t in p.topics.get(n=3, method="cached")] == ["Quantum widgets"]

    def test_llm_fallback_to_cluster_persists_as_a_cache(self, monkeypatch, indexed_profile):
        """A failed LLM call downgrades to clustering; persisting that as content would be wrong."""
        pytest.importorskip("sklearn")
        p = ResearcherProfile.from_files(indexed_profile)

        monkeypatch.setattr(
            topics_mod,
            "_topics_llm",
            lambda profile, n: ("cluster", topics_mod._topics_cluster(profile, n)),
        )
        p.topics.get(n=3, method="llm")

        assert (cache_dir(indexed_profile) / "topics.json").is_file()
        assert not (indexed_profile / "personality" / "topics.json").exists()


class TestFlatExport:
    """Rows drop from the public export by the derivation rule (a chunk's tier is its source's), not an allowlist."""

    @pytest.fixture
    def deep_profile(self, tmp_path: Path) -> Path:
        """A deep profile whose sqlite mixes public and private (cv/web/grant) chunks."""
        return build_profile_dir(
            tmp_path / "tester-deep",
            name="Tester Deep",
            level="deep",
            grants=True,
            cv=True,
            web=True,
        )

    def test_export_writes_three_files_and_matches_spec(self, shared_indexed: Path):
        emb = shared_indexed / "embeddings"
        index = json.loads((emb / "index.json").read_text())

        # Spec section 3 fields.
        expected = {
            "backend_spec": "fake:tiny",
            "dtype": "float32",
            "byte_order": "little",
            "layout": "row_major",
            "metric": "cosine",
            "normalized": False,
            "row_key": "chunk_index",
            "dim": 16,
            "rows": list(range(index["count"])),
        }
        assert {k: index[k] for k in expected} == expected

        raw = (emb / index["file"]).read_bytes()
        assert len(raw) == index["count"] * index["dim"] * 4
        assert hashlib.sha256(raw).hexdigest() == index["sha256"]

        chunks = json.loads((emb / (Path(index["file"]).stem + ".chunks.json")).read_text())
        assert len(chunks) == index["count"]
        # No chunk text is ever exposed.
        for c in chunks:
            assert set(c) == {"source_type", "source_id", "chunk_index", "section", "char_count"}

    def test_backend_filename_is_colon_free(self, tmp_path: Path):
        p = build_profile_dir(tmp_path / "tester-slug", name="Tester Slug")
        idx = SqliteEmbeddingIndex(p)
        idx.build_index(backend=FakeBackend(name="st:all-MiniLM-L6-v2"))  # a colon, like real names
        write_flat_export(p)

        index = json.loads((p / "embeddings" / "index.json").read_text())
        assert index["backend_spec"] == "st:all-MiniLM-L6-v2"
        assert ":" not in index["file"]
        assert index["file"] == slugify_backend("st:all-MiniLM-L6-v2") + ".bin"
        # The recorded filename is the actual file on disk.
        assert (p / "embeddings" / index["file"]).is_file()

    def test_private_source_chunks_are_dropped(self, deep_profile: Path):
        idx = SqliteEmbeddingIndex(deep_profile)
        idx.build_index(backend=FakeBackend())

        # The sqlite contains private chunk types.
        conn = connect_vec(idx.db_path)
        sqlite_types = {r[0] for r in conn.execute("SELECT DISTINCT source_type FROM chunks")}
        conn.close()
        assert {"cv", "web", "grant"} <= sqlite_types

        result = write_flat_export(deep_profile)
        assert result.dropped > 0

        chunks = json.loads(
            (
                deep_profile / "embeddings" / (Path(result.blob_path.name).stem + ".chunks.json")
            ).read_text()
        )
        exported_types = {c["source_type"] for c in chunks}
        assert exported_types <= {"expertise", "soul", "paper_summary", "paper_abstract"}
        index = json.loads((deep_profile / "embeddings" / "index.json").read_text())
        assert index["count"] == len(chunks)

    def test_internal_profile_exports_nothing(self, tmp_path: Path):
        p = build_profile_dir(
            tmp_path / "tester-limited", name="Tester Limited", visibility="limited"
        )
        SqliteEmbeddingIndex(p).build_index(backend=FakeBackend())

        write_flat_export(p)
        # No flat form advertised.
        assert not (p / "embeddings" / "index.json").is_file()

    def test_contaminated_summary_rows_are_dropped(self, indexed_profile: Path):
        bs = BuildState.load(indexed_profile)
        bs.paper("paperA").contaminated = True
        bs.save(indexed_profile)

        result = write_flat_export(indexed_profile)
        chunks = json.loads(
            (
                indexed_profile / "embeddings" / (Path(result.blob_path.name).stem + ".chunks.json")
            ).read_text()
        )
        summary_ids = {c["source_id"] for c in chunks if c["source_type"] == "paper_summary"}
        assert "paperA" not in summary_ids

    def test_export_is_content_addressed_and_idempotent(self, indexed_profile: Path):
        r1 = write_flat_export(indexed_profile)
        r2 = write_flat_export(indexed_profile)
        assert r1.sha256 == r2.sha256  # an unchanged sqlite yields an identical blob digest

    def test_no_sqlite_means_no_flat_form(self, synthetic_profile: Path):
        write_flat_export(synthetic_profile)
        assert not (synthetic_profile / "embeddings" / "index.json").is_file()


class TestFlatReader:
    """What an external consumer has: ``embeddings/`` but no ``meta/``, no sqlite."""

    def test_load_roundtrips_vectors(self, shared_indexed: Path):
        emb = shared_indexed / "embeddings"
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

    def test_search_returns_ordered_hits_without_text(self, shared_indexed: Path):
        flat = FlatEmbeddingIndex.load(shared_indexed / "embeddings")
        hits = flat.search_vector(flat.vectors[0], k=3)
        assert hits
        scores = [h.score for h in hits]
        assert scores == sorted(scores, reverse=True)
        assert not any(hasattr(h, "text") for h in hits)  # the public form carries no text

    def test_search_text_refuses_mismatched_backend(self, shared_indexed: Path):
        flat = FlatEmbeddingIndex.load(shared_indexed / "embeddings")
        with pytest.raises(ValueError, match="backend"):
            flat.search_text("anything", FakeBackend(name="other:model"))
        assert flat.search_text("ATAC-seq", FakeBackend(name="fake:tiny"), k=2)

    def test_truncated_blob_raises(self, indexed_profile: Path):
        emb = indexed_profile / "embeddings"
        index = json.loads((emb / "index.json").read_text())
        blob_path = emb / index["file"]
        blob_path.write_bytes(blob_path.read_bytes()[:-4])  # drop one float
        with pytest.raises(ValueError, match="byte length"):
            FlatEmbeddingIndex.load(emb)

    def test_tampered_byte_fails_sha256(self, indexed_profile: Path):
        emb = indexed_profile / "embeddings"
        index = json.loads((emb / "index.json").read_text())
        blob_path = emb / index["file"]
        raw = bytearray(blob_path.read_bytes())
        raw[0] ^= 0xFF  # flip a byte, same length
        blob_path.write_bytes(bytes(raw))
        with pytest.raises(ValueError, match="sha256"):
            FlatEmbeddingIndex.load(emb)

    def test_rebuild_sqlite_from_flat_matches_original_top_hit(self, indexed_profile: Path):
        original = SqliteEmbeddingIndex(indexed_profile)
        original._cached_backend = FakeBackend()
        orig_hits = original.search("region universes", k=1)
        assert orig_hits
        orig_top = (orig_hits[0].source_type, orig_hits[0].source_id, orig_hits[0].chunk_index)

        # Blow away the sqlite; rebuild from the flat form (no re-embedding).
        (cache_dir(indexed_profile) / "embeddings.sqlite").unlink()
        report = rebuild_sqlite_from_flat(indexed_profile)
        assert report.added > 0
        assert (cache_dir(indexed_profile) / "embeddings.sqlite").is_file()

        rebuilt = SqliteEmbeddingIndex(indexed_profile)
        rebuilt._cached_backend = FakeBackend()
        new_hits = rebuilt.search("region universes", k=1)
        assert new_hits
        assert (new_hits[0].source_type, new_hits[0].source_id, new_hits[0].chunk_index) == orig_top
        assert new_hits[0].text  # recovered text is needed to display a hit


def _candidate(title, abstract=None, year=2026):
    from researcher_profiles.schema import PaperRecord

    return PaperRecord(title=title, year=year, abstract=abstract)


class TestRankWorksAgainstProfile:
    """The inverse direction: works scored against a profile vector."""

    @pytest.fixture
    def profile(self, shared_indexed: Path) -> ResearcherProfile:
        return ResearcherProfile.from_files(shared_indexed)

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


class TestRecomputeCentroid:
    def test_recompute_matches_and_is_stable(self, shared_indexed: Path):
        from researcher_profiles.embeddings.profile_vec import recompute_centroid

        prof = ResearcherProfile.from_files(shared_indexed)
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


class TestProbeAndSchema:
    """A fresh index.json carries a probe and validates against the committed schema."""

    def test_flat_export_includes_probe(self, shared_indexed: Path):
        index = json.loads((shared_indexed / "embeddings" / "index.json").read_text())
        probe = index["probe"]
        assert probe["text"]
        assert len(probe["vector"]) == index["dim"]
        assert probe["vector"] == pytest.approx(FakeBackend().embed([probe["text"]])[0], abs=1e-6)

    def test_flat_export_validates_against_schema(self, shared_indexed: Path):
        import jsonschema

        # rp-sdk/schemas/ ships in the wheel: its absence is a packaging failure, not a skip.
        schema_path = (
            Path(__file__).resolve().parents[1] / "schemas" / "embedding_index.schema.json"
        )
        assert schema_path.is_file(), f"missing generated schema: {schema_path}"

        schema = json.loads(schema_path.read_text())
        index = json.loads((shared_indexed / "embeddings" / "index.json").read_text())
        jsonschema.validate(index, schema)


def _write_bare_index(
    profile_dir: Path,
    rows: list[tuple[str, str]],
    *,
    meta: dict[str, str] | None = None,
    with_meta_table: bool = True,
) -> Path:
    """A minimal ``embeddings.sqlite`` of ``(source_type, source_id)`` chunks, plain sqlite3 (no vec)."""
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
    """``SqliteEmbeddingIndex`` answers questions about its own file, without sqlite-vec.

    A missing file or a missing table degrades to zeros rather than raising.
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

    @pytest.mark.parametrize(
        ("rows", "with_meta_table", "level", "expected"),
        [
            (
                None,
                True,
                "full",
                {
                    "exists": False,
                    "n_chunks": 0,
                    "n_papers": 0,
                    "backend_name": "",
                    "last_built_at": None,
                },
            ),
            (
                [("paper_summary", "paperA")],
                False,
                "full",
                {"exists": True, "n_chunks": 1, "backend_name": "", "last_built_at": None},
            ),
            # Abstract-only chunks are papers: a lite profile is not paperless.
            (
                [("paper_abstract", "paper2020a"), ("paper_abstract", "paper2021b")] * 2,
                True,
                "lite",
                {"n_papers": 2},
            ),
        ],
        ids=["file-absent", "no-meta-table", "lite-reports-papers"],
    )
    def test_stats_degrade_gracefully(self, tmp_path: Path, rows, with_meta_table, level, expected):
        prof = tmp_path / "someone"
        prof.mkdir()
        write_profile(prof, name="Someone", level=level)
        if rows is not None:
            _write_bare_index(prof, rows, with_meta_table=with_meta_table)
        idx = SqliteEmbeddingIndex(prof)
        stats = idx.stats()
        assert {k: getattr(stats, k) for k in expected} == expected
        if rows is None:
            assert idx.exists() is False
            assert idx.mtime() is None
            assert idx.counts() == (0, 0)
        if not with_meta_table or rows is None:
            assert idx.index_meta() == {}

    def test_index_backend_name_from_a_path(self, tmp_path: Path):
        prof = tmp_path / "someone"
        prof.mkdir()
        write_profile(prof, name="Someone")
        assert index_backend_name(prof) == ""
        _write_bare_index(prof, [("soul", "SOUL")], meta={"backend_name": "fake:tiny"})
        assert index_backend_name(prof) == "fake:tiny"


class TestChunkEnumeration:
    """Indexing and serve-time text recovery share one enumerator; a stored vector row names its text by these keys."""

    DEEP_KEYS = [
        ("cv", "cv", 0),
        ("cv", "cv", 1),
        ("expertise", "expertise", 0),
        ("expertise", "expertise", 1),
        ("expertise", "expertise", 2),
        ("grant", "grantX", 0),
        ("paper_summary", "paperA", 0),
        ("paper_summary", "paperB", 0),
        ("soul", "soul", 0),
        ("soul", "soul", 1),
        ("web", "1-lab", 0),
        ("web", "1-lab", 1),
    ]

    def test_keys_are_stable_and_shared_with_the_sql_store(self, tmp_path):
        from researcher_profiles.embeddings.cache import SqliteEmbeddingIndex
        from researcher_profiles.embeddings.chunking import enumerate_source_chunks
        from researcher_profiles.store.sql import SqlProfileStore

        d = build_profile_dir(tmp_path / "p", level="deep", grants=True, cv=True, web=True)
        indexed = sorted(
            (c.source_type, c.source_id, c.chunk_index)
            for c in SqliteEmbeddingIndex(d)._enumerate_chunks()
        )
        assert indexed == self.DEEP_KEYS

        store = SqlProfileStore("sqlite://")
        store.create_all()
        store.put(ResearcherProfile.from_files(d))
        served = enumerate_source_chunks(store.get("p"))
        assert sorted((c.source_type, c.source_id, c.chunk_index) for c in served) == indexed

    def test_lite_profiles_enumerate_abstracts(self, tmp_path):
        from researcher_profiles.embeddings.cache import SqliteEmbeddingIndex

        d = build_profile_dir(
            tmp_path / "p",
            level="lite",
            personality=False,
            summaries=False,
            papers=[
                {"paper_id": "a2020x", "title": "T", "abstract": "Para one.\n\nPara two."},
                {"paper_id": "b2021y", "title": "U"},
            ],
        )
        keys = [
            (c.source_type, c.source_id, c.chunk_index)
            for c in SqliteEmbeddingIndex(d)._enumerate_chunks()
        ]
        assert keys == [("paper_abstract", "a2020x", 0)]


def test_corpus_only_leaves_out_summaries_of_papers_not_in_the_corpus(tmp_path):
    """The builder's corpus filter is opt-in: off, every summary is indexed."""
    from researcher_profiles.embeddings.cache import SqliteEmbeddingIndex

    d = build_profile_dir(
        tmp_path / "p",
        papers=[{"paper_id": "paperA", "title": "A", "year": 2020}],
    )

    def summary_ids(idx):
        return {c.source_id for c in idx._enumerate_chunks() if c.source_type == "paper_summary"}

    assert summary_ids(SqliteEmbeddingIndex(d)) == {"paperA", "paperB"}
    assert summary_ids(SqliteEmbeddingIndex(d, corpus_only=True)) == {"paperA"}


def test_citation_tags_of_every_paper_id_shape_are_stripped():
    """Ids ending at the year (live2005) or with a numbered suffix (extracellular2023_2) were left in."""
    from researcher_profiles.embeddings.chunking import _strip_citations

    text = (
        "A [live2005, gi2007] B [extracellular2023_2] C [2016activation] "
        "D [corces2016lineage] E [20261062] F [2005] G [interview]"
    )
    assert _strip_citations(text) == "A  B  C  D  E  F [2005] G [interview]"


def _write_seeds(path: Path, seeds: list[dict]) -> None:
    from researcher_profiles.schema import SeedPapersDocument
    from researcher_profiles.schema.jsonld import canonical_dumps

    doc = SeedPapersDocument.model_validate({"hasPart": seeds})
    (path / "sources" / "seed_papers.jsonld").write_text(
        canonical_dumps(doc.model_dump(mode="json")), encoding="utf-8"
    )


_SEEDS = [
    {"title": "Seed one", "openalex_id": "W1", "abstract": "Chromatin accessibility."},
    {"title": "Title only seed", "doi": "10.1093/seed.x"},
]


class TestSeedChunks:
    """Interest seed papers are indexed at every level, and never count as papers."""

    @pytest.mark.parametrize("level", ["lite", "full", "deep"])
    def test_every_level_indexes_seed_abstracts(self, tmp_path, level):
        from researcher_profiles.embeddings.chunking import PAPER_CHUNK_TYPES

        d = build_profile_dir(
            tmp_path / "p",
            level=level,
            papers=[{"paper_id": "a2020x", "title": "T", "abstract": "Own abstract."}],
        )
        _write_seeds(d, _SEEDS)
        chunks = SqliteEmbeddingIndex(d)._enumerate_chunks()
        seeds = [(c.source_id, c.text) for c in chunks if c.source_type == "seed_abstract"]
        assert seeds == [
            ("W1", "Seed one\n\nChromatin accessibility."),
            ("doi:10.1093/seed.x", "Title only seed"),
        ]
        assert "seed_abstract" not in PAPER_CHUNK_TYPES

    def test_seeds_never_count_as_papers(self, tmp_path):
        d = build_profile_dir(
            tmp_path / "p",
            level="lite",
            personality=False,
            summaries=False,
            papers=[{"paper_id": "a2020x", "title": "T", "abstract": "Own."}],
        )
        _write_seeds(d, _SEEDS)
        idx = SqliteEmbeddingIndex(d)
        idx.build_index(backend=FakeBackend())
        n_chunks, n_papers = idx.counts()
        assert (n_chunks, n_papers) == (3, 1)

    def test_private_seeds_never_reach_the_flat_export(self, tmp_path):
        d = build_profile_dir(tmp_path / "p")
        _write_seeds(d, _SEEDS)
        from .factories import refresh_manifest

        refresh_manifest(d)
        idx = SqliteEmbeddingIndex(d)
        idx.build_index(backend=FakeBackend())
        result = write_flat_export(d)
        chunks = json.loads(result.chunks_path.read_text())
        assert "seed_abstract" not in {c["source_type"] for c in chunks}
        assert result.dropped >= 2


class TestBlendCentroid:
    """The one centroid rule: authored work dominates, seeds nudge."""

    @pytest.mark.parametrize(
        ("n_auth", "n_seed", "has_authored", "expected"),
        [
            pytest.param(0, 5, False, 1.0, id="pure-seed-profile"),
            pytest.param(0, 10, True, 10 / 13, id="narrative-only-no-cap"),
            pytest.param(2, 10, True, 0.4, id="few-papers-capped"),
            pytest.param(20, 5, True, 5 / 65, id="twenty-papers"),
            pytest.param(100, 10, True, 10 / 310, id="senior-with-upvotes"),
            pytest.param(100, 0, True, 0.0, id="no-seeds"),
        ],
    )
    def test_seed_share_worked_examples(self, n_auth, n_seed, has_authored, expected):
        from researcher_profiles.embeddings.profile_vec import seed_share

        assert seed_share(n_auth, n_seed, has_authored_chunks=has_authored) == pytest.approx(
            expected
        )

    def test_no_seeds_is_the_plain_mean(self):
        from researcher_profiles.embeddings.profile_vec import blend_centroid

        rng = np.random.default_rng(0)
        vecs = rng.normal(size=(5, 8)).astype(np.float32)
        metas = [{"source_type": "paper_summary", "source_id": f"p{i}"} for i in range(5)]
        mean = vecs.mean(axis=0)
        np.testing.assert_allclose(
            blend_centroid(vecs, metas), mean / np.linalg.norm(mean), rtol=1e-5
        )

    def test_seeds_only_gives_their_direction(self):
        from researcher_profiles.embeddings.profile_vec import blend_centroid

        vecs = np.array([[1, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
        metas = [
            {"source_type": "seed_abstract", "source_id": "W1"},
            {"source_type": "seed_abstract", "source_id": "W1"},  # long abstract, one vote
            {"source_type": "seed_abstract", "source_id": "W2"},
        ]
        out = blend_centroid(vecs, metas)
        np.testing.assert_allclose(out, np.array([1, 1, 0]) / np.sqrt(2), rtol=1e-5)

    def test_authored_work_dominates(self):
        from researcher_profiles.embeddings.profile_vec import blend_centroid

        # Two authored papers along x, ten seeds along y: seeds are capped at 40%.
        vecs = np.array([[1, 0]] * 2 + [[0, 1]] * 10, dtype=np.float32)
        metas = [{"source_type": "paper_abstract", "source_id": f"p{i}"} for i in range(2)] + [
            {"source_type": "seed_abstract", "source_id": f"W{i}"} for i in range(10)
        ]
        out = blend_centroid(vecs, metas)
        expected = np.array([0.6, 0.4]) / np.linalg.norm([0.6, 0.4])
        np.testing.assert_allclose(out, expected, rtol=1e-5)

    def test_a_cached_plain_mean_centroid_is_recomputed(self, indexed_profile):
        """A centroid cached under the old rule is not trusted after an upgrade."""
        from researcher_profiles.embeddings.profile_vec import (
            CENTROID_RULE,
            _load_cache_at,
            _save_cache_at,
        )

        prof = ResearcherProfile.from_files(indexed_profile)
        first = prof.index.embedding("centroid")
        npz = cache_dir(indexed_profile) / "profile_vec.npz"
        cache = _load_cache_at(npz)
        assert str(cache["_centroid_rule"]) == CENTROID_RULE
        del cache["_centroid_rule"]
        cache["centroid"] = np.zeros_like(first)
        _save_cache_at(npz, cache)
        np.testing.assert_allclose(prof.index.embedding("centroid"), first, rtol=1e-6)
