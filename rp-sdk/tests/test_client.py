"""The client package: ``src/researcher_profiles/client/``.

The two read-only storage backends (``from_api`` / ``ApiArtifactStorage``,
``from_url`` / ``StaticArtifactStorage``), the registry verbs and local cache
(``install_profile``, ``seek_profile``, ``list_installed``, ``list_registry``),
and ``push_profile``'s archive choice. Their contract must match a locally loaded
profile.
"""

from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from researcher_profiles import LLMClient, ResearcherProfile, StaticArtifactStorage
from researcher_profiles.client import (
    ApiArtifactStorage,
    _split_profile_url,
    install_profile,
    list_installed,
    list_registry,
    push_profile,
    seek_profile,
)
from researcher_profiles.embeddings.cache import SearchHit
from researcher_profiles.errors import (
    CapabilityUnavailableError,
    ProfileLoadError,
    ProfileWriteError,
)
from researcher_profiles.models.results import PersonaResponse

from .factories import (
    add_fulltext_pdf,
    fake_llm_response,
    remote_from_app,
    tar_names,
)

SLUG = "jane-doe"
BASE = "https://example.org/profiles/jane-doe"


def _local_profile(root: Path) -> ResearcherProfile:
    return ResearcherProfile.from_files(root / SLUG)


def _static_transport(profile_dir: Path, base_url: str):
    """An ``httpx.MockTransport`` serving ``profile_dir`` file-for-file.

    That is exactly what a published profile is: a static directory behind a
    URL prefix.
    """
    root = urlsplit(base_url).path.rstrip("/")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith(root + "/"):
            return httpx.Response(404)
        f = profile_dir / path[len(root) + 1 :]
        if f.is_file():
            return httpx.Response(200, text=f.read_text(encoding="utf-8"))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _captured_push_bytes(src: Path, **kw) -> bytes:
    """Run push_profile against a stub client and return the uploaded body.

    The stub 404s the preflight GET, so the push is a create: nothing on the
    far side to diff against, and nothing it could remove.
    """

    sent: dict[str, bytes] = {}

    class _StubResponse:
        def __init__(self, status_code: int = 200):
            self.status_code = status_code
            self.text = ""

        @staticmethod
        def json() -> dict:
            return {"slug": SLUG, "name": "Jane Doe", "level": "full", "indexed": False}

    class _StubClient:
        def get(self, url):
            return _StubResponse(404)

        def put(self, url, *, content, headers=None):
            sent["body"] = content
            return _StubResponse()

    push_profile("http://testserver", src, client=_StubClient(), **kw)
    return sent["body"]


class TestFromApiClient:
    """``from_api``: the same assertions must pass over HTTP as over files."""

    def test_remote_404_raises_keyerror(self, api_client):
        remote = remote_from_app(api_client.app, slug="does-not-exist")
        with pytest.raises(KeyError):
            _ = remote.metadata

    def test_remote_401_raises_permission_error(self, make_api_client, fixture_profiles_root):
        # reads have no credential gate (a bad token reads as public); a scoped endpoint does
        secured = make_api_client(fixture_profiles_root(SLUG), token="real-token").app
        http = TestClient(secured, headers={"Authorization": "Bearer WRONG"})
        remote = ResearcherProfile(
            ApiArtifactStorage(slug=SLUG, base_url="http://testserver", client=http)
        )
        with pytest.raises(PermissionError):
            _ = remote.index.search("anything")

    @pytest.fixture
    def both_profiles(self, api_client, fixture_profiles_root):
        local = _local_profile(fixture_profiles_root(SLUG))
        remote = remote_from_app(api_client.app)
        yield local, remote
        remote.close()

    @pytest.mark.parametrize(
        "accessor",
        [
            lambda p: p.slug,
            lambda p: p.metadata.rid,
            lambda p: p.name,
            lambda p: p.metadata.name,
            lambda p: p.metadata.affiliation,
            lambda p: [(x.title, x.year) for x in p.papers],
            lambda p: p.expertise,
            lambda p: p.soul,
            # remote lists only summary_available papers; local enumerates disk
            lambda p: set(p.summaries.keys()),
        ],
        ids=[
            "slug",
            "rid",
            "name",
            "papers",
            "metadata.name",
            "metadata.affiliation",
            "expertise",
            "soul",
            "summaries-keys",
        ],
    )
    def test_contract_parity(self, both_profiles, accessor):
        local, remote = both_profiles
        assert accessor(local) == accessor(remote)

    def test_contract_search_isinstance_and_attrs(
        self, api_client, fixture_profiles_root, monkeypatch
    ):
        """Both backends should return objects with the same SearchHit shape."""

        # Stub local search.
        local = _local_profile(fixture_profiles_root(SLUG))
        fake_hits = [
            SearchHit(
                text="t",
                source_type="paper_summary",
                source_id="abc",
                chunk_index=0,
                section=None,
                cosine=0.0,
                score=0.5,
                meta={"year": 2020},
            )
        ]
        local.index.search = MagicMock(return_value=fake_hits)  # type: ignore[assignment]

        # Stub the server's chunk search.
        import researcher_profiles.api._semantic as semantic

        monkeypatch.setattr(semantic, "search_chunks", lambda *a, **kw: (fake_hits, None))

        remote = remote_from_app(api_client.app)
        try:
            for prof in (local, remote):
                hits = prof.index.search("anything", k=3)
                assert len(hits) == 1
                assert isinstance(hits[0], SearchHit)
                assert hits[0].source_id == "abc"
                assert hits[0].score == 0.5
        finally:
            remote.close()

    def test_contract_ask(self, api_client, fixture_profiles_root):
        """ask() must return PersonaResponse from both backends."""
        # Local: stub the LLM client.
        local = _local_profile(fixture_profiles_root(SLUG))
        fake_client = MagicMock(spec=LLMClient)
        fake_client.complete.return_value = fake_llm_response("answer")
        object.__setattr__(local, "_llm_client", fake_client)
        local.index.search = MagicMock(return_value=[])  # type: ignore[assignment]

        # Server-side: stub the LLM on the cached profile too.
        cache = api_client.app.state.store
        server_prof = cache.get(SLUG)
        server_fake_client = MagicMock(spec=LLMClient)
        server_fake_client.complete.return_value = fake_llm_response("answer")
        object.__setattr__(server_prof, "_llm_client", server_fake_client)
        server_prof.index.search = MagicMock(return_value=[])  # type: ignore[assignment]

        remote = remote_from_app(api_client.app)
        try:
            local_resp = local.persona.ask("Q")
            remote_resp = remote.persona.ask("Q")
            assert isinstance(local_resp, PersonaResponse)
            assert isinstance(remote_resp, PersonaResponse)
            assert local_resp.text == remote_resp.text == "answer"
            assert local_resp.model == remote_resp.model
        finally:
            remote.close()

    def test_remote_hash_and_refusals(self, both_profiles):
        """A published view refuses writes; its digest is of the projected document."""
        _local, remote = both_profiles
        assert remote.content_hash().startswith("sha256:")
        with pytest.raises(ProfileWriteError):
            remote.save_soul("# not yours\n")
        with pytest.raises(CapabilityUnavailableError):
            remote.index.build()

    def test_list_remote(self, api_client):
        http = TestClient(api_client.app)
        items = ResearcherProfile.list_remote("http://testserver", client=http)
        listing = list_registry("http://testserver", client=http)[0]
        assert SLUG in [i["slug"] for i in items]
        assert [i["slug"] for i in items] == [p["slug"] for p in listing.profiles]

    def test_list_registry_reports_a_dead_server_as_data_not_an_exception(self):
        # a dead registry is one failed listing, never a raised exception
        listings = list_registry("http://127.0.0.1:9")
        assert len(listings) == 1
        listing = listings[0]
        assert listing.profiles == ()
        assert listing.error is not None

    @pytest.mark.parametrize(
        "url, expected_base, expected_slug",
        [
            ("http://localhost:8109", "http://localhost:8109", None),
            (
                "http://localhost:8109/api/v1/profiles/jane-doe",
                "http://localhost:8109",
                "jane-doe",
            ),
            (
                "http://localhost:8109/api/v1/profiles/jane-doe/",
                "http://localhost:8109",
                "jane-doe",
            ),
        ],
        ids=["root", "with-slug", "with-slug-trailing-slash"],
    )
    def test_split_profile_url(self, url, expected_base, expected_slug):
        base, slug = _split_profile_url(url)
        assert base == expected_base
        assert slug == expected_slug


class TestInstall:
    """The serve -> install round trip, the cache oracle, and atomicity."""

    @pytest.fixture
    def cache_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "cache"
        root.mkdir()
        return root

    def test_install_round_trip(self, make_api_client, server_root_with_pdf, cache_root):
        http = make_api_client(server_root_with_pdf)
        summary = install_profile(SLUG, url="http://testserver", root=cache_root, client=http)

        assert summary["status"] == "installed"
        assert summary["slug"] == SLUG
        assert summary["level"]
        installed = Path(summary["path"])
        assert installed == cache_root / SLUG
        assert (installed / "profile.jsonld").is_file()
        assert ResearcherProfile.from_files(installed).slug == SLUG

        # paper full text defaults to private, so a public install omits it
        assert summary["archive_tier"] == "public"
        assert not (installed / "sources" / "papers").exists()

    @pytest.mark.parametrize(
        ("force", "status", "marker_survives"),
        [(False, "present", True), (True, "installed", False)],
        ids=["cache-hit-skips-download", "force-replaces"],
    )
    def test_reinstall(
        self, make_api_client, server_root_with_pdf, cache_root, force, status, marker_survives
    ):
        http = make_api_client(server_root_with_pdf)
        install_profile(SLUG, url="http://testserver", root=cache_root, client=http)
        marker = cache_root / SLUG / "LOCAL_EDIT"
        marker.write_text("untouched")

        again = install_profile(
            SLUG, url="http://testserver", root=cache_root, client=http, force=force
        )

        assert again["status"] == status
        assert marker.exists() is marker_survives
        if marker_survives:
            assert marker.read_text() == "untouched"

    def test_digest_mismatch_aborts_before_commit(
        self, make_api_client, server_root_with_pdf, cache_root, monkeypatch
    ):
        """A corrupted transfer must not land in the cache, and must clean up."""
        http = make_api_client(server_root_with_pdf)
        orig_stream = http.stream

        class _BadStream:
            def __init__(self, cm):
                self._cm = cm

            def __enter__(self):
                resp = self._cm.__enter__()
                resp.read()
                object.__setattr__(resp, "_content", resp.content + b"garbage")
                return resp

            def __exit__(self, *exc):
                return self._cm.__exit__(*exc)

        def _patched_stream(method, url, **kw):
            return _BadStream(orig_stream(method, url, **kw))

        monkeypatch.setattr(http, "stream", _patched_stream)

        with pytest.raises(RuntimeError, match="digest mismatch"):
            install_profile(SLUG, url="http://testserver", root=cache_root, client=http)

        assert not (cache_root / SLUG).exists()
        assert list(cache_root.glob(".download-*")) == []
        assert list(cache_root.glob(".install-*")) == []

    def test_missing_profile_reports_cleanly(
        self, make_api_client, server_root_with_pdf, cache_root
    ):
        http = make_api_client(server_root_with_pdf)
        with pytest.raises(RuntimeError, match="not found"):
            install_profile(
                "no-such-person",
                url="http://testserver",
                root=cache_root,
                client=http,
            )
        assert not (cache_root / "no-such-person").exists()

    @pytest.mark.parametrize(
        ("slug", "url", "match"),
        [
            ("../escape", "http://testserver", "invalid slug"),
            (SLUG, None, "no registry URL"),
        ],
    )
    def test_install_argument_errors(self, cache_root, monkeypatch, slug, url, match):
        monkeypatch.delenv("RESEARCHER_PROFILES_REGISTRY_URL", raising=False)
        with pytest.raises(ValueError, match=match):
            install_profile(slug, url=url, root=cache_root)

    def test_seek_and_list(self, make_api_client, server_root_with_pdf, cache_root):
        assert list_installed(cache_root) == []
        with pytest.raises(FileNotFoundError, match="not installed"):
            seek_profile(SLUG, root=cache_root)

        http = make_api_client(server_root_with_pdf)
        install_profile(SLUG, url="http://testserver", root=cache_root, client=http)

        assert list_installed(cache_root) == [SLUG]
        assert seek_profile(SLUG, root=cache_root) == cache_root / SLUG


class TestPushProfile:
    """A publisher PDF does not travel unless the pushing client opts in."""

    @pytest.fixture
    def push_src(self, fixture_profile) -> Path:
        """A local profile directory carrying a copyrighted fulltext PDF."""
        src = fixture_profile(SLUG)
        add_fulltext_pdf(src)
        return src

    @pytest.mark.parametrize(
        "push_kwargs, expect_fulltext",
        [
            # A plain push must not ship publisher-copyrighted PDFs.
            ({}, False),
            # The legitimate case (own backup / entitled registry) still works.
            ({"include_fulltext": True}, True),
        ],
        ids=["excludes-by-default", "can-opt-in"],
    )
    def test_push_fulltext_gate(self, push_src: Path, push_kwargs, expect_fulltext) -> None:
        names = tar_names(_captured_push_bytes(push_src, **push_kwargs))

        if expect_fulltext:
            assert "sources/papers/paper-001.pdf" in names
        else:
            assert not any(n.startswith("sources/papers/") for n in names)
        # The rest of the profile still goes, either way.
        assert "profile.jsonld" in names
        assert "sources/papers.jsonld" in names


class TestStaticClient:
    """``StaticArtifactStorage`` against an httpx.MockTransport serving the fixture dir."""

    @pytest.fixture
    def local(self, jane_doe_readonly_dir):
        """The directory the mock host serves: a published profile is a directory."""
        return jane_doe_readonly_dir

    @pytest.fixture
    def http(self, local) -> httpx.Client:
        return httpx.Client(transport=_static_transport(local, BASE))

    def test_parity_with_from_files(self, http, local):
        local = ResearcherProfile.from_files(local)
        remote = ResearcherProfile.from_url(BASE, client=http)
        assert isinstance(remote.storage, StaticArtifactStorage)
        assert remote.slug == "jane-doe"
        assert remote.name == local.name
        assert remote.rid == local.rid
        assert remote.metadata.rid == local.metadata.rid
        assert remote.level == local.level
        assert len(remote.papers) == len(local.papers)
        assert remote.papers[0].paper_id == local.papers[0].paper_id
        assert remote.expertise == local.expertise
        assert remote.soul == local.soul
        assert remote.citations == local.citations
        assert sorted(remote.summaries) == sorted(local.summaries)
        assert remote.content_hash() == local.content_hash()

    def test_summaries_enumerated_from_manifest_and_lazy(self, http, local):
        prof = ResearcherProfile.from_url(BASE, client=http)
        keys = list(prof.summaries)
        assert keys
        sample = keys[0]
        body = prof.summaries[sample]
        assert body == (local / "sources" / "summaries" / f"{sample}.summary.md").read_text(
            encoding="utf-8"
        )
        with pytest.raises(KeyError):
            prof.summaries["no-such-paper"]

    def test_missing_optional_files_default(self, http):
        prof = ResearcherProfile.from_url(BASE, client=http)
        # fixture has no grants.jsonld
        assert prof.grants == []
        # build state is local-only: always empty on a static profile
        assert not prof.build_state.papers

    def test_from_files_dispatches_urls(self):
        prof = ResearcherProfile.from_files(BASE)
        assert isinstance(prof.storage, StaticArtifactStorage)
        assert prof.storage.base_url == BASE
        prof.close()

    def test_from_files_url_validate_raises(self):
        with pytest.raises(ValueError):
            ResearcherProfile.from_files(BASE, validate=True)

    def test_s3_url_translated(self, http):
        prof = ResearcherProfile.from_url("s3://my-bucket/profiles/jane-doe", client=http)
        assert prof.storage.base_url.startswith("https://my-bucket.s3.amazonaws.com")
        assert prof.slug == "jane-doe"
        assert "Doe" in prof.name

    @pytest.mark.parametrize(
        ("call", "exc"),
        [
            (lambda p: p.index.search("anything"), NotImplementedError),
            (lambda p: p.index.build(), CapabilityUnavailableError),
            (lambda p: p.validate(), NotImplementedError),
            (lambda p: p.save_soul("# not yours\n"), ProfileWriteError),
        ],
        ids=["search", "build", "validate", "save-soul"],
    )
    def test_static_refusals(self, http, call, exc):
        with pytest.raises(exc):
            call(ResearcherProfile.from_url(BASE, client=http))

    def test_missing_profile_jsonld_raises(self, http):
        prof = ResearcherProfile.from_url(
            "https://example.org/profiles/jane-doe/nonexistent", client=http
        )
        with pytest.raises(ProfileLoadError):
            _ = prof.metadata
