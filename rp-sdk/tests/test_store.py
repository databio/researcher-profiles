"""The ``ProfileStore`` protocol, and the two backends against it.

The storage layer (``ArtifactStorage`` in ``storage.py``) is about one profile's
artifacts and is exercised in ``test_write_hooks.py``. This file is about the
level above it, a *set* of profiles, and its job is to prove the two shipped
backends are interchangeable behind
:class:`researcher_profiles.store.ProfileStore`.

Almost everything here is parametrized over both backends through the
``both_stores`` fixture. A contract asserted on only one of them is a wish.
``test_db.py`` owns what is specific to the SQL backend (the ``rp_*`` schema,
the projections, the byte-level round trip); this file owns what the two must
share.
"""

import hashlib
import json
from pathlib import Path

import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.env import RetiredEnvVarError
from researcher_profiles.errors import (
    ProfileError,
    ProfileWriteError,
    WriteHookError,
)
from researcher_profiles.profile.storage import ArtifactStorage, DirectoryArtifactStorage
from researcher_profiles.schema import PaperRecord, ProfileDocument
from researcher_profiles.store import (
    DuplicateIdentityError,
    FilesystemProfileStore,
    ProfileNotFoundError,
    ProfileStore,
    UploadError,
    build_store,
)
from researcher_profiles.store.config import DATABASE_URL_ENV_VAR, PROFILES_ROOT_ENV_VAR
from researcher_profiles.store.sql import SqlArtifactStorage, SqlProfileStore

from .factories import FakeBackend, build_profile_dir
from .test_profile import DictStorage


@pytest.fixture
def fake_embeddings(monkeypatch):
    """Force index builds and query embedding through the deterministic
    ``FakeBackend`` (dim 16, no model, no network), so a real end-to-end
    ``/match`` is offline and stable. ``get_match_store`` imports ``build_index``
    from ``researcher_profiles.embeddings`` at call time, so patching the module
    attribute reaches the materialization build; ``_resolve_backend`` embeds the
    query via ``profile_vec.get_backend``, patched to the same backend so the
    index and query vectors share a dimension. ``store.get_backend`` and
    ``backends.get_backend`` are patched too: a chunk search re-resolves the
    backend from the name recorded in the index, and ``fake:tiny`` is not a
    spec the real resolver knows.
    """
    import researcher_profiles.analytics.centroids as cen
    import researcher_profiles.embeddings as emb
    import researcher_profiles.embeddings.backends as backends
    import researcher_profiles.embeddings.cache as store
    import researcher_profiles.embeddings.profile_vec as pv

    monkeypatch.delenv("RESEARCHER_PROFILES_EMBEDDING_BACKEND", raising=False)
    # The flat reader (which is how a SQL store serves its rows) resolves a
    # backend through ``embeddings.backends`` rather than through the sqlite
    # reader's own module, so ``fake:tiny`` has to be answerable there too.
    monkeypatch.setattr(backends, "get_backend", lambda spec=None: FakeBackend())
    real_build = emb.build_index
    monkeypatch.setattr(
        emb,
        "build_index",
        lambda profile, *, force=False, backend=None: real_build(
            profile, force=force, backend=FakeBackend()
        ),
    )
    monkeypatch.setattr(pv, "get_backend", lambda spec=None: FakeBackend())
    monkeypatch.setattr(store, "get_backend", lambda spec=None: FakeBackend())
    if hasattr(cen, "get_backend"):
        monkeypatch.setattr(cen, "get_backend", lambda spec=None: FakeBackend())
    return FakeBackend()


def _relative_files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _explode(ctx):
    raise RuntimeError("dependent state unreachable")


def _new_document(rid: str, name: str) -> ProfileDocument:
    """The smallest document a store will accept for a create.

    ``provenance`` follows the rid: ``synthetic`` demands a ``local:`` id, and
    an ORCID demands a provenance that can hold one.
    """
    provenance = "synthetic" if rid.startswith("local:") else "third_party"
    return ProfileDocument(name=name, rid=rid, provenance=provenance)


@pytest.fixture
def fs_store(fixture_profiles_root) -> FilesystemProfileStore:
    return FilesystemProfileStore(fixture_profiles_root("jane-doe"))


@pytest.fixture
def sql_store(jane_doe) -> SqlProfileStore:
    store = SqlProfileStore("sqlite://")
    store.create_all()
    store.put(jane_doe)
    return store


@pytest.fixture
def indexed_sql_store(jane_doe_dir, fake_embeddings) -> SqlProfileStore:
    """A SQL store whose profile carries vectors, because ingest is where they enter.

    ``put`` shreds a built ``.cache/embeddings.sqlite`` into ``rp_chunk_vectors``
    and ``rp_profile_vectors``. A profile ingested without one is hosted but
    unrankable: the store serves the vectors it was given and never embeds
    anything itself.
    """
    from researcher_profiles.embeddings.cache import SqliteEmbeddingIndex

    SqliteEmbeddingIndex(jane_doe_dir).build_index(backend=fake_embeddings)
    store = SqlProfileStore("sqlite://")
    store.create_all()
    store.put(ResearcherProfile.from_files(jane_doe_dir))
    return store


@pytest.fixture(params=["filesystem", "sql"])
def both_stores(request) -> ProfileStore:
    """The same one-profile store, once per backend.

    Requested by name rather than built here so each backend's own fixture
    keeps its own setup; this only chooses.
    """
    return request.getfixturevalue("fs_store" if request.param == "filesystem" else "sql_store")


class TestStoreProtocol:
    """Everything here must hold on both backends or the protocol is a wish."""

    def test_both_backends_satisfy_the_protocol(self, both_stores):
        assert isinstance(both_stores, ProfileStore)

    def test_list_slugs(self, both_stores):
        assert both_stores.list_slugs() == ["jane-doe"]

    def test_resolve_slug_accepts_a_slug_or_a_rid(self, both_stores, jane_doe):
        assert both_stores.resolve_slug("jane-doe") == "jane-doe"
        assert both_stores.resolve_slug(jane_doe.rid) == "jane-doe"

    def test_rid_for_accepts_a_slug_or_a_rid(self, both_stores, jane_doe):
        """The mirror of ``resolve_slug``, and both directions of the same map."""
        assert both_stores.rid_for("jane-doe") == jane_doe.rid
        assert both_stores.rid_for(jane_doe.rid) == jane_doe.rid

    def test_rid_for_misses_raise(self, both_stores):
        with pytest.raises(ProfileNotFoundError):
            both_stores.rid_for("nobody")

    def test_get_accepts_a_bare_orcid(self, both_stores, jane_doe):
        """An ORCID rid *is* its ORCID, so no separate index is needed for one."""
        assert jane_doe.orcid is not None
        assert both_stores.get(jane_doe.orcid).slug == "jane-doe"

    def test_get_misses_raise(self, both_stores):
        with pytest.raises(ProfileNotFoundError):
            both_stores.get("not-a-real-slug")

    def test_a_miss_is_both_a_ProfileError_and_a_KeyError(self, both_stores):
        """The API layer's 404 path reads ``except KeyError``; a management host
        catches ``ProfileError``. Neither should need a second clause."""
        for expected in (ProfileNotFoundError, KeyError, ProfileError):
            with pytest.raises(expected):
                both_stores.resolve_slug("nobody")
        assert both_stores.exists("nobody") is False

    def test_get_returns_a_researcher_profile(self, both_stores, jane_doe):
        assert both_stores.get("jane-doe").rid == jane_doe.rid
        assert both_stores.get(jane_doe.rid).name == jane_doe.name

    def test_document_bytes_are_the_published_bytes(self, both_stores, jane_doe_dir):
        assert (
            both_stores.document_bytes("jane-doe") == (jane_doe_dir / "profile.jsonld").read_bytes()
        )

    def test_artifact_bytes(self, both_stores, jane_doe_dir):
        assert (
            both_stores.artifact_bytes("jane-doe", "personality/SOUL.md")
            == (jane_doe_dir / "personality" / "SOUL.md").read_bytes()
        )

    @pytest.mark.parametrize(
        "content_url",
        ["personality/nope.md", "../../etc/passwd"],
        ids=["absent", "traversal"],
    )
    def test_an_unservable_artifact_is_a_miss_not_a_read(self, both_stores, content_url):
        with pytest.raises(ProfileNotFoundError):
            both_stores.artifact_bytes("jane-doe", content_url)

    def test_content_hash_agrees_with_the_profile(self, both_stores):
        prof = both_stores.get("jane-doe")
        assert both_stores.content_hash("jane-doe") == prof.content_hash()
        assert both_stores.content_hash("jane-doe").startswith("sha256:")

    def test_export_directory_round_trips(self, both_stores, jane_doe_dir, tmp_path):
        out = both_stores.export_directory("jane-doe", tmp_path / "exported")
        assert _relative_files(out) == _relative_files(jane_doe_dir)
        for rel in _relative_files(out):
            assert _sha256(out / rel) == _sha256(jane_doe_dir / rel), rel

    def test_hooks_registered_before_a_get_fire(self, both_stores):
        seen = []
        both_stores.add_pre_commit_hook(seen.append)
        both_stores.get("jane-doe").save_soul("# hooked\n")
        assert [c.kind for c in seen] == ["soul"]

    def test_hooks_registered_after_a_get_reach_the_handed_out_profile(self, both_stores):
        """What makes registration order irrelevant. The backends reach it
        differently (an LRU, a weak set), and must not differ in whether."""
        prof = both_stores.get("jane-doe")
        seen = []
        both_stores.add_pre_commit_hook(seen.append)
        prof.save_soul("# hooked late\n")
        assert [c.kind for c in seen] == ["soul"]

    def test_a_raising_hook_aborts_the_write(self, both_stores):
        """Shared: the hook's exception propagates as ``WriteHookError`` and the
        in-memory profile is not updated. Whether the bytes are rolled back is
        where the backends differ; see ``TestStoreDifferences``."""
        both_stores.add_pre_commit_hook(_explode)
        prof = both_stores.get("jane-doe")
        original = prof.soul
        with pytest.raises(WriteHookError):
            prof.save_soul("# must not land\n")
        assert prof.soul == original

    def test_create_then_delete(self, both_stores):
        prof = both_stores.create(
            _new_document("local:new-person-a1b2c3", "New Person"), slug="new-person"
        )
        assert prof.rid == "local:new-person-a1b2c3"
        assert both_stores.list_slugs() == ["jane-doe", "new-person"]
        assert both_stores.get("new-person").name == "New Person"

        assert both_stores.delete("new-person") == "local:new-person-a1b2c3"
        assert both_stores.exists("new-person") is False
        assert both_stores.list_slugs() == ["jane-doe"]

    def test_create_runs_in_one_write_unit_of_kind_create(self, both_stores):
        """What closes the orphan window: a host's ownership row commits WITH
        the profile, not after it."""
        seen = []
        both_stores.add_pre_commit_hook(seen.append)
        both_stores.create(_new_document("local:owned-b2c3d4", "Owned"), slug="owned")
        assert [c.kind for c in seen] == ["create"]
        assert seen[0].rid == "local:owned-b2c3d4"

    @pytest.mark.parametrize(
        "rid, slug",
        [("local:other-c3d4e5", "jane-doe"), ("0000-0002-1825-0097", "somewhere-else")],
        ids=["slug-taken", "rid-taken"],
    )
    def test_create_refuses_a_taken_slug_or_rid(self, both_stores, rid, slug):
        with pytest.raises(ProfileWriteError):
            both_stores.create(_new_document(rid, "Clash"), slug=slug)

    def test_commit_directory_makes_a_staged_tree_live(self, both_stores, tmp_path):
        staging = build_profile_dir(
            tmp_path / "staged", name="Staged Person", rid="local:staged-d4e5f6", summaries=False
        )
        result = both_stores.commit_directory("staged-person", staging)
        assert (result.slug, result.rid, result.name) == (
            "staged-person",
            "local:staged-d4e5f6",
            "Staged Person",
        )
        assert both_stores.get("staged-person").name == "Staged Person"

    def test_commit_directory_rejects_a_tree_that_does_not_load(self, both_stores, tmp_path):
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "profile.jsonld").write_text("{not json", encoding="utf-8")
        with pytest.raises(UploadError):
            both_stores.commit_directory("broken", broken)

    def test_evict_leaves_the_profile_readable(self, both_stores):
        both_stores.evict("jane-doe")
        assert both_stores.get("jane-doe").name


class TestStoreDifferences:
    """Where the backends legitimately differ, pinned so nobody "fixes" it."""

    def test_only_the_filesystem_store_has_a_root(self, fs_store, sql_store):
        assert fs_store.root is not None and fs_store.root.is_dir()
        assert sql_store.root is None

    def test_location_names_the_backing_store(self, fs_store, sql_store):
        assert fs_store.location == str(fs_store.root)
        assert sql_store.location.startswith("sqlite")

    def test_only_the_sql_store_writes_atomically(self, fs_store, sql_store):
        """A hook branches on ``ctx.atomic``, not ``session is None``. This is
        the pair of values that makes the distinction real."""
        seen = []
        for store in (fs_store, sql_store):
            store.add_pre_commit_hook(seen.append)
            store.get("jane-doe").save_soul("# x\n")
        assert [c.atomic for c in seen] == [False, True]
        assert seen[0].session is None and seen[1].session is not None

    def test_only_the_sql_store_rolls_a_failed_write_back(self, fs_store, sql_store):
        """A raising hook aborts on both, but only a transaction can UNDO.

        The filesystem backend's compensating write covers the profile document
        alone (``save_profile`` had read its predecessor anyway); a soul write
        is already on disk by the time a hook runs. That limitation is a
        property of the backend, not a bug, and ``ctx.atomic`` is how a hook is
        told which it is getting.
        """
        before = {s.location: s.content_hash("jane-doe") for s in (fs_store, sql_store)}
        for store in (fs_store, sql_store):
            store.add_pre_commit_hook(_explode)
            with pytest.raises(WriteHookError):
                store.get("jane-doe").save_soul("# must not land\n")
        assert sql_store.content_hash("jane-doe") == before[sql_store.location]
        assert fs_store.content_hash("jane-doe") != before[fs_store.location]

    def test_a_sql_commit_reports_the_staged_index(self, sql_store, tmp_path):
        """A staged ``.cache/embeddings.sqlite`` is stored, so ``True`` is honest.

        The store keeps the staged index as a binary artifact and
        ``export_directory`` restores it, which is what makes ``/match`` work on
        a SQL-backed deployment. Reporting ``False`` here would tell a caller to
        stop looking for a row that is in fact there.
        """
        staging = build_profile_dir(
            tmp_path / "st", rid="local:st-e5f607", summaries=False, index="sqlite"
        )
        result = sql_store.commit_directory("st", staging, build_missing_index=True)
        assert result.indexed is True

    def test_build_store_prefers_a_database_url(self, tmp_path):
        assert isinstance(build_store(profiles_dir=tmp_path), FilesystemProfileStore)
        assert isinstance(build_store(database_url="sqlite://"), SqlProfileStore)
        assert isinstance(
            build_store(database_url="sqlite://", profiles_dir=tmp_path), SqlProfileStore
        )
        with pytest.raises(ValueError):
            build_store()

    def test_build_store_also_reads_the_environment(self, tmp_path, monkeypatch):
        """The composition rule ``create_app`` and ``__main__`` both rely on.

        Neither caller reads ``$RESEARCHER_PROFILES_DATABASE_URL`` /
        ``$RESEARCHER_PROFILES_ROOT`` itself; both just call ``build_store()``
        and let it decide, so they cannot disagree about which store a given
        environment means.
        """
        monkeypatch.setenv(PROFILES_ROOT_ENV_VAR, str(tmp_path))
        assert isinstance(build_store(), FilesystemProfileStore)

        monkeypatch.setenv(DATABASE_URL_ENV_VAR, "sqlite://")
        assert isinstance(build_store(), SqlProfileStore)  # database wins over directory

        # An explicit argument still beats the environment.
        assert isinstance(build_store(profiles_dir=tmp_path, database_url=None), ProfileStore)

    def test_build_store_does_not_guess_a_directory(self, monkeypatch):
        """Unlike :func:`resolve_profiles_root`, no ``DEFAULT_CACHE_DIR`` fallback.

        A server silently pointed at a guessed directory is a worse failure
        than one that refuses to start; nothing here has a "correct" guess the
        way the CLI's local cache does.
        """
        with pytest.raises(ValueError, match="database_url or profiles_dir"):
            build_store()

    def test_build_store_refuses_a_retired_env_var_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RP_DATABASE_URL", "sqlite:///old.db")
        with pytest.raises(RetiredEnvVarError):
            build_store(profiles_dir=tmp_path)


class TestPutDocument:
    """``put_document`` is the JSON create-or-replace primitive, on both backends.

    It writes the canonical document only (identity + expertise), leaving any
    ``sources/``/``.cache/`` artifacts alone, and rides the same
    validate -> stamp -> persist write unit every other write takes so
    ``dateModified`` stays honest and hooks fire. Anything asserted here must
    hold on the filesystem AND the SQL backend or the primitive is not a
    protocol.
    """

    def test_creates_then_replaces(self, both_stores):
        r = both_stores.put_document("cr", _new_document("local:cr-aa11bb", "Create Me"))
        assert both_stores.exists("cr")
        assert r.metadata.name == "Create Me"
        assert both_stores.get("cr").metadata.name == "Create Me"
        h0 = both_stores.content_hash("cr")

        both_stores.put_document("cr", _new_document("local:cr-aa11bb", "Replaced"))
        assert both_stores.get("cr").metadata.name == "Replaced"
        assert both_stores.content_hash("cr") != h0

    def test_stamps_date_modified_only_on_a_content_change(self, both_stores, monkeypatch):
        # Inject the dateModified clock (second-precision) so the test proves
        # advance-on-change without sleeping. Only content changes call it; a
        # no-op carries the previous stamp forward untouched.
        ticks = iter(f"2020-01-01T00:00:{n:02d}+00:00" for n in range(1, 60))
        monkeypatch.setattr(
            "researcher_profiles.utils.date_modified.utc_now_iso", lambda *a, **k: next(ticks)
        )

        both_stores.put_document("dm", _new_document("local:dm-aa11bb", "DM One"))
        dm0 = both_stores.get("dm").metadata.date_modified
        assert dm0  # a create is a content change, so it is stamped

        # No-op re-write with byte-identical content must not advance the stamp.
        both_stores.put_document("dm", _new_document("local:dm-aa11bb", "DM One"))
        assert both_stores.get("dm").metadata.date_modified == dm0

        # A real content change advances it.
        both_stores.put_document("dm", _new_document("local:dm-aa11bb", "DM Two"))
        dm1 = both_stores.get("dm").metadata.date_modified
        assert dm1 != dm0 and dm1 > dm0

    def test_rejects_a_rid_change_on_replace(self, both_stores):
        both_stores.put_document("mm", _new_document("local:mm-aa11bb", "MM"))
        with pytest.raises(ProfileWriteError):
            both_stores.put_document("mm", _new_document("local:mm-cc22dd", "MM"))


class TestCreateAppOverAStore:
    """``create_app`` takes a store, and serves either backend identically."""

    def test_it_refuses_a_path(self, jane_doe_dir):
        from researcher_profiles.api.app import create_app

        with pytest.raises(TypeError, match="FilesystemProfileStore"):
            create_app(str(jane_doe_dir.parent))

    def test_health_names_the_store(self, both_stores, make_api_client):
        body = make_api_client(both_stores).get("/health").json()
        assert body["store"] == both_stores.location
        assert body["profile_count"] == 1

    def test_the_read_surface_is_backend_agnostic(self, both_stores, make_api_client, jane_doe):
        c = make_api_client(both_stores)
        assert [p["slug"] for p in c.get("/api/v1/profiles").json()["profiles"]] == ["jane-doe"]
        assert c.get("/api/v1/profiles/jane-doe").json()["metadata"]["name"] == jane_doe.name
        assert len(c.get("/api/v1/profiles/jane-doe/papers").json()) == len(jane_doe.papers)

    def test_the_document_route_serves_the_published_bytes(
        self, both_stores, make_api_client, jane_doe_dir
    ):
        c = make_api_client(both_stores)
        r = c.get("/api/v1/profiles/jane-doe/profile.jsonld")
        assert r.content == (jane_doe_dir / "profile.jsonld").read_bytes()

    def test_a_write_through_a_route_fires_the_apps_hooks(self, both_stores, make_api_client):
        seen = []
        c = make_api_client(both_stores, pre_commit_hooks=[seen.append])
        assert c.put("/api/v1/profiles/jane-doe/soul", json={"soul": "# via http\n"}).status_code
        assert [x.kind for x in seen] == ["soul"]

    def test_match_returns_the_profile_on_a_sql_store(self, indexed_sql_store, make_api_client):
        """A SQL-backed ``/match`` returns real ranked results, not only a 200.

        The store has no filesystem root and nothing is exported anywhere: the
        ranking runs over ``rp_chunk_vectors`` / ``rp_profile_vectors`` through
        the store's own ``VectorStore`` capability. Before those tables the
        registry had to materialize the whole corpus to a temp directory and
        build an index there on every cold start.
        """
        c = make_api_client(indexed_sql_store)
        r = c.post("/api/v1/match", json={"query": "chromatin accessibility genomics"})
        assert r.status_code == 200, r.text
        matches = r.json()["matches"]
        assert matches, "SQL-backed /match returned no profiles: the vector rows were not read"
        assert any(m["slug"] == "jane-doe" for m in matches)
        assert isinstance(matches[0]["score"], (int, float))
        body = r.json()
        assert body["total_profiles"] >= 1
        assert body["ranked_profiles"] == len(matches)
        # Nothing was exported: the temp dir exists only for the graph now.
        assert getattr(c.app.state, "_registry_tempdir", None) is None

    def test_match_fails_loud_when_the_embedding_backend_is_unavailable(
        self, indexed_sql_store, make_api_client, monkeypatch
    ):
        """A broken query-embedding path must 5xx, never 200 with an empty ranking.

        The store serves its stored vectors without embedding anything, but the
        *query* still has to be embedded, and a serve image that ships no
        encoder cannot do it. That has to arrive as a loud failure: a 200 with
        an empty ranking is indistinguishable from "nobody matched", which is
        the bug this pins.
        """
        from researcher_profiles.embeddings.backends import MissingEmbeddingBackendError

        def _boom(spec=None):
            raise MissingEmbeddingBackendError(
                "sentence-transformers is required to embed the query for this embedding backend."
            )

        for target in (
            "researcher_profiles.analytics.centroids.get_backend",
            "researcher_profiles.embeddings.backends.get_backend",
            "researcher_profiles.embeddings.cache.get_backend",
        ):
            monkeypatch.setattr(target, _boom)

        c = make_api_client(indexed_sql_store)
        r = c.post("/api/v1/match", json={"query": "chromatin accessibility genomics"})
        assert r.status_code >= 500, (
            f"expected a loud failure, got {r.status_code} {r.text!r}. "
            "A 200 here means the embedding failure is silently masquerading "
            "as an empty ranking again"
        )
        assert r.status_code != 200
        detail = r.json()["detail"]
        assert "embedding" in detail.lower()
        assert "sentence-transformers" in detail

    def test_a_write_refreshes_the_sql_match_corpus(self, indexed_sql_store, make_api_client):
        """A push after a /match must be visible without restarting the app.

        There is no snapshot on ``app.state`` to drop any more. The analytics
        cache on the store and stamp what they cached with the store's write
        generation, which the write itself moved, so the next ``/match``
        rebuilds over the live rows with nobody invalidating anything.
        """
        c = make_api_client(indexed_sql_store)
        assert c.post("/api/v1/match", json={"query": "genomics"}).status_code == 200
        before = indexed_sql_store.generation
        assert indexed_sql_store._rostered().slugs == ["jane-doe"]

        r = c.put(
            "/api/v1/profiles/newbie",
            json={"name": "New Bie", "mintLocalRid": True, "provenance": "synthetic"},
        )
        assert r.status_code == 200, r.text
        assert indexed_sql_store.generation > before
        assert indexed_sql_store._rostered().slugs == ["jane-doe", "newbie"]

        # The next /match ranks over the live corpus.
        r2 = c.post("/api/v1/match", json={"query": "chromatin accessibility genomics"})
        assert r2.status_code == 200
        assert any(m["slug"] == "jane-doe" for m in r2.json()["matches"])


# ---------------------------------------------------------------------------
# One level down: ArtifactStorage
# ---------------------------------------------------------------------------


@pytest.fixture
def file_storage(jane_doe_dir):
    return DirectoryArtifactStorage(jane_doe_dir)


@pytest.fixture
def sql_storage(jane_doe):
    store = SqlProfileStore("sqlite://")
    store.create_all()
    rid = store.put(jane_doe)
    return SqlArtifactStorage(store, rid, slug="jane-doe")


@pytest.fixture
def dict_storage(jane_doe_dir, jane_doe):
    return DictStorage(
        {
            "document": json.loads((jane_doe_dir / "profile.jsonld").read_text()),
            "soul": jane_doe.soul,
            "expertise": jane_doe.expertise,
            "papers": list(jane_doe.papers),
            "grants": list(jane_doe.grants),
        }
    )


@pytest.fixture(params=["file", "sql", "dict"])
def storage(request) -> ArtifactStorage:
    """The same jane-doe profile, once per backend, as a raw storage."""
    return request.getfixturevalue(f"{request.param}_storage")


@pytest.fixture
def stored_profile(storage) -> ResearcherProfile:
    """A profile over each backend, so writes go through the real write unit."""
    return ResearcherProfile(storage)


class TestArtifactStorageContract:
    """One profile's backing, asserted identically on every writable backend.

    The sibling of :class:`TestStoreProtocol` one level down. A backend that
    passes this is interchangeable behind ``ArtifactStorage``; a contract
    asserted on only the filesystem is a wish.
    """

    def test_it_is_a_profile_storage(self, storage):
        assert isinstance(storage, ArtifactStorage)
        assert not type(storage).__abstractmethods__
        assert isinstance(storage.slug, str) and storage.slug
        assert isinstance(storage.key, str) and storage.key

    @pytest.mark.parametrize("paper_id", ["../escape", "a/b", "", "..", "."])
    def test_file_storage_refuses_paper_ids_that_leave_the_summaries_dir(self, tmp_path, paper_id):
        """A paper id becomes a filename only in DirectoryArtifactStorage, so that is where
        an id with a path separator is refused."""
        storage = DirectoryArtifactStorage(build_profile_dir(tmp_path))
        with pytest.raises(ProfileWriteError):
            storage.save_summary(paper_id, "body")
        with pytest.raises(ProfileWriteError):
            storage.delete_summary(paper_id)

    def test_the_eight_artifacts_round_trip(self, stored_profile):
        prof = stored_profile
        prof.save_expertise("# Expertise\nround trip\n")
        prof.save_soul("# SOUL\nround trip\n")
        prof.save_papers([PaperRecord(paper_id="rt:1", title="Round Trip")])
        prof.save_grants([])
        prof.save_citations({"rt:1": ["rt:0"]})
        prof.save_summary("rt:1", "A summary body.\n")
        prof.save_build_state()
        doc = prof.save_profile()

        storage = prof.storage
        assert storage.load_expertise() == "# Expertise\nround trip\n"
        assert storage.load_soul() == "# SOUL\nround trip\n"
        assert [p.paper_id for p in storage.load_papers()] == ["rt:1"]
        assert storage.load_grants() == []
        assert storage.load_citations() == {"rt:1": ["rt:0"]}
        assert storage.load_summaries()["rt:1"] == "A summary body.\n"
        assert storage.load_document().rid == doc.rid
        assert storage.load_build_state() is not None

    def test_content_hash_changes_on_a_soul_only_write(self, stored_profile):
        """The digest spans document AND soul, so a soul write must move it."""
        before = stored_profile.content_hash()
        stored_profile.save_soul("# A different soul\n")
        assert stored_profile.content_hash() != before

    def test_content_hash_changes_on_a_document_write(self, stored_profile):
        before = stored_profile.content_hash()
        stored_profile.edit.patch_metadata({"field": "Systems Biology"})
        assert stored_profile.content_hash() != before

    def test_persisted_document_is_the_stored_dict(self, stored_profile):
        stored = stored_profile.persisted_document()
        assert stored["name"] == stored_profile.metadata.name

    def test_save_citations_none_deletes(self, stored_profile):
        stored_profile.save_citations({"a": ["b"]})
        assert stored_profile.storage.load_citations() == {"a": ["b"]}
        stored_profile.save_citations(None)
        assert stored_profile.storage.load_citations() is None

    def test_delete_summary_is_idempotent(self, stored_profile):
        stored_profile.save_summary("rt:1", "body\n")
        stored_profile.delete_summary("rt:1")
        stored_profile.delete_summary("rt:1")
        assert "rt:1" not in stored_profile.storage.load_summaries()

    def test_build_manifest_answers_without_a_directory_walk(self, stored_profile):
        parts, subjects = stored_profile.storage.build_manifest()
        assert isinstance(parts, list) and isinstance(subjects, list)


class TestVerbatimArtifactBodies:
    """A store that normalizes line endings is not holding what it was given.

    Three papers in the reference corpus carry a lone ``\\r`` inside extracted
    PDF text, and ``read_text()`` silently rewrites it. Both backends that can
    hold an arbitrary body are asserted on the same bytes.
    """

    LONE_CR = "first line\rsecond line\r\nthird line\n"

    def test_a_lone_carriage_return_survives(self, jane_doe_dir):
        target = jane_doe_dir / "sources" / "summaries" / "cr-test.summary.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.LONE_CR.encode("utf-8"))

        file_storage = DirectoryArtifactStorage(jane_doe_dir)
        assert file_storage.artifact_text("sources/summaries/cr-test.summary.md") == self.LONE_CR

        # The manifest is what an ingest walks, so regenerate it first: the
        # new body has to be an artifact before the store can hold its bytes.
        ResearcherProfile.from_files(jane_doe_dir).build_manifest(write=True)
        store = SqlProfileStore("sqlite://")
        store.create_all()
        rid = store.put(ResearcherProfile.from_files(jane_doe_dir))
        sql_storage = SqlArtifactStorage(store, rid, slug="jane-doe")
        assert sql_storage.artifact_text("sources/summaries/cr-test.summary.md") == self.LONE_CR


class TestIdentityLookup:
    """``path_for`` and the ``rid <-> slug`` file, which are the store's job.

    Going from a rid to a directory requires an index. That is the price of
    separating identity from the directory name, and it is paid here (and, for
    non-Python callers, in ``<root>/.cache/index.json``).
    """

    def test_path_for_accepts_a_slug_or_a_rid(self, fs_store, jane_doe):
        assert fs_store.path_for("jane-doe") == fs_store.root / "jane-doe"
        assert fs_store.path_for(jane_doe.rid) == fs_store.root / "jane-doe"

    def test_path_for_a_miss_raises(self, fs_store):
        with pytest.raises(ProfileNotFoundError):
            fs_store.path_for("nobody")

    def test_write_lookup_index_writes_both_directions(self, fs_store, jane_doe):
        path = fs_store.write_lookup_index()
        assert path == fs_store.root / ".cache" / "index.json"
        payload = json.loads(path.read_text())
        assert payload["by_rid"] == {jane_doe.rid: "jane-doe"}
        assert payload["by_slug"] == {"jane-doe": jane_doe.rid}

    def test_write_lookup_index_does_not_churn_the_mtime(self, fs_store):
        path = fs_store.write_lookup_index()
        before = path.stat().st_mtime_ns
        assert fs_store.write_lookup_index() == path
        assert path.stat().st_mtime_ns == before

    def test_a_rootless_store_has_no_lookup_file(self, sql_store):
        """Not a degradation: ``rid_for`` is one indexed query and always current."""
        assert sql_store.write_lookup_index() is None


class TestDuplicateIdentity:
    """Two directories claiming one rid is a corrupt root, not a near miss.

    Only the filesystem backend can reach it: SQL keys on the rid, and a
    published ``by-rid.json`` is a mapping. Silently keeping the first would
    make which profile a rid resolves to depend on directory ordering.
    """

    @pytest.fixture
    def duplicated_root(self, fixture_profiles_root, jane_doe):
        root = fixture_profiles_root("jane-doe")
        twin = root / "jane-doe-again"
        twin.mkdir()
        (twin / "profile.jsonld").write_text(
            (root / "jane-doe" / "profile.jsonld").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return root

    def test_a_rid_lookup_raises(self, duplicated_root, jane_doe):
        store = FilesystemProfileStore(duplicated_root)
        with pytest.raises(DuplicateIdentityError, match=jane_doe.rid):
            store.get(jane_doe.rid)

    def test_writing_the_lookup_index_raises(self, duplicated_root):
        store = FilesystemProfileStore(duplicated_root)
        with pytest.raises(DuplicateIdentityError):
            store.write_lookup_index()


class TestStaleRegistryIndex:
    """``<root>/.cache/index.json`` is a cache, and caches go stale.

    The filesystem store prefers it for rid lookups. Before the registry
    stopped writing it on every load, a stale copy was hidden by the accident
    that something usually rebuilt it first. Now nothing does, so the store has
    to notice staleness itself.
    """

    def test_a_stale_index_does_not_hide_a_new_profile(self, fixture_profiles_root, jane_doe):
        root = fixture_profiles_root("jane-doe", "john-smith")
        john = ResearcherProfile.from_files(root / "john-smith")
        # An index naming only jane-doe: exactly what a registry written before
        # john-smith arrived would leave behind.
        reg_dir = root / ".cache"
        reg_dir.mkdir(exist_ok=True)
        (reg_dir / "index.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "root": str(root),
                    "by_rid": {jane_doe.rid: "jane-doe"},
                    "by_slug": {"jane-doe": jane_doe.rid},
                }
            ),
            encoding="utf-8",
        )
        store = FilesystemProfileStore(root)
        assert store.resolve_slug(john.rid) == "john-smith"
        assert store.resolve_slug(jane_doe.rid) == "jane-doe"

    def test_a_fresh_index_is_still_used(self, fixture_profiles_root, jane_doe):
        root = fixture_profiles_root("jane-doe")
        reg_dir = root / ".cache"
        reg_dir.mkdir(exist_ok=True)
        (reg_dir / "index.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "root": str(root),
                    "by_rid": {jane_doe.rid: "jane-doe"},
                    "by_slug": {"jane-doe": jane_doe.rid},
                }
            ),
            encoding="utf-8",
        )
        store = FilesystemProfileStore(root)
        assert store._rid_map() == {jane_doe.rid: "jane-doe"}


class TestSqlVectorsAtQueryTime:
    """Where the backend-mismatch guard lives now that vectors are rows.

    It used to live in ``materialize_store_to_tempdir``, which exported every
    profile to a temp directory on every cold start, read the stamped backend
    name off the restored ``.cache/embeddings.sqlite``, and rebuilt the index
    when it disagreed with the deployment's encoder. That whole apparatus
    existed because the SQL store could not answer a vector query.

    It can now. The stored rows carry their own ``backend_spec``, ranking
    reads it off the store to pick a query encoder, and a mismatch is refused
    rather than silently ranked. These pin that, and pin that the materializer
    has stopped indexing anything.
    """

    def test_ranking_takes_its_query_encoder_from_the_stored_rows(self, indexed_sql_store):
        """The query encoder is the store's ``backend_spec``, which is one column."""
        assert indexed_sql_store.backend_spec == "fake:tiny"
        assert indexed_sql_store.centroids.backend.name == "fake:tiny"

    def test_a_query_from_another_model_is_refused_not_ranked(self, indexed_sql_store):
        """Cosine across embedding models is meaningless (spec section 7).

        The stale-index failure mode this replaces was silent: an ``st:``-built
        index served to a deployment embedding with something else produced
        numbers, and numbers sort. Refusing is the only safe answer, and the
        row's ``backend_spec`` is what makes it detectable.
        """
        from .factories import FakeBackend

        idx = indexed_sql_store.vector_index("jane-doe")
        with pytest.raises(ValueError, match="refusing cross-model similarity"):
            idx.search_text("genomics", FakeBackend(name="st:all-MiniLM-L6-v2"))

    def test_materialization_only_exports_now(self, indexed_sql_store, monkeypatch):
        """The temp directory is the graph's, and the graph needs no embeddings.

        A ``build_index`` call from here would be pure waste: it would re-embed
        a corpus whose vectors the store already holds, on every cold start of
        a feature that never looks at them.
        """
        import types

        import researcher_profiles.embeddings as emb
        from researcher_profiles.api.deps import materialize_store_to_tempdir

        def _never(*a, **k):
            raise AssertionError("materialization must not build an embedding index")

        monkeypatch.setattr(emb, "build_index", _never)
        request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
        root = materialize_store_to_tempdir(request, indexed_sql_store)
        assert (Path(root) / "jane-doe" / "profile.jsonld").is_file()
        assert not (Path(root) / "jane-doe" / ".cache" / "embeddings.sqlite").exists()
        # The flat form travels instead, so the export is still rankable.
        assert (Path(root) / "jane-doe" / "embeddings" / "index.json").is_file()

    def test_the_temp_dir_is_reused_across_calls(self, indexed_sql_store):
        import types

        from researcher_profiles.api.deps import materialize_store_to_tempdir

        request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace()))
        first = materialize_store_to_tempdir(request, indexed_sql_store)
        assert materialize_store_to_tempdir(request, indexed_sql_store) == first
