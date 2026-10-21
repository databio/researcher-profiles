"""The ``ProfileStore`` protocol and the two backends against it, through ``both_stores``.
A contract asserted on only one backend is a wish. test_db.py owns what is specific to SQL;
this file owns what the two must share.
"""

import hashlib
import json
from pathlib import Path

import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.env import RetiredEnvVarError
from researcher_profiles.errors import ProfileError, ProfileWriteError, WriteHookError
from researcher_profiles.profile.storage import ArtifactStorage, DirectoryArtifactStorage
from researcher_profiles.schema import ArtifactRef, PaperRecord, ProfileDocument, TrialRecord
from researcher_profiles.store import (
    DuplicateIdentityError,
    FilesystemProfileStore,
    ProfileNotFoundError,
    ProfileStore,
    UploadError,
    build_store,
)
from researcher_profiles.store.config import DATABASE_URL_ENV_VAR, PROFILES_ROOT_ENV_VAR
from researcher_profiles.store.db import content_hash_for
from researcher_profiles.store.sql import SqlArtifactStorage, SqlProfileStore

from .factories import ADA, FakeBackend, build_profile_dir, tar_gz_of_dir
from .test_profile import DictStorage


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


def _assert_proof_stripped(store, slug: str) -> None:
    stored = json.loads(store.document_bytes(slug))
    kinds = [p["kind"] for p in stored.get("proof", [])]
    assert "orcid_login" not in kinds
    assert "orcid_roundtrip" in kinds
    soul = store.get(slug).soul or ""
    assert store.content_hash(slug) == content_hash_for(stored, soul)


@pytest.fixture
def fs_store(fixture_profiles_root) -> FilesystemProfileStore:
    return FilesystemProfileStore(fixture_profiles_root("jane-doe"))


@pytest.fixture
def sql_store(empty_sql_store, jane_doe) -> SqlProfileStore:
    empty_sql_store.put(jane_doe)
    return empty_sql_store


@pytest.fixture
def indexed_sql_store(empty_sql_store, jane_doe_dir, fake_backend) -> SqlProfileStore:
    """A SQL store whose profile carries vectors: ingest is where they enter, the store never embeds."""
    from researcher_profiles.embeddings.cache import SqliteEmbeddingIndex

    SqliteEmbeddingIndex(jane_doe_dir).build_index(backend=fake_backend)
    empty_sql_store.put(ResearcherProfile.from_files(jane_doe_dir))
    return empty_sql_store


@pytest.fixture(params=["filesystem", "sql"])
def both_stores(request) -> ProfileStore:
    """The same one-profile store, once per backend."""
    return request.getfixturevalue("fs_store" if request.param == "filesystem" else "sql_store")


class TestStoreProtocol:
    """Everything here must hold on both backends or the protocol is a wish."""

    def test_lists_and_evicts(self, both_stores, jane_doe):
        assert isinstance(both_stores, ProfileStore)
        assert both_stores.list_slugs() == ["jane-doe"]
        both_stores.evict("jane-doe")
        assert both_stores.get("jane-doe").name == jane_doe.name

    @pytest.mark.parametrize("ref_attr", ["slug", "rid", "orcid"])
    def test_any_handle_finds_the_profile(self, both_stores, jane_doe, ref_attr):
        """An ORCID rid *is* its ORCID, so a bare ORCID needs no separate index."""
        ref = "jane-doe" if ref_attr == "slug" else getattr(jane_doe, ref_attr)
        assert both_stores.resolve_slug(ref) == "jane-doe"
        assert both_stores.rid_for(ref) == jane_doe.rid
        assert both_stores.get(ref).name == jane_doe.name

    @pytest.mark.parametrize("method", ["resolve_slug", "rid_for", "get"])
    def test_a_miss_raises(self, both_stores, method):
        """KeyError for the API 404 path, ProfileError for hosts: one clause each."""
        with pytest.raises(ProfileNotFoundError) as e:
            getattr(both_stores, method)("nobody")
        assert isinstance(e.value, KeyError) and isinstance(e.value, ProfileError)
        assert both_stores.exists("nobody") is False

    @pytest.mark.parametrize(
        "read, path",
        [
            (lambda s: s.document_bytes("jane-doe"), "profile.jsonld"),
            (lambda s: s.artifact_bytes("jane-doe", "personality/SOUL.md"), "personality/SOUL.md"),
        ],
        ids=["document", "soul"],
    )
    def test_served_bytes_are_the_published_bytes(self, both_stores, jane_doe_dir, read, path):
        assert read(both_stores) == (jane_doe_dir / path).read_bytes()

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

    @pytest.mark.parametrize("hook_first", [True, False])
    def test_hooks_fire_whatever_the_registration_order(self, both_stores, hook_first):
        """The backends reach a handed-out profile differently (LRU, weak set); order must not matter."""
        seen = []
        if hook_first:
            both_stores.add_pre_commit_hook(seen.append)
        prof = both_stores.get("jane-doe")
        if not hook_first:
            both_stores.add_pre_commit_hook(seen.append)
        prof.save_soul("# hooked\n")
        assert [c.kind for c in seen] == ["soul"]

    def test_a_raising_hook_aborts_the_write(self, both_stores):
        """The hook's error is ``WriteHookError`` and the in-memory profile is unchanged (rollback: TestStoreDifferences)."""
        both_stores.add_pre_commit_hook(_explode)
        prof = both_stores.get("jane-doe")
        original = prof.soul
        with pytest.raises(WriteHookError):
            prof.save_soul("# must not land\n")
        assert prof.soul == original

    def test_create_then_delete(self, both_stores):
        """A create is one write unit of kind ``create``, so a host's ownership row commits WITH the profile."""
        seen = []
        both_stores.add_pre_commit_hook(seen.append)
        prof = both_stores.create(
            _new_document("local:new-person-a1b2c3", "New Person"), slug="new-person"
        )
        assert [c.kind for c in seen] == ["create"]
        assert seen[0].rid == prof.rid == "local:new-person-a1b2c3"
        assert both_stores.list_slugs() == ["jane-doe", "new-person"]
        assert both_stores.get("new-person").name == "New Person"

        assert both_stores.delete("new-person") == "local:new-person-a1b2c3"
        assert both_stores.exists("new-person") is False
        assert both_stores.list_slugs() == ["jane-doe"]

    @pytest.mark.parametrize(
        "rid, slug",
        [("local:other-c3d4e5", "jane-doe"), ("0000-0002-1825-0097", "somewhere-else")],
        ids=["slug-taken", "rid-taken"],
    )
    def test_create_refuses_a_taken_slug_or_rid(self, both_stores, rid, slug):
        with pytest.raises(ProfileWriteError):
            both_stores.create(_new_document(rid, "Clash"), slug=slug)

    def test_create_bundle_installs_every_artifact_in_one_unit(self, both_stores):
        """Document, persona docs and free artifacts land together: no half-assembled profile."""
        doc = _new_document("local:bundled-e5f6a7", "Bundled")
        doc.subject_of = [
            ArtifactRef(
                **{
                    "contentUrl": "personality/clinical_expertise.md",
                    "role": "clinical_expertise",
                    "name": "Clinical expertise",
                    "encodingFormat": "text/markdown",
                }
            )
        ]
        seen = []
        both_stores.add_pre_commit_hook(seen.append)
        prof = both_stores.create_bundle(
            doc,
            slug="bundled",
            expertise="# Expertise\n",
            soul="# SOUL\n",
            artifacts={"personality/clinical_expertise.md": "# Clinical\n"},
        )
        assert prof.rid == "local:bundled-e5f6a7"
        assert [c.kind for c in seen] == ["create"]
        installed = both_stores.get("bundled")
        assert installed.expertise == "# Expertise\n"
        assert installed.clinical_expertise == "# Clinical\n"

    def test_create_bundle_refuses_an_undeclared_artifact(self, both_stores):
        """An artifact nothing declares is one no consumer can find."""
        with pytest.raises(ProfileWriteError):
            both_stores.create_bundle(
                _new_document("local:undeclared-f6a7b8", "Undeclared"),
                slug="undeclared",
                artifacts={"personality/clinical_expertise.md": "# Clinical\n"},
            )
        assert both_stores.exists("undeclared") is False

    def test_trials_round_trip_through_the_storage_seam(self, both_stores):
        prof = both_stores.create(_new_document("local:clinical-a7b8c9", "Clinical"), slug="clin")
        prof.save_trials([TrialRecord(nct_id="NCT01234567", name="A Phase 2 Study", enrollment=40)])
        both_stores.evict("clin")
        reloaded = both_stores.get("clin").trials
        assert [(t.nct_id, t.enrollment) for t in reloaded] == [("NCT01234567", 40)]

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


class TestStoreDifferences:
    """Where the backends legitimately differ, pinned so nobody "fixes" it."""

    def test_root_and_location(self, fs_store, sql_store):
        assert fs_store.root is not None and fs_store.root.is_dir()
        assert sql_store.root is None
        assert fs_store.location == str(fs_store.root)
        assert sql_store.location.startswith("sqlite")

    def test_a_missing_root_is_an_empty_store(self, tmp_path):
        store = FilesystemProfileStore(tmp_path / "not-yet")
        assert store.list_slugs() == []
        assert store._root_stamp() == (0, 0)
        with pytest.raises(ProfileNotFoundError):
            store.resolve_slug("nobody")

    def test_only_the_sql_store_writes_atomically(self, fs_store, sql_store):
        """A hook branches on ``ctx.atomic``, not ``session is None``."""
        seen = []
        for store in (fs_store, sql_store):
            store.add_pre_commit_hook(seen.append)
            store.get("jane-doe").save_soul("# x\n")
        assert [c.atomic for c in seen] == [False, True]
        assert seen[0].session is None and seen[1].session is not None

    def test_only_the_sql_store_rolls_a_failed_write_back(self, fs_store, sql_store):
        """A raising hook aborts on both, but only a transaction can UNDO; ``ctx.atomic`` says which."""
        before = {s.location: s.content_hash("jane-doe") for s in (fs_store, sql_store)}
        original_sql_soul = sql_store.get("jane-doe").soul
        for store in (fs_store, sql_store):
            store.add_pre_commit_hook(_explode)
            with pytest.raises(WriteHookError):
                store.get("jane-doe").edit.set_soul("# must not land\n")
        assert sql_store.content_hash("jane-doe") == before[sql_store.location]
        assert sql_store.get("jane-doe").soul == original_sql_soul
        assert fs_store.content_hash("jane-doe") != before[fs_store.location]

    def test_a_sql_commit_reports_the_staged_index(self, sql_store, tmp_path):
        """The staged index is stored (and restored on export), so ``indexed=True`` is honest."""
        staging = build_profile_dir(
            tmp_path / "st", rid="local:st-e5f607", summaries=False, index="sqlite"
        )
        result = sql_store.commit_directory("st", staging, build_missing_index=True)
        assert result.indexed is True

    @pytest.mark.parametrize(
        "kwargs, env, expected",
        [
            ({"profiles_dir": "TMP"}, {}, FilesystemProfileStore),
            ({"database_url": "sqlite://"}, {}, SqlProfileStore),
            ({"database_url": "sqlite://", "profiles_dir": "TMP"}, {}, SqlProfileStore),
            ({}, {PROFILES_ROOT_ENV_VAR: "TMP"}, FilesystemProfileStore),
            (
                {},
                {PROFILES_ROOT_ENV_VAR: "TMP", DATABASE_URL_ENV_VAR: "sqlite://"},
                SqlProfileStore,
            ),
            (
                {
                    "profiles_dir": "TMP",
                    "database_url": None,
                },  # None means "unset": the env URL wins
                {PROFILES_ROOT_ENV_VAR: "TMP", DATABASE_URL_ENV_VAR: "sqlite://"},
                SqlProfileStore,
            ),
        ],
        ids=[
            "dir-arg",
            "url-arg",
            "url-beats-dir",
            "dir-env",
            "url-env-beats-dir-env",
            "none-arg-defers-to-env",
        ],
    )
    def test_build_store_picks(self, tmp_path, monkeypatch, kwargs, env, expected):
        """``create_app`` and ``__main__`` both just call ``build_store()``, so they cannot disagree."""
        monkeypatch.delenv(PROFILES_ROOT_ENV_VAR, raising=False)
        monkeypatch.delenv(DATABASE_URL_ENV_VAR, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, str(tmp_path) if v == "TMP" else v)
        kwargs = {k: str(tmp_path) if v == "TMP" else v for k, v in kwargs.items()}
        assert type(build_store(**kwargs)) is expected

    @pytest.mark.parametrize("retired_env", [False, True], ids=["no-config", "retired-env-var"])
    def test_build_store_refuses_to_guess(self, tmp_path, monkeypatch, retired_env):
        """No ``DEFAULT_CACHE_DIR`` fallback: a server on a guessed directory is worse than one that won't start."""
        if retired_env:
            monkeypatch.setenv("RP_DATABASE_URL", "sqlite:///old.db")
            with pytest.raises(RetiredEnvVarError):
                build_store(profiles_dir=tmp_path)
        else:
            monkeypatch.delenv(PROFILES_ROOT_ENV_VAR, raising=False)
            monkeypatch.delenv(DATABASE_URL_ENV_VAR, raising=False)
            with pytest.raises(ValueError, match="database_url or profiles_dir"):
                build_store()


class TestPutDocument:
    """``put_document`` is the JSON create-or-replace primitive; it must hold on both backends."""

    def test_creates_then_replaces(self, both_stores):
        r = both_stores.put_document("cr", _new_document("local:cr-aa11bb", "Create Me"))
        assert r.metadata.name == "Create Me"
        assert both_stores.get("cr").metadata.name == "Create Me"
        h0 = both_stores.content_hash("cr")

        both_stores.put_document("cr", _new_document("local:cr-aa11bb", "Replaced"))
        assert both_stores.get("cr").metadata.name == "Replaced"
        assert both_stores.content_hash("cr") != h0

    def test_stamps_date_modified_only_on_a_content_change(self, both_stores, monkeypatch):
        # Inject the clock so advance-on-change is proven without sleeping.
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


class TestRegistryProofStrip:
    """No write path stores a registry-issued proof (``orcid_login``); other proof kinds are kept.

    ``content_hash`` covers the stripped document. Push and PATCH enter through HTTP because
    those routes are how an author's document reaches the store.
    """

    FORGED = {
        "kind": "orcid_login",
        "issuer": "https://evil.example",
        "orcid": ADA,
        "verifiedAt": "2026-09-01T12:00:00+00:00",
    }
    ROUNDTRIP = {"kind": "orcid_roundtrip", "issuer": f"https://orcid.org/{ADA}"}
    LOCAL = "local:lee-local-abc123"

    def _doc(self, rid: str = ADA) -> ProfileDocument:
        return ProfileDocument(
            name="Ada Lovelace",
            rid=rid,
            provenance="third_party",
            proof=[self.FORGED, self.ROUNDTRIP] if rid == ADA else [self.ROUNDTRIP],
        )

    def _staged(self, tmp_path) -> Path:
        return build_profile_dir(
            tmp_path / "stage",
            name="Ada Lovelace",
            rid=ADA,
            level="lite",
            papers=False,
            personality=False,
            summaries=False,
            proof=[self.FORGED, self.ROUNDTRIP],
        )

    def test_create_strips_it(self, any_store):
        any_store.create(self._doc(), slug="ada")
        _assert_proof_stripped(any_store, "ada")
        # The in-memory profile matches what was persisted.
        assert [p.kind for p in any_store.get("ada").metadata.proof] == ["orcid_roundtrip"]

    @pytest.mark.parametrize("push", ["tarball", "json"])
    def test_a_push_strips_it(self, any_store, tmp_path, make_api_client, push):
        if push == "tarball":
            kwargs = {"content": tar_gz_of_dir(self._staged(tmp_path))}
        else:
            kwargs = {
                "json": {
                    "name": "Ada Lovelace",
                    "rid": ADA,
                    "provenance": "third_party",
                    "proof": [self.FORGED, self.ROUNDTRIP],
                }
            }
        r = make_api_client(any_store).put("/api/v1/profiles/ada", **kwargs)
        assert r.status_code == 200, r.text
        _assert_proof_stripped(any_store, "ada")

    def test_a_metadata_patch_cannot_store_it(self, any_store, make_api_client):
        any_store.create(self._doc(), slug="ada")
        c = make_api_client(any_store)
        r = c.patch("/api/v1/profiles/ada/metadata", json={"proof": [self.FORGED]})
        # ``proof`` is not an owner-editable field; whatever the answer, nothing is stored.
        assert r.status_code >= 400
        r = c.patch("/api/v1/profiles/ada/metadata", json={"summary": "An edit."})
        assert r.status_code == 200, r.text
        _assert_proof_stripped(any_store, "ada")

    def test_merge_into_strips_it(self, empty_sql_store, tmp_path):
        store = empty_sql_store
        store.create(self._doc(self.LOCAL), slug="lee")
        store.merge_into(
            self.LOCAL,
            self._staged(tmp_path),
            survivor_rid=ADA,
            survivor_slug="lee",
            build_missing_index=False,
        )
        _assert_proof_stripped(store, "lee")

    def test_other_proof_kinds_are_kept_verbatim(self, any_store):
        any_store.create(self._doc(self.LOCAL), slug="lee")
        stored = json.loads(any_store.document_bytes("lee"))
        assert stored["proof"] == [self.ROUNDTRIP]


class TestCreateAppOverAStore:
    """``create_app`` takes a store, and serves either backend identically."""

    def test_it_refuses_a_path(self, jane_doe_dir):
        from researcher_profiles.api.app import create_app

        with pytest.raises(TypeError, match="FilesystemProfileStore"):
            create_app(str(jane_doe_dir.parent))

    def test_the_read_surface_is_backend_agnostic(self, both_stores, make_api_client, jane_doe):
        c = make_api_client(both_stores)
        body = c.get("/health").json()
        assert body["store"] == both_stores.location
        assert body["profile_count"] == 1
        assert [p["slug"] for p in c.get("/api/v1/profiles").json()["profiles"]] == ["jane-doe"]
        assert c.get("/api/v1/profiles/jane-doe").json()["fields"]["name"] == jane_doe.name
        rows = c.get("/api/v1/profiles/jane-doe/papers").json()["items"]
        assert len(rows) == len(jane_doe.papers)
        # Per-caller sizes come out the same from either backend.
        assert sum(r["summary"]["available"] for r in rows) == 5
        assert all(r["summary"]["bytes"] for r in rows if r["summary"]["available"])

    def test_the_document_route_serves_the_published_bytes(
        self, both_stores, make_api_client, jane_doe_dir
    ):
        """The published JSON as written, plus a derived ``accessRights`` on each manifest entry."""
        c = make_api_client(both_stores)
        served = c.get("/api/v1/profiles/jane-doe/profile.jsonld").json()
        parts = served["hasPart"] + served.get("subjectOf", [])
        assert parts and all("accessRights" in p for p in parts)
        for p in parts:
            del p["accessRights"]
        assert served == json.loads((jane_doe_dir / "profile.jsonld").read_text(encoding="utf-8"))

    def test_a_write_through_a_route_fires_the_apps_hooks(self, both_stores, make_api_client):
        seen = []
        c = make_api_client(both_stores, pre_commit_hooks=[seen.append])
        assert c.patch(
            "/api/v1/profiles/jane-doe/metadata", json={"soul": "# via http\n"}
        ).status_code
        assert [x.kind for x in seen] == ["soul"]

    def test_match_fails_loud_when_the_embedding_backend_is_unavailable(
        self, indexed_sql_store, make_api_client, monkeypatch
    ):
        """A broken query encoder must 5xx: a 200 with an empty ranking looks like "nobody matched"."""
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
        assert r.status_code >= 500, f"expected a loud failure, got {r.status_code} {r.text!r}"
        detail = r.json()["detail"]
        assert "embedding" in detail.lower()
        assert "sentence-transformers" in detail

    def test_sql_match_ranks_stored_vectors_and_sees_writes(
        self, indexed_sql_store, make_api_client
    ):
        """SQL ``/match`` ranks stored vectors with no export, and a later push shows up without a restart.

        The store's analytics cache is stamped with its write generation, which a write moves.
        """
        c = make_api_client(indexed_sql_store)
        r = c.post("/api/v1/match", json={"query": "chromatin accessibility genomics"})
        assert r.status_code == 200, r.text
        body = r.json()
        matches = body["matches"]
        assert any(m["slug"] == "jane-doe" for m in matches), "the vector rows were not read"
        assert body["total_profiles"] >= 1
        assert body["ranked_profiles"] == len(matches)
        assert c.app.state.service.registry_tempdir is None  # nothing was exported

        before = indexed_sql_store.generation
        assert indexed_sql_store._rostered().slugs == ["jane-doe"]
        r = c.put(
            "/api/v1/profiles/newbie",
            json={"name": "New Bie", "mintLocalRid": True, "provenance": "synthetic"},
        )
        assert r.status_code == 200, r.text
        assert indexed_sql_store.generation > before
        assert indexed_sql_store._rostered().slugs == ["jane-doe", "newbie"]

        r2 = c.post("/api/v1/match", json={"query": "chromatin accessibility genomics"})
        assert r2.status_code == 200
        assert any(m["slug"] == "jane-doe" for m in r2.json()["matches"])


@pytest.fixture
def file_storage(jane_doe_dir):
    return DirectoryArtifactStorage(jane_doe_dir)


@pytest.fixture
def sql_storage(empty_sql_store, jane_doe):
    rid = empty_sql_store.put(jane_doe)
    return SqlArtifactStorage(empty_sql_store, rid, slug="jane-doe")


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
    """One profile's backing, asserted identically on every writable backend (one level below the store)."""

    def test_it_is_a_profile_storage(self, storage):
        assert isinstance(storage, ArtifactStorage)
        assert not type(storage).__abstractmethods__
        assert storage.slug and storage.key

    @pytest.mark.parametrize("paper_id", ["../escape", "a/b", "", "..", "."])
    def test_file_storage_refuses_paper_ids_that_leave_the_summaries_dir(self, tmp_path, paper_id):
        """A paper id becomes a filename only here, so this is where a path separator is refused."""
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
        assert prof.persisted_document()["name"] == prof.metadata.name
        parts, subjects = storage.build_manifest()
        assert isinstance(parts, list) and isinstance(subjects, list)

    @pytest.mark.parametrize(
        "write",
        [
            lambda p: p.save_soul("# A different soul\n"),
            lambda p: p.edit.patch_metadata({"field": "Systems Biology"}),
        ],
        ids=["soul", "document"],
    )
    def test_content_hash_changes_on_a_write(self, stored_profile, write):
        """The digest spans document AND soul, so either write must move it."""
        before = stored_profile.content_hash()
        write(stored_profile)
        assert stored_profile.content_hash() != before

    def test_deletes_are_idempotent(self, stored_profile):
        p = stored_profile
        p.save_citations({"a": ["b"]})
        assert p.storage.load_citations() == {"a": ["b"]}
        p.save_citations(None)
        p.save_citations(None)
        assert p.storage.load_citations() is None

        p.save_summary("rt:1", "body\n")
        p.delete_summary("rt:1")
        p.delete_summary("rt:1")
        assert "rt:1" not in p.storage.load_summaries()


class TestVerbatimArtifactBodies:
    """A store that normalizes line endings is not holding what it was given (``read_text()`` rewrites a lone ``\\r``)."""

    LONE_CR = "first line\rsecond line\r\nthird line\n"

    def test_a_lone_carriage_return_survives(self, empty_sql_store, jane_doe_dir, tmp_path):
        rel = "sources/summaries/cr-test.summary.md"
        target = jane_doe_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.LONE_CR.encode("utf-8"))

        file_storage = DirectoryArtifactStorage(jane_doe_dir)
        assert file_storage.artifact_text(rel) == self.LONE_CR

        # The manifest is what an ingest walks, so regenerate it before the store can hold the body.
        ResearcherProfile.from_files(jane_doe_dir).build_manifest(write=True)
        rid = empty_sql_store.put(ResearcherProfile.from_files(jane_doe_dir))
        sql_storage = SqlArtifactStorage(empty_sql_store, rid, slug="jane-doe")
        assert sql_storage.artifact_text(rel) == self.LONE_CR
        assert empty_sql_store.artifact_bytes(rid, rel) == self.LONE_CR.encode("utf-8")
        out = empty_sql_store.export_directory(rid, tmp_path / "out")
        assert (out / rel).read_bytes() == self.LONE_CR.encode("utf-8")


class TestIdentityLookup:
    """``path_for`` and the ``rid <-> slug`` file: the price of separating identity from the directory name."""

    @pytest.mark.parametrize("ref", ["jane-doe", "RID", "nobody"], ids=["slug", "rid", "miss"])
    def test_path_for(self, fs_store, jane_doe, ref):
        ref = jane_doe.rid if ref == "RID" else ref
        if ref == "nobody":
            with pytest.raises(ProfileNotFoundError):
                fs_store.path_for(ref)
        else:
            assert fs_store.path_for(ref) == fs_store.root / "jane-doe"

    def test_write_lookup_index_writes_both_directions_without_churn(self, fs_store, jane_doe):
        path = fs_store.write_lookup_index()
        assert path == fs_store.root / ".cache" / "index.json"
        payload = json.loads(path.read_text())
        assert payload["by_rid"] == {jane_doe.rid: "jane-doe"}
        assert payload["by_slug"] == {"jane-doe": jane_doe.rid}

        before = path.stat().st_mtime_ns
        assert fs_store.write_lookup_index() == path
        assert path.stat().st_mtime_ns == before

    def test_a_rootless_store_has_no_lookup_file(self, sql_store):
        """Not a degradation: ``rid_for`` is one indexed query and always current."""
        assert sql_store.write_lookup_index() is None


class TestDuplicateIdentity:
    """Two directories claiming one rid is a corrupt root, and only the filesystem can reach it.

    Silently keeping the first would make a rid's profile depend on directory ordering.
    """

    @pytest.fixture
    def duplicated_root(self, fixture_profiles_root):
        root = fixture_profiles_root("jane-doe")
        twin = root / "jane-doe-again"
        twin.mkdir()
        (twin / "profile.jsonld").write_text(
            (root / "jane-doe" / "profile.jsonld").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return root

    @pytest.mark.parametrize(
        "op, match",
        [
            (lambda s, rid: s.get(rid), "RID"),
            (lambda s, rid: s.write_lookup_index(), None),
        ],
        ids=["get", "write_lookup_index"],
    )
    def test_a_duplicate_rid_raises(self, duplicated_root, jane_doe, op, match):
        store = FilesystemProfileStore(duplicated_root)
        with pytest.raises(DuplicateIdentityError, match=jane_doe.rid if match else None):
            op(store, jane_doe.rid)


def _write_index(root: Path, by_rid: dict[str, str]) -> None:
    (root / ".cache").mkdir(exist_ok=True)
    (root / ".cache" / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "root": str(root),
                "by_rid": by_rid,
                "by_slug": {slug: rid for rid, slug in by_rid.items()},
            }
        ),
        encoding="utf-8",
    )


class TestStaleRegistryIndex:
    """``<root>/.cache/index.json`` is a cache the filesystem store prefers, so it must notice staleness itself."""

    def test_a_stale_index_does_not_hide_a_new_profile(self, fixture_profiles_root, jane_doe):
        root = fixture_profiles_root("jane-doe", "john-smith")
        john = ResearcherProfile.from_files(root / "john-smith")
        _write_index(root, {jane_doe.rid: "jane-doe"})  # as left before john-smith arrived
        store = FilesystemProfileStore(root)
        assert store.resolve_slug(john.rid) == "john-smith"
        assert store.resolve_slug(jane_doe.rid) == "jane-doe"

    def test_a_fresh_index_is_still_used(self, fixture_profiles_root, jane_doe):
        root = fixture_profiles_root("jane-doe")
        _write_index(root, {jane_doe.rid: "jane-doe"})
        store = FilesystemProfileStore(root)
        assert store._rid_map() == {jane_doe.rid: "jane-doe"}


class TestSqlVectorsAtQueryTime:
    """Vectors are rows: ranking reads the query encoder off the store and refuses a mismatch."""

    def test_query_encoder_comes_from_the_rows_and_mismatch_is_refused(self, indexed_sql_store):
        """Cosine across embedding models is meaningless, and numbers sort silently, so refuse."""
        assert indexed_sql_store.backend_spec == "fake:tiny"
        assert indexed_sql_store.centroids.backend.name == "fake:tiny"

        idx = indexed_sql_store.vector_index("jane-doe")
        with pytest.raises(ValueError, match="refusing cross-model similarity"):
            idx.search_text("genomics", FakeBackend(name="st:all-MiniLM-L6-v2"))

    def test_materialization_only_exports_and_reuses_its_temp_dir(
        self, indexed_sql_store, monkeypatch
    ):
        """The temp dir is the graph's, which needs no embeddings: re-embedding here would be waste."""
        import types

        import researcher_profiles.embeddings as emb
        from researcher_profiles.api.deps import materialize_store_to_tempdir
        from researcher_profiles.api.service import Service

        def _never(*a, **k):
            raise AssertionError("materialization must not build an embedding index")

        monkeypatch.setattr(emb, "build_index", _never)
        state = types.SimpleNamespace(service=Service(indexed_sql_store))
        request = types.SimpleNamespace(app=types.SimpleNamespace(state=state))
        root = materialize_store_to_tempdir(request, indexed_sql_store)
        assert (Path(root) / "jane-doe" / "profile.jsonld").is_file()
        assert not (Path(root) / "jane-doe" / ".cache" / "embeddings.sqlite").exists()
        # The flat form travels instead, so the export is still rankable.
        assert (Path(root) / "jane-doe" / "embeddings" / "index.json").is_file()
        assert materialize_store_to_tempdir(request, indexed_sql_store) == root


class TestSeedPapers:
    """The interest seed list: a private-by-default collection, never authored works."""

    SEEDS = [
        {"title": "Seed one", "openalex_id": "W101", "abstract": "About chromatin."},
        {"title": "Seed two", "doi": "10.1093/seed.two"},
    ]

    def _seeded(self, store):
        from researcher_profiles.schema import SeedPaperRecord

        store.create(ProfileDocument(name="Ada", rid=ADA, provenance="third_party"), slug="ada")
        prof = store.get("ada")
        prof.save_papers([PaperRecord(paper_id="mine2020", title="Mine", openalex_id="W900")])
        prof.save_seed_papers([SeedPaperRecord.model_validate(s) for s in self.SEEDS])
        return prof

    def test_round_trips_and_exports(self, any_store, tmp_path):
        self._seeded(any_store)
        prof = any_store.get("ada")
        assert [s.seed_id for s in prof.seed_papers] == ["W101", "doi:10.1093/seed.two"]
        assert [p.paper_id for p in prof.papers] == ["mine2020"]  # seeds are never works
        entry = next(
            p for p in prof.build_manifest() if p.content_url == "sources/seed_papers.jsonld"
        )
        assert entry.role == "seed_papers"
        assert entry.visibility == "private"
        out = any_store.export_directory("ada", tmp_path / "out")
        doc = json.loads((out / "sources" / "seed_papers.jsonld").read_text())
        assert [e["name"] for e in doc["hasPart"]] == ["Seed one", "Seed two"]

    def test_add_dedupes_against_seeds_and_authored_works_and_caps(self, any_store, monkeypatch):
        from researcher_profiles.profile import researcher_profile as rp_mod
        from researcher_profiles.schema import SeedPaperRecord

        prof = self._seeded(any_store)
        monkeypatch.setattr(rp_mod, "SEED_PAPERS_MAX", 3)
        incoming = [
            SeedPaperRecord.model_validate(r)
            for r in (
                {"title": "Again", "doi": "10.1093/SEED.TWO"},  # DOI case differs
                {"title": "Mine", "openalex_id": "https://openalex.org/W900"},
                {"title": "New", "openalex_id": "W102"},
                {"title": "Too many", "openalex_id": "W103"},
            )
        ]
        added, skipped = prof.add_seed_papers(incoming)
        assert [s.seed_id for s in added] == ["W102"]
        assert [(s.title, why) for s, why in skipped] == [
            ("Again", "already_seed"),
            ("Mine", "already_authored"),
            ("Too many", "over_cap"),
        ]
        assert len(any_store.get("ada").seed_papers) == 3
        assert prof.remove_seed_paper("W102") is True
        assert prof.remove_seed_paper("W102") is False
        assert [s.seed_id for s in any_store.get("ada").seed_papers] == [
            "W101",
            "doi:10.1093/seed.two",
        ]
