"""The ``VectorStore`` capability over a directory, a published site and a database.

The parity tests are the ones that matter: the same query over the same profiles,
ranked through a filesystem, an HTTP and a SQL store, has to agree. Offline:
``FakeBackend`` vectors, a file-backed ``httpx`` transport, in-memory SQLite.
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
from researcher_profiles.errors import CapabilityUnavailableError, ProfileWriteError
from researcher_profiles.publish import build_site, render_profile
from researcher_profiles.store import FilesystemProfileStore, ProfileNotFoundError
from researcher_profiles.store.http import HttpProfileStore

from .factories import FakeBackend, build_profile_dir, new_sql_store, patch_embedding_backends

pytestmark = pytest.mark.usefixtures("fake_backend")

_RIDS = {
    "ada-lovelace": "0000-0001-2345-6789",
    "grace-hopper": "0000-0002-1825-0097",
}

_QUERY = "chromatin accessibility and region set enrichment"


# ---------------------------------------------------------------------------
# A published site, served over a file-backed transport
# ---------------------------------------------------------------------------


class _DirectoryTransport(httpx.BaseTransport):
    """Serve a local directory as a static host (a real client with a fake bottom)."""

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


def _build_indexed_root(dest: Path) -> None:
    """Two profiles, each with a real sqlite index, its flat export, and a render."""
    fb = FakeBackend()
    with pytest.MonkeyPatch.context() as mp:
        patch_embedding_backends(mp, fb)
        for slug, rid in _RIDS.items():
            d = build_profile_dir(dest / slug, name=slug.replace("-", " ").title(), rid=rid)
            SqliteEmbeddingIndex(d).build_index(backend=fb)
            assert write_flat_export(d, backend=fb) is not None
            render_profile(d)


def _build_site(golden):
    """The root published: collection files plus the profile folders (the deploy rsync)."""

    def _build(dest: Path) -> None:
        with pytest.MonkeyPatch.context() as mp:
            patch_embedding_backends(mp, FakeBackend())
            root = shutil.copytree(golden("vs-root", _build_indexed_root), dest / "_root")
            build_site(root, dest / "site", base_url="https://profiles.example.org")
            for slug in _RIDS:
                shutil.copytree(root / slug, dest / "site" / "profiles" / slug)
            shutil.rmtree(root)

    return _build


def _add_unbuilt(root: Path) -> Path:
    return build_profile_dir(
        root / "unbuilt", name="Unbuilt", rid="0000-0004-1415-9266", index=None
    )


@pytest.fixture
def indexed_root(golden, tmp_path: Path) -> Path:
    return shutil.copytree(golden("vs-root", _build_indexed_root), tmp_path / "profiles")


@pytest.fixture
def published_site(golden, tmp_path: Path) -> Path:
    return shutil.copytree(golden("vs-site", _build_site(golden)) / "site", tmp_path / "site")


@pytest.fixture
def site_transport(published_site: Path) -> _DirectoryTransport:
    return _DirectoryTransport(published_site)


@pytest.fixture
def http_store(site_transport: _DirectoryTransport) -> HttpProfileStore:
    client = httpx.Client(transport=site_transport)
    store = HttpProfileStore("https://profiles.example.org", client=client)
    yield store
    client.close()


@pytest.fixture
def fs_store(indexed_root: Path) -> FilesystemProfileStore:
    return FilesystemProfileStore(indexed_root)


@pytest.fixture
def sql_store(indexed_root: Path):
    """The same corpus ingested into SQL: ``put`` shreds the vectors into rows."""
    store = new_sql_store()
    for slug in _RIDS:
        store.put(ResearcherProfile.from_files(indexed_root / slug))
    return store


class _NoVectorStore:
    """A store with no vector capability: delegates everything but the vector methods."""

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

    def test_ranking_over_a_non_vector_store_raises_up_front(self, fs_store):
        """One actionable error before any work, not an empty ranking."""
        from researcher_profiles.analytics.centroids import CentroidManager
        from researcher_profiles.analytics.match import MatchManager
        from researcher_profiles.analytics.roster import _RosterCache

        bad = _NoVectorStore(fs_store)
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

    def test_search_returns_hits_without_text(self, http_store):
        hits = http_store.vector_index("ada-lovelace").search(_QUERY, k=3)
        assert hits
        assert not hasattr(hits[0], "text")

    def test_centroid_prefers_the_stacked_blob_over_per_profile_fetches(
        self, http_store, site_transport
    ):
        http_store.centroid("ada-lovelace")
        http_store.centroid("grace-hopper")
        assert not any(r.endswith(".chunks.json") for r in site_transport.requested), (
            "a per-profile flat index was fetched when the collection blob had the answer"
        )

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
        """Order and evidence, not raw score: the sqlite and flat readers score on different scales."""
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
        """A database deployment ranks like the root it holds."""
        local = fs_store.match.rank(_QUERY, k=2, normalize=False, diversify=False)
        db = sql_store.match.rank(_QUERY, k=2, normalize=False, diversify=False)
        assert local and db
        assert [m.profile.slug for m in local] == [m.profile.slug for m in db]
        for a, b in zip(local, db):
            assert a.evidence.centroid_score == pytest.approx(b.evidence.centroid_score, abs=1e-5)

    def test_write_lookup_index_is_accepted_by_a_remote_store(self, http_store):
        """The composition root (``api/deps.py``, ``analytics/indexes.py``) calls it on every store."""
        http_store.write_lookup_index()

    def test_backend_spec_is_detected_through_the_store(self, fs_store, http_store, sql_store):
        assert fs_store.backend_spec == "fake:tiny"
        assert http_store.backend_spec == "fake:tiny"
        assert sql_store.backend_spec == "fake:tiny"

    def test_an_unindexed_profile_is_rostered_but_never_ranks(self, indexed_root):
        """It gets a zero centroid row, which scores 0 against every query."""
        _add_unbuilt(indexed_root)
        store = FilesystemProfileStore(indexed_root)
        assert len(store.list_slugs()) == 3
        assert not store.has_vector_index("unbuilt")

        roster, matrix = store.centroids.snapshot()
        assert len(roster.slugs) == 3
        assert matrix.shape[0] == 3
        assert not matrix[roster.slugs.index("unbuilt")].any()
        ranked = store.match.rank(_QUERY, k=3, normalize=False, diversify=False)
        assert "unbuilt" not in [m.profile.slug for m in ranked if m.score > 0]


# ---------------------------------------------------------------------------
# The same capability over the HTTP and the SQL store
# ---------------------------------------------------------------------------


_REMOTE = ["http_store", "sql_store"]


class TestRemoteStoreVectors:
    """Both stores with no directory answer the vector questions the same way."""

    @pytest.mark.parametrize("store_fixture", _REMOTE)
    def test_has_vector_index_answers_for_the_whole_roster(self, request, store_fixture):
        store = request.getfixturevalue(store_fixture)
        assert store.has_vector_index("ada-lovelace")
        assert not store.has_vector_index("not-a-profile")
        assert store.has_vector_index(_RIDS["ada-lovelace"])

    @pytest.mark.parametrize("store_fixture", _REMOTE)
    def test_centroids_matrix_is_one_call_for_the_whole_roster(self, request, store_fixture):
        slugs, vectors = request.getfixturevalue(store_fixture).centroids_matrix()
        assert slugs == sorted(_RIDS)
        assert vectors.shape == (2, 16)
        # Published normalized (spec section 8), so no caller has to renormalize.
        assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)

    @pytest.mark.parametrize("store_fixture, atol", [("http_store", 1e-5), ("sql_store", 1e-6)])
    def test_centroid_matches_the_local_sqlite(self, request, indexed_root, store_fixture, atol):
        """A mismatch means a deployment ranks its profiles differently from its source root."""
        local = SqliteEmbeddingIndex(indexed_root / "ada-lovelace").centroid()
        store = request.getfixturevalue(store_fixture)
        assert np.allclose(store.centroid("ada-lovelace"), local, atol=atol)

    @pytest.mark.parametrize("store_fixture", _REMOTE)
    def test_vector_index_searches_the_stored_vectors(self, request, store_fixture):
        idx = request.getfixturevalue(store_fixture).vector_index("ada-lovelace")
        assert idx.backend_spec == "fake:tiny"
        assert idx.count > 0
        hits = idx.search(_QUERY, k=3)
        assert hits
        assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)

    @pytest.mark.parametrize("store_fixture", _REMOTE)
    def test_ranking_needs_no_directory(self, request, store_fixture):
        store = request.getfixturevalue(store_fixture)
        assert store.centroids.matrix.shape == (2, 16)
        assert store.centroids.matrix is store.centroids.matrix  # memoized in process


# ---------------------------------------------------------------------------
# The filesystem store's own vector paths
# ---------------------------------------------------------------------------


class TestFilesystemStoreVectors:
    """The directory backend serves whichever form the profile actually has."""

    def test_sqlite_wins_when_both_forms_exist(self, fs_store):
        hits = fs_store.vector_index("ada-lovelace").search(_QUERY, k=1)
        assert hits and hits[0].text  # the build-local sqlite is the richer index: hits carry text

    def test_flat_is_used_when_there_is_no_sqlite(self, fs_store, indexed_root):
        """A directory of *downloaded* profiles still ranks, at the public subset."""
        (indexed_root / "ada-lovelace" / ".cache" / "embeddings.sqlite").unlink()
        fs_store.evict("ada-lovelace")
        idx = fs_store.vector_index("ada-lovelace")
        published = FlatEmbeddingIndex.load(indexed_root / "ada-lovelace" / "embeddings")
        assert idx.count == published.count
        assert idx.search(_QUERY, k=3)
        assert fs_store.centroid("ada-lovelace").shape == (16,)

    def test_a_profile_with_neither_form_says_so(self, fs_store, indexed_root):
        from researcher_profiles.embeddings import IndexNotBuiltError

        _add_unbuilt(indexed_root)
        with pytest.raises(IndexNotBuiltError, match="has no vectors"):
            fs_store.vector_index("unbuilt")

    def test_the_directory_matrix_is_built_from_each_profile_centroid(self, fs_store):
        """``.cache/centroids.npz`` is the caller's memo, not the store's answer."""
        roster, matrix = fs_store.centroids.snapshot()
        for i, slug in enumerate(roster.slugs):
            assert np.allclose(matrix[i], fs_store.centroid(slug), atol=1e-6)


# ---------------------------------------------------------------------------
# The SQL store's own vector paths
# ---------------------------------------------------------------------------


class TestSqlStoreVectors:
    """Vectors as relational rows, not one opaque ``embeddings.sqlite`` BLOB."""

    def test_the_chunk_rows_are_the_published_public_subset(self, sql_store, indexed_root):
        """Embeddings are partially invertible, so a private chunk must not reach a queryable table."""
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

    def test_the_centroid_is_a_row_not_a_recompute(self, sql_store):
        """``centroid`` reads ``rp_profile_vectors`` (whole index), never the public chunks."""
        from researcher_profiles.store.db import ProfileVectorRow

        rid = sql_store.rid_for("ada-lovelace")
        with sql_store.session() as sess:
            row = sess.get(ProfileVectorRow, (rid, "centroid"))
        assert np.allclose(sql_store.centroid("ada-lovelace"), np.frombuffer(row.vector, "<f4"))

    def test_the_rows_rebuild_the_published_blob_exactly(self, sql_store, indexed_root):
        """Round trip: the bytes served from rows equal the bytes exported to disk."""
        emb = indexed_root / "ada-lovelace" / "embeddings"
        published = FlatEmbeddingIndex.load(emb)
        assert np.array_equal(sql_store.vector_index("ada-lovelace").vectors, published.vectors)

    def test_a_profile_ingested_without_an_index_says_so(self, sql_store, indexed_root):
        from researcher_profiles.embeddings import IndexNotBuiltError

        _add_unbuilt(indexed_root)
        sql_store.put(ResearcherProfile.from_files(indexed_root / "unbuilt"))
        assert not sql_store.has_vector_index("unbuilt")
        with pytest.raises(IndexNotBuiltError, match="rp_chunk_vectors"):
            sql_store.vector_index("unbuilt")

    def test_export_writes_the_flat_form_and_no_sqlite_blob(self, sql_store, tmp_path):
        """``index.json`` is regenerated beside its blob; nothing reconstitutes the sqlite."""
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

    def test_a_profile_with_only_the_flat_form_keeps_its_vectors(
        self, indexed_root, empty_sql_store
    ):
        """A downloaded profile has ``embeddings/`` and no ``.cache/``; ingest must keep its vectors."""
        store = empty_sql_store
        prof_dir = indexed_root / "ada-lovelace"
        published = FlatEmbeddingIndex.load(prof_dir / "embeddings")
        (prof_dir / ".cache" / "embeddings.sqlite").unlink()

        store.put(ResearcherProfile.from_files(prof_dir))
        assert store.has_vector_index("ada-lovelace")
        assert np.array_equal(store.vector_index("ada-lovelace").vectors, published.vectors)
        assert np.allclose(store.centroid("ada-lovelace"), published.centroid(), atol=1e-6)


# ---------------------------------------------------------------------------
# The flat reader, byte-constructible
# ---------------------------------------------------------------------------


def _flat_parts(emb: Path) -> tuple[bytes, bytes, bytes]:
    """(index_json, blob, chunks) bytes of a flat export."""
    index_json = (emb / "index.json").read_bytes()
    blob = json.loads(index_json)["file"]
    chunks = (emb / (Path(blob).stem + ".chunks.json")).read_bytes()
    return index_json, (emb / blob).read_bytes(), chunks


class TestFlatFromBytes:
    """``from_bytes`` is what lets a reader exist without a filesystem."""

    def test_from_bytes_and_load_agree(self, indexed_root):
        emb = indexed_root / "ada-lovelace" / "embeddings"
        from_bytes = FlatEmbeddingIndex.from_bytes(*_flat_parts(emb))
        assert np.array_equal(from_bytes.vectors, FlatEmbeddingIndex.load(emb).vectors)

    def test_a_truncated_blob_is_refused(self, indexed_root):
        index_json, blob, chunks = _flat_parts(indexed_root / "ada-lovelace" / "embeddings")
        with pytest.raises(ValueError, match="byte length"):
            FlatEmbeddingIndex.from_bytes(index_json, blob[:-4], chunks)

    def test_the_flat_centroid_is_the_normalized_mean_of_its_rows(self, indexed_root):
        flat = FlatEmbeddingIndex.load(indexed_root / "ada-lovelace" / "embeddings")
        expected = flat.vectors.mean(axis=0)
        expected = expected / np.linalg.norm(expected)
        assert np.allclose(flat.centroid(), expected, atol=1e-6)
