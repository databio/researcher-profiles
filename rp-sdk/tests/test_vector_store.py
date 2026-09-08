"""The ``VectorStore`` capability and its two implementers.

The cross-profile analytics used to reach past their store straight to
``.cache/embeddings.sqlite``, which is why every one of them degraded to a 503
the moment there was no directory. Vectors have an interface now
(:class:`~researcher_profiles.embeddings.protocol.VectorIndex`) and stores have
a capability that serves them
(:class:`~researcher_profiles.store.VectorStore`), so the same ranking runs
over a directory, over a published site read with nothing but HTTP, and over a
database.

The parity tests are the ones that matter: the same query, over the same
profiles, ranked through a ``FilesystemProfileStore``, through an
``HttpProfileStore`` pointed at that root's published output, and through a
``SqlProfileStore`` loaded from the same corpus, has to agree. If it ever stops
agreeing, the abstraction is decorative.

Everything here is offline: ``FakeBackend`` supplies the vectors, a
file-backed ``httpx`` transport supplies the network, and the database is
in-memory SQLite.
"""

import json
import shutil
from pathlib import Path

import httpx
import numpy as np
import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.embeddings.cache import SqliteEmbeddingIndex
from researcher_profiles.embeddings.flat import FlatEmbeddingIndex, write_flat_export
from researcher_profiles.embeddings.protocol import VectorIndex
from researcher_profiles.errors import CapabilityUnavailableError, ProfileWriteError
from researcher_profiles.publish import build_site
from researcher_profiles.store import FilesystemProfileStore, ProfileNotFoundError
from researcher_profiles.store.http import HttpProfileStore
from researcher_profiles.store.protocol import VectorStore

from .factories import FakeBackend, build_profile_dir

_RIDS = {
    "ada-lovelace": "0000-0001-2345-6789",
    "grace-hopper": "0000-0002-1825-0097",
}

_QUERY = "chromatin accessibility and region set enrichment"


# ---------------------------------------------------------------------------
# A published site, served over a file-backed transport
# ---------------------------------------------------------------------------


class _DirectoryTransport(httpx.BaseTransport):
    """Serve a local directory as if it were a static host.

    A real ``httpx.Client`` with a fake bottom, rather than a fake client: the
    store's 404 / 403 / binary handling is code under test, and a hand-rolled
    double would let a bug in it pass.
    """

    def __init__(self, root: Path):
        self._root = root
        self.requested: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        rel = request.url.path.lstrip("/")
        self.requested.append(rel)
        path = self._root / rel
        # A static host serves files, not directories, and never escapes its root.
        try:
            resolved = path.resolve()
            resolved.relative_to(self._root.resolve())
        except ValueError:
            return httpx.Response(403, text="forbidden")
        if not resolved.is_file():
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=resolved.read_bytes())


@pytest.fixture
def fake_backend_everywhere(monkeypatch) -> FakeBackend:
    """Resolve every ``backend_spec`` to ``FakeBackend``: no model, no network.

    ``fake:tiny`` is not a spec the real resolver knows, and three separate
    call sites re-resolve a backend from the name recorded in an index: the
    sqlite search path, the flat search path, and the store's query embedder.
    """
    import researcher_profiles.analytics.centroids as centroids
    import researcher_profiles.embeddings.backends as backends
    import researcher_profiles.embeddings.cache as cache

    monkeypatch.delenv("RESEARCHER_PROFILES_EMBEDDING_BACKEND", raising=False)
    monkeypatch.setattr(backends, "get_backend", lambda spec=None: FakeBackend())
    monkeypatch.setattr(cache, "get_backend", lambda spec=None: FakeBackend())
    monkeypatch.setattr(centroids, "get_backend", lambda spec=None: FakeBackend())
    return FakeBackend()


@pytest.fixture
def indexed_root(tmp_path: Path, fake_backend_everywhere: FakeBackend) -> Path:
    """A two-profile root, each with a real sqlite index and its flat export."""
    root = tmp_path / "profiles"
    root.mkdir()
    for slug, rid in _RIDS.items():
        prof_dir = build_profile_dir(root / slug, name=slug.replace("-", " ").title(), rid=rid)
        SqliteEmbeddingIndex(prof_dir).build_index(backend=fake_backend_everywhere)
        assert write_flat_export(prof_dir, backend=fake_backend_everywhere) is not None
    return root


@pytest.fixture
def published_site(indexed_root: Path, tmp_path: Path) -> Path:
    """``indexed_root`` published: collection files plus the profile folders.

    ``build_site`` writes only the documents *about* the collection; deployment
    rsyncs the folders alongside them (``publish/_site.py``). The copy here is
    that rsync, so the tree an ``HttpProfileStore`` reads is the tree a real
    static host serves.
    """
    out = tmp_path / "site"
    build_site(indexed_root, out, base_url="https://profiles.example.org")
    for slug in _RIDS:
        shutil.copytree(indexed_root / slug, out / "profiles" / slug)
    return out


@pytest.fixture
def http_store(published_site: Path) -> HttpProfileStore:
    client = httpx.Client(transport=_DirectoryTransport(published_site))
    store = HttpProfileStore("https://profiles.example.org", client=client)
    yield store
    client.close()


@pytest.fixture
def fs_store(indexed_root: Path) -> FilesystemProfileStore:
    return FilesystemProfileStore(indexed_root)


@pytest.fixture
def sql_store(indexed_root: Path):
    """A SQL-backed store holding the same corpus, vectors and all.

    Ingest is where vectors enter a database-backed store: ``put`` shreds each
    profile's index into ``rp_chunk_vectors`` and its centroid into
    ``rp_profile_vectors``, and everything downstream is a query.
    """
    from researcher_profiles.store.sql import SqlProfileStore

    store = SqlProfileStore("sqlite://")
    store.create_all()
    for slug in _RIDS:
        store.put(ResearcherProfile.from_files(indexed_root / slug))
    return store


class _NoVectorStore:
    """A store with no vector capability at all, for the up-front-error test.

    Delegates the base contract to a real filesystem store and simply does not
    define the ``VectorStore`` methods (nor the analytics accessors the mixin
    supplies alongside them), which is what a backend over a bare metadata API
    would look like. Written by hand rather than found among the SDK's backends
    because all three of those serve vectors now.
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in (
            "backend_spec",
            "has_vector_index",
            "vector_index",
            "centroid",
            "centroids_matrix",
            "centroids",
            "match",
            "indexes",
        ):
            raise AttributeError(name)
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------
# The capability itself
# ---------------------------------------------------------------------------


class TestCapability:
    """Who satisfies ``VectorStore``, and what a store that does not gets told."""

    def test_all_three_backends_satisfy_the_protocol(self, fs_store, http_store, sql_store):
        assert isinstance(fs_store, VectorStore)
        assert isinstance(http_store, VectorStore)
        assert isinstance(sql_store, VectorStore)

    def test_every_index_reader_satisfies_the_index_protocol(self, fs_store, http_store, sql_store):
        """The sqlite reader and the flat reader are interchangeable to a caller."""
        assert isinstance(fs_store.vector_index("ada-lovelace"), VectorIndex)
        assert isinstance(http_store.vector_index("ada-lovelace"), VectorIndex)
        assert isinstance(sql_store.vector_index("ada-lovelace"), VectorIndex)

    def test_ranking_over_a_non_vector_store_raises_up_front(self, fs_store):
        """One actionable error before any work, not an empty ranking.

        The failure mode this replaces: every analytic discovering separately
        that there was no directory, and each turning that into its own 503, so
        "this deployment cannot rank" arrived as "nobody matched".
        """
        from researcher_profiles.analytics.centroids import CentroidManager
        from researcher_profiles.analytics.match import MatchManager
        from researcher_profiles.analytics.roster import _RosterCache

        bad = _NoVectorStore(fs_store)
        assert not isinstance(bad, VectorStore)
        rostered = _RosterCache(bad)
        assert len(rostered()) == 2  # the roster itself needs no vectors
        with pytest.raises(CapabilityUnavailableError, match="serve vectors"):
            MatchManager(bad, rostered).rank(_QUERY, k=2)
        with pytest.raises(CapabilityUnavailableError, match="serve vectors"):
            _ = CentroidManager(bad, rostered).matrix


# ---------------------------------------------------------------------------
# HttpProfileStore: the read half of ProfileStore
# ---------------------------------------------------------------------------


class TestHttpStoreReads:
    """Enumeration, resolution and bytes, read off the published files."""

    def test_list_slugs_comes_from_index_json(self, http_store):
        assert http_store.list_slugs() == sorted(_RIDS)

    def test_resolve_slug_accepts_a_rid(self, http_store):
        assert http_store.resolve_slug(_RIDS["grace-hopper"]) == "grace-hopper"

    def test_resolve_slug_rejects_a_stranger(self, http_store):
        with pytest.raises(ProfileNotFoundError, match="nothing at"):
            http_store.resolve_slug("not-a-profile")

    def test_document_bytes_are_the_published_bytes(self, http_store, published_site):
        raw = http_store.document_bytes("ada-lovelace")
        assert raw == (published_site / "profiles/ada-lovelace/profile.jsonld").read_bytes()

    def test_get_loads_a_working_profile(self, http_store):
        prof = http_store.get("ada-lovelace")
        assert prof.slug == "ada-lovelace"
        assert prof.rid == _RIDS["ada-lovelace"]

    def test_artifact_bytes_refuses_to_climb_out_of_the_profile(self, http_store):
        with pytest.raises(ProfileNotFoundError, match="not inside profile"):
            http_store.artifact_bytes("ada-lovelace", "../grace-hopper/profile.jsonld")

    def test_export_directory_round_trips_the_published_form(self, http_store, tmp_path):
        out = http_store.export_directory("ada-lovelace", tmp_path / "exported")
        assert (out / "profile.jsonld").is_file()
        assert (out / "embeddings" / "index.json").is_file()
        # The export is searchable on its own, with no network and no sqlite.
        assert FlatEmbeddingIndex.load(out / "embeddings").count > 0

    @pytest.mark.parametrize(
        "call",
        [
            lambda s: s.delete("ada-lovelace"),
            lambda s: s.commit_directory("x", Path("/tmp/nope")),
            lambda s: s.put_document("ada-lovelace", None),
        ],
        ids=["delete", "commit_directory", "put_document"],
    )
    def test_every_writer_is_refused(self, http_store, call):
        with pytest.raises(ProfileWriteError, match="remote profile site"):
            call(http_store)


# ---------------------------------------------------------------------------
# HttpProfileStore: the vectors
# ---------------------------------------------------------------------------


class TestHttpStoreVectors:
    """Where the capability earns its keep: vectors with no filesystem."""

    def test_vector_index_is_built_from_the_published_bytes(self, http_store):
        idx = http_store.vector_index("ada-lovelace")
        assert idx.backend_spec == "fake:tiny"
        assert idx.count > 0

    def test_search_returns_hits_without_text(self, http_store):
        hits = http_store.vector_index("ada-lovelace").search(_QUERY, k=3)
        assert hits
        assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
        assert not hasattr(hits[0], "text")

    def test_centroids_matrix_is_one_fetch_for_the_whole_roster(self, http_store):
        slugs, vectors = http_store.centroids_matrix()
        assert slugs == sorted(_RIDS)
        assert vectors.shape == (2, 16)
        # Published normalized (spec section 8), so no caller has to renormalize.
        assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)

    def test_centroid_matches_the_local_sqlite(self, http_store, indexed_root):
        """The published centroid is the one the build machine computed.

        Not merely "some unit vector": a mismatch here means a site ranks its
        own profiles differently from the root it was published from.
        """
        local = SqliteEmbeddingIndex(indexed_root / "ada-lovelace").centroid()
        assert np.allclose(http_store.centroid("ada-lovelace"), local, atol=1e-5)

    def test_centroid_prefers_the_stacked_blob_over_per_profile_fetches(self, http_store):
        http_store.centroid("ada-lovelace")
        http_store.centroid("grace-hopper")
        transport = http_store._http._transport
        assert not any(r.endswith(".chunks.json") for r in transport.requested), (
            "a per-profile flat index was fetched when the collection blob had the answer"
        )

    def test_backend_spec_comes_off_the_registry_index(self, http_store):
        assert http_store.backend_spec == "fake:tiny"

    def test_has_vector_index_answers_for_the_whole_roster(self, http_store):
        assert http_store.has_vector_index("ada-lovelace")
        assert not http_store.has_vector_index("not-a-profile")

    def test_a_tampered_blob_is_refused(self, http_store, published_site):
        emb = published_site / "profiles" / "ada-lovelace" / "embeddings"
        index = json.loads((emb / "index.json").read_text())
        blob_path = emb / index["file"]
        raw = bytearray(blob_path.read_bytes())
        raw[0] ^= 0xFF
        blob_path.write_bytes(bytes(raw))
        with pytest.raises(ValueError, match="sha256"):
            http_store.vector_index("ada-lovelace")


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------


class TestRankingParity:
    """The same query ranks the same over a directory and over its published site."""

    def test_ranking_agrees_between_the_two_stores(self, fs_store, http_store):
        """Same profiles, same order, same evidence.

        Order and evidence, not raw score: the two readers report similarity on
        different scales (the sqlite reader rescales sqlite-vec's L2 distance,
        the flat reader returns plain cosine), which is a pre-existing
        difference in ``_rows_to_hits`` and not something the store abstraction
        introduced. Both are monotone in cosine, so the ranking is identical.
        """
        local = fs_store.match.rank(_QUERY, k=2, normalize=False, diversify=False)
        remote = http_store.match.rank(_QUERY, k=2, normalize=False, diversify=False)
        assert local and remote
        assert [m.profile.slug for m in local] == [m.profile.slug for m in remote]
        for a, b in zip(local, remote):
            assert a.evidence.centroid_score == pytest.approx(b.evidence.centroid_score, abs=1e-5)
            assert [h.source_id for h in a.evidence.top_chunks] == [
                h.source_id for h in b.evidence.top_chunks
            ]

    def test_ranking_agrees_between_the_directory_and_the_database(self, fs_store, sql_store):
        """The scope-C payoff: a database deployment ranks like the root it holds.

        Same corpus, same order, same centroid scores. The SQL store serves the
        public flat subset (as the published site does), so the comparison is
        the same one the HTTP parity test makes and holds for the same reason:
        both readers are monotone in cosine over the same vectors.
        """
        local = fs_store.match.rank(_QUERY, k=2, normalize=False, diversify=False)
        db = sql_store.match.rank(_QUERY, k=2, normalize=False, diversify=False)
        assert local and db
        assert [m.profile.slug for m in local] == [m.profile.slug for m in db]
        for a, b in zip(local, db):
            assert a.evidence.centroid_score == pytest.approx(b.evidence.centroid_score, abs=1e-5)

    def test_ranking_a_sql_store_needs_no_directory(self, sql_store):
        assert sql_store.root is None
        assert sql_store.centroids.matrix.shape == (2, 16)

    def test_ranking_a_remote_store_needs_no_directory(self, http_store):
        assert http_store.root is None
        assert http_store.centroids.cache_path is None
        assert http_store.match.topics_cache_path is None
        # Still ranks, and still memoizes the matrix in process.
        assert http_store.centroids.matrix.shape == (2, 16)
        assert http_store.centroids.matrix is http_store.centroids.matrix

    def test_write_lookup_index_is_a_no_op_without_a_root(self, http_store, sql_store):
        assert http_store.write_lookup_index() is None
        assert sql_store.write_lookup_index() is None

    def test_backend_spec_is_detected_through_the_store(self, fs_store, http_store, sql_store):
        assert fs_store.backend_spec == "fake:tiny"
        assert http_store.backend_spec == "fake:tiny"
        assert sql_store.backend_spec == "fake:tiny"

    def test_an_unindexed_profile_is_rostered_but_never_ranks(self, indexed_root):
        """It gets a zero centroid row, which scores 0 against every query.

        The dissolved registry dropped such a profile from the roster
        entirely (``skip_unindexed``), which made a wholly unindexed
        deployment answer "nobody matched". The store keeps it; the serving
        layer asks "is anything indexed?" explicitly instead.
        """
        build_profile_dir(
            indexed_root / "unbuilt", name="Unbuilt", rid="0000-0004-1415-9266", index=None
        )
        store = FilesystemProfileStore(indexed_root)
        assert len(store.list_slugs()) == 3
        assert len(store._rostered()) == 3
        assert not store.has_vector_index("unbuilt")

        roster, matrix = store.centroids.snapshot()
        assert matrix.shape[0] == 3
        assert not matrix[roster.slugs.index("unbuilt")].any()
        ranked = store.match.rank(_QUERY, k=3, normalize=False, diversify=False)
        assert "unbuilt" not in [m.profile.slug for m in ranked if m.score > 0]


# ---------------------------------------------------------------------------
# The filesystem store's own vector paths
# ---------------------------------------------------------------------------


class TestFilesystemStoreVectors:
    """The directory backend serves whichever form the profile actually has."""

    def test_sqlite_wins_when_both_forms_exist(self, fs_store):
        assert isinstance(fs_store.vector_index("ada-lovelace"), SqliteEmbeddingIndex)

    def test_flat_is_used_when_there_is_no_sqlite(self, fs_store, indexed_root):
        """A directory of *downloaded* profiles still ranks, at the public subset."""
        (indexed_root / "ada-lovelace" / ".cache" / "embeddings.sqlite").unlink()
        fs_store.evict("ada-lovelace")
        assert isinstance(fs_store.vector_index("ada-lovelace"), FlatEmbeddingIndex)
        assert fs_store.centroid("ada-lovelace").shape == (16,)

    def test_a_profile_with_neither_form_says_so(self, fs_store, indexed_root):
        from researcher_profiles.embeddings import IndexNotBuiltError

        build_profile_dir(
            indexed_root / "unbuilt", name="Unbuilt", rid="0000-0004-1415-9266", index=None
        )
        with pytest.raises(IndexNotBuiltError, match="has no vectors"):
            fs_store.vector_index("unbuilt")

    def test_the_directory_backend_holds_no_stacked_matrix(self, fs_store):
        """``.cache/centroids.npz`` is the caller's memo, not the store's answer."""
        assert fs_store.centroids_matrix() is None


# ---------------------------------------------------------------------------
# The SQL store's own vector paths
# ---------------------------------------------------------------------------


class TestSqlStoreVectors:
    """Vectors as relational rows: the backend that used to have none.

    ``.cache/embeddings.sqlite`` used to land in ``rp_artifacts`` as one opaque
    BLOB, so a database-backed deployment had to export every profile to a temp
    directory and rank through the filesystem index. These are the queries that
    replaced that.
    """

    def test_the_chunk_rows_are_the_published_public_subset(self, sql_store, indexed_root):
        """The shred applies the export's privacy filter, not a weaker one.

        Embeddings are partially invertible (spec section 6), so a chunk built
        from a restricted source must no more reach a queryable table than it
        reaches a published ``.bin``. The assertion is against
        ``public_chunk_keys``, which is the exporter's own answer.
        """
        from sqlmodel import select

        from researcher_profiles.embeddings.flat import public_chunk_keys
        from researcher_profiles.store.db import ChunkVectorRow

        expected = public_chunk_keys(indexed_root / "ada-lovelace")
        rid = sql_store.rid_for("ada-lovelace")
        with sql_store.session() as sess:
            rows = list(
                sess.exec(
                    select(ChunkVectorRow)
                    .where(ChunkVectorRow.profile_rid == rid)
                    .order_by(ChunkVectorRow.ordinal)
                ).all()
            )
        assert expected
        assert [(r.source_type, r.source_id, r.chunk_index) for r in rows] == expected
        assert all(len(r.vector) == r.dim * 4 for r in rows)

    def test_centroid_matches_the_filesystem_index(self, sql_store, indexed_root):
        """The number this store reports is the number the build machine computed.

        Computed at ingest by ``profile_vec._centroid_vec`` over the whole
        index, exactly as ``SqliteEmbeddingIndex.centroid`` and the published
        ``collection/embeddings/`` blob are. A mismatch here means a database
        deployment ranks its own profiles differently from the root it was
        loaded from.
        """
        local = SqliteEmbeddingIndex(indexed_root / "ada-lovelace").centroid()
        assert np.allclose(sql_store.centroid("ada-lovelace"), local, atol=1e-6)

    def test_the_centroid_is_a_row_not_a_recompute(self, sql_store):
        """``centroid`` reads ``rp_profile_vectors``; it never opens the chunks.

        The two answer different questions (whole index vs. public subset), so
        a ``centroid`` that quietly fell through to ``vector_index`` would be a
        silent change of meaning, not an optimization.
        """
        from researcher_profiles.store.db import ProfileVectorRow

        rid = sql_store.rid_for("ada-lovelace")
        with sql_store.session() as sess:
            row = sess.get(ProfileVectorRow, (rid, "centroid"))
        assert row is not None
        assert np.allclose(sql_store.centroid("ada-lovelace"), np.frombuffer(row.vector, "<f4"))

    def test_centroids_matrix_is_one_select_for_the_whole_roster(self, sql_store):
        slugs, vectors = sql_store.centroids_matrix()
        assert slugs == sorted(_RIDS)
        assert vectors.shape == (2, 16)
        assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)

    def test_vector_index_searches_the_rows(self, sql_store):
        idx = sql_store.vector_index("ada-lovelace")
        assert isinstance(idx, FlatEmbeddingIndex)
        assert idx.backend_spec == "fake:tiny"
        hits = idx.search(_QUERY, k=3)
        assert hits
        assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)

    def test_the_rows_rebuild_the_published_blob_exactly(self, sql_store, indexed_root):
        """Round trip: the bytes served from rows equal the bytes exported to disk."""
        emb = indexed_root / "ada-lovelace" / "embeddings"
        published = FlatEmbeddingIndex.load(emb)
        assert np.array_equal(sql_store.vector_index("ada-lovelace").vectors, published.vectors)

    def test_backend_spec_and_has_vector_index_are_cheap_answers(self, sql_store):
        assert sql_store.backend_spec == "fake:tiny"
        assert sql_store.has_vector_index("ada-lovelace")
        assert sql_store.has_vector_index(_RIDS["ada-lovelace"])
        assert not sql_store.has_vector_index("not-a-profile")

    def test_a_profile_ingested_without_an_index_says_so(self, sql_store, indexed_root):
        from researcher_profiles.embeddings import IndexNotBuiltError

        build_profile_dir(
            indexed_root / "unbuilt", name="Unbuilt", rid="0000-0004-1415-9266", index=None
        )
        sql_store.put(ResearcherProfile.from_files(indexed_root / "unbuilt"))
        assert not sql_store.has_vector_index("unbuilt")
        with pytest.raises(IndexNotBuiltError, match="rp_chunk_vectors"):
            sql_store.vector_index("unbuilt")

    def test_export_writes_the_flat_form_and_no_sqlite_blob(self, sql_store, tmp_path):
        """The export serves the flat form; the opaque blob is gone for good.

        ``export_directory`` regenerates ``index.json`` beside the blob it
        describes, so the declared ``count`` and ``sha256`` cannot be stale.
        Nothing reconstitutes ``.cache/embeddings.sqlite``: the filesystem
        backend reads the flat form when there is no sqlite.
        """
        out = sql_store.export_directory("ada-lovelace", tmp_path / "out")
        assert not (out / ".cache" / "embeddings.sqlite").exists()
        idx = FlatEmbeddingIndex.load(out / "embeddings")
        assert idx.count == sql_store.vector_index("ada-lovelace").count
        assert np.array_equal(idx.vectors, sql_store.vector_index("ada-lovelace").vectors)

    def test_an_exported_profile_still_ranks_through_a_directory_store(self, sql_store, tmp_path):
        root = tmp_path / "exported"
        for slug in _RIDS:
            sql_store.export_directory(slug, root / slug)
        exported = FilesystemProfileStore(root)
        assert [m.profile.slug for m in exported.match.rank(_QUERY, k=2, diversify=False)]

    def test_a_reput_replaces_the_vector_rows_rather_than_doubling_them(
        self, sql_store, indexed_root
    ):
        before = sql_store.vector_index("ada-lovelace").count
        sql_store.put(ResearcherProfile.from_files(indexed_root / "ada-lovelace"))
        assert sql_store.vector_index("ada-lovelace").count == before

    def test_delete_takes_the_vector_rows_with_it(self, sql_store):
        from sqlmodel import select

        from researcher_profiles.store.db import ChunkVectorRow, ProfileVectorRow

        rid = sql_store.rid_for("ada-lovelace")
        sql_store.delete("ada-lovelace")
        with sql_store.session() as sess:
            assert not sess.exec(
                select(ChunkVectorRow).where(ChunkVectorRow.profile_rid == rid)
            ).all()
            assert sess.get(ProfileVectorRow, (rid, "centroid")) is None

    def test_a_profile_with_only_the_flat_form_keeps_its_vectors(self, indexed_root):
        """The second materializer: no sqlite to shred, only the published files.

        A downloaded profile has ``embeddings/`` and no ``.cache/``. Without this
        fallback, ingesting a corpus of published profiles into a database would
        silently lose every vector and leave ``/match`` empty.
        """
        from researcher_profiles.store.sql import SqlProfileStore

        prof_dir = indexed_root / "ada-lovelace"
        published = FlatEmbeddingIndex.load(prof_dir / "embeddings")
        (prof_dir / ".cache" / "embeddings.sqlite").unlink()

        store = SqlProfileStore("sqlite://")
        store.create_all()
        store.put(ResearcherProfile.from_files(prof_dir))
        assert store.has_vector_index("ada-lovelace")
        assert np.array_equal(store.vector_index("ada-lovelace").vectors, published.vectors)
        assert np.allclose(store.centroid("ada-lovelace"), published.centroid(), atol=1e-6)


# ---------------------------------------------------------------------------
# The flat reader, byte-constructible
# ---------------------------------------------------------------------------


class TestFlatFromBytes:
    """``from_bytes`` is what lets a reader exist without a filesystem."""

    def test_from_bytes_and_load_agree(self, indexed_root):
        emb = indexed_root / "ada-lovelace" / "embeddings"
        index_json = (emb / "index.json").read_bytes()
        index = json.loads(index_json)
        chunks_name = Path(index["file"]).stem + ".chunks.json"
        from_bytes = FlatEmbeddingIndex.from_bytes(
            index_json, (emb / index["file"]).read_bytes(), (emb / chunks_name).read_bytes()
        )
        assert np.array_equal(from_bytes.vectors, FlatEmbeddingIndex.load(emb).vectors)

    def test_a_truncated_blob_is_refused(self, indexed_root):
        emb = indexed_root / "ada-lovelace" / "embeddings"
        index_json = (emb / "index.json").read_bytes()
        index = json.loads(index_json)
        chunks_name = Path(index["file"]).stem + ".chunks.json"
        with pytest.raises(ValueError, match="byte length"):
            FlatEmbeddingIndex.from_bytes(
                index_json,
                (emb / index["file"]).read_bytes()[:-4],
                (emb / chunks_name).read_bytes(),
            )

    def test_the_flat_centroid_is_the_normalized_mean_of_its_rows(self, indexed_root):
        flat = FlatEmbeddingIndex.load(indexed_root / "ada-lovelace" / "embeddings")
        expected = flat.vectors.mean(axis=0)
        expected = expected / np.linalg.norm(expected)
        assert np.allclose(flat.centroid(), expected, atol=1e-6)
