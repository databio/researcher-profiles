"""The served profile: the HTTP surface and every client that speaks to it.

One FastAPI app, four ways in: the read/LLM endpoints, push, archive+install,
and match. Plus the three clients (``from_api``, ``push_profile`` /
``install_profile``, and the static-host reader) whose contract must match what
a locally loaded profile gives you.
"""

import io
import json
import re
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from researcher_profiles import LLMClient, ResearcherProfile, StaticArtifactStorage
from researcher_profiles.cli.auth.agent import Credential, InsufficientScopeError, ManagementClient
from researcher_profiles.client import (
    ApiArtifactStorage,
    _split_profile_url,
    install_profile,
    list_installed,
    list_registry,
    push_profile,
    resolve_rid,
    seek_profile,
)
from researcher_profiles.embeddings.cache import SearchHit
from researcher_profiles.errors import (
    CapabilityUnavailableError,
    ProfileLoadError,
    ProfileWriteError,
)
from researcher_profiles.models.results import Match, MatchEvidence, PersonaResponse
from researcher_profiles.resolve import Candidate
from researcher_profiles.schema import ProfileDocument

from .factories import FIXTURE_DIR, fake_llm_response

# --------------------------------------------------------------------------
# The server: routes, auth, and the from_api client
# --------------------------------------------------------------------------


SLUG = "jane-doe"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_fulltext_pdf(profile_dir: Path) -> Path:
    """Plant a publisher-copyrighted PDF the archive boundary must withhold."""
    papers = profile_dir / "sources" / "papers"
    papers.mkdir(parents=True, exist_ok=True)
    path = papers / "paper-001.pdf"
    path.write_bytes(b"%PDF-1.4 fake fulltext")
    return path


def _tar_of_dir(src: Path) -> bytes:
    """Gzipped tar of a directory's CONTENTS (``profile.jsonld`` at tar root)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for child in sorted(src.iterdir()):
            tf.add(child, arcname=child.name)
    return buf.getvalue()


def _tar_of_members(members) -> bytes:
    """Gzipped tar built member-by-member, so a test can plant a hostile name."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _tar_names(data: bytes) -> set[str]:
    """The member names of a gzipped tarball."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        return {m.name for m in tf.getmembers()}


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


def _management_client() -> tuple[ManagementClient, MagicMock]:
    """A management client with its one session replaced by a request recorder."""
    client = ManagementClient(
        Credential(key="rpa_test", url="https://profiles.example.org", source="test")
    )
    session = MagicMock()
    client._session = session
    return client, session


class TestManagementClient:
    """The management client preserves the profile-service wire contract."""

    @pytest.mark.parametrize(
        "resource, method, args, kwargs, http_method, path, body",
        [
            ("identity", "whoami", (), {}, "get", "/api/manage/agent/whoami", None),
            ("identity", "scopes", (), {}, "get", "/api/manage/agent/scopes", None),
            ("profile", "get", (SLUG,), {}, "get", f"/api/v1/profiles/{SLUG}", None),
            (
                "profile",
                "patch_metadata",
                (SLUG, {"name": "Jane Q. Doe"}),
                {"base_hash": "before"},
                "patch",
                f"/api/v1/profiles/{SLUG}/metadata",
                {"name": "Jane Q. Doe", "base_hash": "before"},
            ),
            (
                "profile",
                "put_soul",
                (SLUG, "A narrative."),
                {},
                "put",
                f"/api/v1/profiles/{SLUG}/soul",
                {"soul": "A narrative."},
            ),
            (
                "profile",
                "patch_visibility",
                (SLUG,),
                {
                    "profile_visibility": "internal",
                    "artifacts": [{"role": "soul", "visibility": "restricted"}],
                },
                "patch",
                f"/api/v1/profiles/{SLUG}/visibility",
                {
                    "profile_visibility": "internal",
                    "artifacts": [{"role": "soul", "visibility": "restricted"}],
                },
            ),
        ],
        ids=["whoami", "scopes", "get", "patch-metadata", "put-soul", "patch-visibility"],
    )
    def test_resource_methods_keep_their_request_shape(
        self, resource, method, args, kwargs, http_method, path, body
    ):
        client, session = _management_client()
        response = MagicMock(status_code=200, is_success=True)
        response.json.return_value = {"ok": True}
        getattr(session, http_method).return_value = response

        result = getattr(getattr(client, resource), method)(*args, **kwargs)

        request = getattr(session, http_method)
        url = f"https://profiles.example.org{path}"
        if body is None:
            request.assert_called_once_with(url)
        else:
            request.assert_called_once_with(url, json=body)
        assert result == {"ok": True}

    @pytest.mark.parametrize(
        "resource, method, args, kwargs",
        [
            ("identity", "whoami", (), {}),
            ("identity", "scopes", (), {}),
            ("profile", "get", (SLUG,), {}),
            ("profile", "patch_metadata", (SLUG, {"name": "Jane"}), {}),
            ("profile", "put_soul", (SLUG, "A narrative."), {}),
            ("profile", "patch_visibility", (SLUG,), {}),
        ],
        ids=["whoami", "scopes", "get", "patch-metadata", "put-soul", "patch-visibility"],
    )
    def test_resource_methods_preserve_insufficient_scope_error(
        self, resource, method, args, kwargs
    ):
        client, session = _management_client()
        response = MagicMock(status_code=403, is_success=False)
        response.json.return_value = {
            "error": "insufficient_scope",
            "hint": "Need profile:metadata.",
            "missing": ["profile:metadata"],
        }
        session.get.return_value = response
        session.patch.return_value = response
        session.put.return_value = response

        with pytest.raises(InsufficientScopeError) as error:
            getattr(getattr(client, resource), method)(*args, **kwargs)

        assert error.value.missing == ["profile:metadata"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client(make_api_client, fixture_profiles_root):
    """A TestClient over a profiles root holding one copy of ``jane-doe``."""
    return make_api_client(fixture_profiles_root(SLUG))


def _remote_from_app(app, slug=SLUG) -> ResearcherProfile:
    """Build an ``ApiArtifactStorage``-backed profile that talks to the in-process app
    using a starlette TestClient (which is itself an httpx.Client)."""
    http = TestClient(app)
    return ResearcherProfile(
        ApiArtifactStorage(slug=slug, base_url="http://testserver", client=http)
    )


def _local_profile(root: Path) -> ResearcherProfile:
    return ResearcherProfile.from_files(root / SLUG)


INCOMPLETE_SLUG = "incomplete-profile"


def _stub_llm_slug(app, slug, response_text):
    cache = app.state.store
    prof = cache.get(slug)
    fake_client = MagicMock(spec=LLMClient)
    fake_client.complete.return_value = fake_llm_response(response_text)
    object.__setattr__(prof, "_llm_client", fake_client)
    prof.index.search = MagicMock(return_value=[])  # type: ignore[assignment]
    return prof


# The four persona endpoints, described once: the endpoint name, the request
# body it takes, and the canned LLM completion the stub should return. Every
# persona test (409 guard, 200 guard, response shape) parametrizes over this.
PERSONA_IDS = ["ask", "review", "innovate", "riff"]

PERSONA_CASES = [
    ("ask", {"question": "what?"}, "server-answer"),
    ("review", {"material": "a draft", "focus": "novelty"}, "server-review"),
    (
        "innovate",
        {"topic": "chromatin", "n": 1},
        '{"ideas": [{"hypothesis":"H","approach":"A","rationale":"R","related_works":["x"]}]}',
    ),
    (
        "riff",
        {"seed": "consensus", "n": 1},
        '{"riffs": [{"angle":"contrarian","text":"T","related_work":null}]}',
    ),
]

# Per-endpoint payload assertions, as dotted JSON paths -> expected values.
PERSONA_EXPECTED = {
    "ask": {"text": "server-answer", "model": "claude-sonnet-4-6"},
    "review": {"text": "server-review"},
    "innovate": {"items.0.hypothesis": "H"},
    "riff": {"items.0.angle": "contrarian", "items.0.related_work": None},
}


def _persona_params(slug, *, with_expected=False):
    """``PERSONA_CASES`` with the endpoint name resolved to a path under ``slug``."""
    out = []
    for endpoint, body, response_text in PERSONA_CASES:
        row = (f"/api/v1/profiles/{slug}/{endpoint}", body, response_text)
        if with_expected:
            row += (PERSONA_EXPECTED[endpoint],)
        out.append(row)
    return out


def _dotted_get(payload, dotted):
    """Walk ``payload`` along a dotted JSON path (list indices are numeric parts).

    A missing key or index raises rather than yielding ``None``, so an expected
    value of ``None`` asserts "present and null", not "absent".
    """
    cur = payload
    for part in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            if part not in cur:
                raise KeyError(f"{dotted!r}: no key {part!r} in {cur!r}")
            cur = cur[part]
    return cur


class TestServerCore:
    """The HTTP surface itself: URL parsing, routes, auth, and error mapping."""

    # ----------------------------------------------------------------------
    # URL parsing
    # ----------------------------------------------------------------------

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

    # ----------------------------------------------------------------------
    # Health + listing
    # ----------------------------------------------------------------------

    def test_health(self, api_client):
        r = api_client.get("/health")
        assert r.status_code == 200
        j = r.json()
        assert j["status"] == "ok"
        assert j["profile_count"] >= 1

    def test_list_profiles(self, api_client):
        r = api_client.get("/api/v1/profiles")
        assert r.status_code == 200
        data = r.json()
        assert "rp:profileList" in data
        assert "profiles" in data
        items = data["profiles"]
        slugs = [x["slug"] for x in items]
        assert SLUG in slugs
        me = [x for x in items if x["slug"] == SLUG][0]
        assert me["name"]
        # ProfileSummary surfaces the depth tier; fixture has no level -> "full".
        assert me["level"] == "full"

    def test_profile_detail(self, api_client):
        r = api_client.get(f"/api/v1/profiles/{SLUG}")
        assert r.status_code == 200
        d = r.json()
        assert d["slug"] == SLUG
        assert d["metadata"]["name"]
        # ProfileDetail.metadata surfaces the depth tier.
        assert d["metadata"]["level"] == "full"
        assert isinstance(d["expertise"], str)
        assert isinstance(d["soul"], str)

    def test_profile_detail_404(self, api_client):
        r = api_client.get("/api/v1/profiles/does-not-exist")
        assert r.status_code == 404

    def test_papers_carry_their_identifiers(self, make_api_client, fixture_profiles_root):
        """DOI / PMID / OpenAlex id / authors survive the wire.

        A remote consumer that gets a title and nothing else has to guess which
        work the title names, and a title search is one near-duplicate away from
        the wrong paper. These come off ``sources/papers.jsonld``, the same
        artifact, at the same tier, as the ``title`` beside them. Serving
        them widens no tier; an identifier points at the open bibliographic
        record, not at content.
        """
        root = fixture_profiles_root(SLUG)
        papers_path = root / SLUG / "sources" / "papers.jsonld"
        doc = json.loads(papers_path.read_text())
        entry = doc["hasPart"][0]
        entry["doi"] = "10.1234/example.2016"
        entry["pmid"] = "27000000"
        entry["openalex_id"] = "W2000000001"
        entry["author"] = ["Jane A. Doe", "John Roe"]
        papers_path.write_text(json.dumps(doc))

        client = make_api_client(root)
        by_id = {p["paper_id"]: p for p in client.get(f"/api/v1/profiles/{SLUG}/papers").json()}
        served = by_id["doe2016example"]
        assert served["doi"] == "10.1234/example.2016"
        assert served["pmid"] == "27000000"
        assert served["openalex_id"] == "W2000000001"
        assert served["authors"] == ["Jane A. Doe", "John Roe"]

        # ...and the SDK client rebuilds a PaperRecord that still carries them,
        # so a consumer reading a hosted profile is not worse off than one
        # reading the directory.
        remote = _remote_from_app(client.app)
        try:
            record = next(p for p in remote.papers if p.paper_id == "doe2016example")
            assert record.doi == "10.1234/example.2016"
            assert record.openalex_id == "W2000000001"
            assert record.authors == ["Jane A. Doe", "John Roe"]
        finally:
            remote.close()

    def test_list_papers(self, api_client):
        r = api_client.get(f"/api/v1/profiles/{SLUG}/papers")
        assert r.status_code == 200
        papers = r.json()
        # The jane-doe fixture carries 8 papers, 5 of which have a summary file
        # in sources/summaries/. Asserting the join unconditionally is the point:
        # `if papers:` let this pass on an empty response.
        by_id = {p["paper_id"]: p for p in papers}
        assert len(by_id) == 8
        assert by_id["doe2016example"]["title"] == (
            "ExampleOverlap: enrichment analysis of example region sets"
        )
        assert by_id["doe2016example"]["year"] == 2016
        # summary_available is a real join against sources/summaries/, not a
        # constant: 5 of the 8 papers have one.
        assert by_id["doe2016example"]["summary_available"] is True
        assert by_id["roe2018toolkit"]["summary_available"] is False
        assert sum(p["summary_available"] for p in papers) == 5

    # ----------------------------------------------------------------------
    # Auth
    # ----------------------------------------------------------------------

    def test_reads_are_never_gated_by_the_operator_token(
        self, make_api_client, fixture_profiles_root
    ):
        # The read surface carries no router-level credential gate, even when a
        # token is configured. Privacy is per profile and per caller (the viewer
        # tier), so an anonymous caller reads a public profile and is
        # projected down to the public tier.
        c = make_api_client(fixture_profiles_root(SLUG), token="secret-token")
        r = c.get("/api/v1/profiles")
        assert r.status_code == 200
        assert r.headers["X-RP-Viewer-Tier"] == "public"
        # A write/heavy endpoint still requires the token.
        r = c.post("/api/v1/match", json={"query": "x"})
        assert r.status_code == 401
        r = c.post(
            "/api/v1/match",
            json={"query": "x"},
            headers={"Authorization": "Bearer secret-token"},
        )
        # 200 (or 503 if embeddings/index unavailable), never 401 with the token.
        assert r.status_code != 401
        # /health should remain open (no auth dependency).
        r = c.get("/health")
        assert r.status_code == 200

    def test_the_operator_token_widens_the_tier_rather_than_opening_a_gate(
        self, make_api_client, fixture_profiles_root
    ):
        # A site-wide "is the surface open" switch would be the wrong axis; the
        # question is "how much of this profile may this caller see". The token
        # widens the caller's tier to ``restricted``; it does not unlock a door.
        c = make_api_client(fixture_profiles_root(SLUG), token="secret-token")
        anon = c.get("/api/v1/profiles")
        assert anon.status_code == 200
        assert anon.headers["X-RP-Viewer-Tier"] == "public"
        operator = c.get(
            "/api/v1/profiles",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert operator.status_code == 200
        assert operator.headers["X-RP-Viewer-Tier"] == "restricted"

    def test_a_private_profile_is_withheld_from_anonymous_but_not_the_operator(
        self, make_api_client, fixture_profiles_root, tmp_path
    ):
        # The private-registry posture, expressed per profile: the document
        # itself declares ``internal``, so an anonymous caller gets the same 404
        # a nonexistent slug gets, and the operator gets the profile.
        import shutil

        from researcher_profiles import ResearcherProfile

        root = tmp_path / "private-profiles"
        root.mkdir()
        shutil.copytree(fixture_profiles_root(SLUG) / SLUG, root / SLUG)
        prof = ResearcherProfile.from_files(root / SLUG)
        doc = prof.metadata
        doc.visibility = "internal"
        prof.save_profile(doc)

        c = make_api_client(root, token="secret-token")
        assert c.get(f"/api/v1/profiles/{SLUG}").status_code == 404
        assert c.get("/api/v1/profiles").json()["profiles"] == []
        auth = {"Authorization": "Bearer secret-token"}
        assert c.get(f"/api/v1/profiles/{SLUG}", headers=auth).status_code == 200
        assert [p["slug"] for p in c.get("/api/v1/profiles", headers=auth).json()["profiles"]] == [
            SLUG
        ]

    # ----------------------------------------------------------------------
    # Search via API (stubbed)
    # ----------------------------------------------------------------------

    def test_search_endpoint_with_stubbed_index(self, api_client, monkeypatch):
        # Stub the search method on the cached profile.
        from researcher_profiles.embeddings.cache import SearchHit

        cache = api_client.app.state.store
        prof = cache.get(SLUG)
        fake_hits = [
            SearchHit(
                text="example chunk",
                source_type="paper_summary",
                source_id="paper1",
                chunk_index=0,
                section=None,
                cosine=0.8,
                score=0.9,
                meta={"year": 2020},
            )
        ]
        prof.index.search = MagicMock(return_value=fake_hits)  # type: ignore[assignment]

        with TestClient(api_client.app) as c:
            r = c.post(
                f"/api/v1/profiles/{SLUG}/search",
                json={"query": "chromatin", "k": 3},
            )
        assert r.status_code == 200
        j = r.json()
        assert len(j["hits"]) == 1
        assert j["hits"][0]["source_id"] == "paper1"

    @pytest.mark.parametrize(
        "path, body, response_text, expected",
        _persona_params(SLUG, with_expected=True),
        ids=PERSONA_IDS,
    )
    def test_persona_endpoint_response_shape(self, api_client, path, body, response_text, expected):
        """Each persona endpoint returns 200 and its own documented payload shape."""
        _stub_llm_slug(api_client.app, SLUG, response_text)
        with TestClient(api_client.app) as c:
            r = c.post(path, json=body)
        assert r.status_code == 200
        j = r.json()
        for dotted, want in expected.items():
            assert _dotted_get(j, dotted) == want

    @pytest.fixture
    def mixed_client(self, make_api_client, fixture_profiles_root):
        """A client over a root serving both the complete and incomplete fixtures."""
        return make_api_client(fixture_profiles_root(SLUG, INCOMPLETE_SLUG))

    @pytest.mark.parametrize(
        "path, body",
        [(path, body) for path, body, _ in _persona_params(INCOMPLETE_SLUG)],
        ids=PERSONA_IDS,
    )
    def test_persona_endpoints_incomplete_return_409(self, mixed_client, path, body):
        r = mixed_client.post(path, json=body)
        assert r.status_code == 409, r.text
        assert r.status_code != 500
        assert "persona" in r.json()["detail"].lower()


class TestFromApiClient:
    """``from_api``: the same assertions must pass over HTTP as over files."""

    def test_remote_404_raises_keyerror(self, api_client):
        remote = _remote_from_app(api_client.app, slug="does-not-exist")
        with pytest.raises(KeyError):
            _ = remote.metadata

    def test_remote_401_raises_permission_error(self, make_api_client, fixture_profiles_root):
        # A wrong credential on a scoped endpoint is a 401, and the client
        # maps it to PermissionError. The read surface has no credential gate
        # to fail (a caller with a bad token is an anonymous caller, projected
        # to the public tier), so the assertion is made where a credential is
        # genuinely required.
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
        remote = _remote_from_app(api_client.app)
        yield local, remote
        remote.close()

    @pytest.mark.parametrize(
        "accessor",
        [
            lambda p: p.slug,
            lambda p: p.metadata.name,
            lambda p: p.metadata.affiliation,
            lambda p: p.expertise,
            lambda p: p.soul,
            # The remote summaries are limited to papers with summary_available
            # True; local LazySummaries enumerates directly from disk. They should
            # agree.
            lambda p: set(p.summaries.keys()),
        ],
        ids=[
            "slug",
            "metadata.name",
            "metadata.affiliation",
            "expertise",
            "soul",
            "summaries-keys",
        ],
    )
    def test_contract_parity(self, both_profiles, accessor):
        """Each simple accessor must read the same over HTTP as over files."""
        local, remote = both_profiles
        assert accessor(local) == accessor(remote)

    def test_contract_papers(self, both_profiles):
        local, remote = both_profiles
        assert len(local.papers) == len(remote.papers)
        if local.papers:
            # Compare a few stable fields on the first paper.
            lp, rp = local.papers[0], remote.papers[0]
            assert lp.title == rp.title
            assert lp.year == rp.year

    def test_contract_search_isinstance_and_attrs(self, api_client, fixture_profiles_root):
        """Both backends should return objects with the same SearchHit shape."""
        from researcher_profiles.embeddings.cache import SearchHit

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

        # Stub server-side search on cached profile.
        cache = api_client.app.state.store
        server_prof = cache.get(SLUG)
        server_prof.index.search = MagicMock(return_value=fake_hits)  # type: ignore[assignment]

        remote = _remote_from_app(api_client.app)
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

        remote = _remote_from_app(api_client.app)
        try:
            local_resp = local.persona.ask("Q")
            remote_resp = remote.persona.ask("Q")
            assert isinstance(local_resp, PersonaResponse)
            assert isinstance(remote_resp, PersonaResponse)
            assert local_resp.text == remote_resp.text == "answer"
            assert local_resp.model == remote_resp.model
        finally:
            remote.close()

    def test_contract_read_parity(self, both_profiles):
        """The same profile, read locally and over HTTP, agrees field for field."""
        local, remote = both_profiles
        assert remote.metadata.rid == local.metadata.rid
        assert remote.name == local.name
        assert remote.soul == local.soul
        assert remote.expertise == local.expertise
        assert sorted(remote.summaries) == sorted(local.summaries)
        # The digest is computed from what the server serves: a
        # privacy-projected document, not the raw published bytes. So it is
        # well-formed here rather than equal to the local one. The static
        # backend, which fetches the file itself, does assert equality.
        assert remote.content_hash().startswith("sha256:")

    def test_a_remote_profile_writes_nowhere_and_builds_no_index(self, both_profiles):
        """A published view refuses, rather than writing to a synthetic path."""
        _local, remote = both_profiles
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
        # A real closed port, matching the style of
        # test_listr_when_every_registry_is_down: a dead registry is one
        # failed listing among possibly several, never a raised exception.
        listings = list_registry("http://127.0.0.1:9")
        assert len(listings) == 1
        listing = listings[0]
        assert listing.profiles == ()
        assert listing.error is not None


# --------------------------------------------------------------------------
# PUT /api/v1/profiles/{slug}
# --------------------------------------------------------------------------


OTHER = "john-smith"


def _tar_with_symlink() -> bytes:
    """A tarball whose second member is a symlink escaping to /etc/passwd."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"name: X\n"
        info = tarfile.TarInfo(name="profile.jsonld")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(name="escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    return buf.getvalue()


class TestPush:
    """Tests for the profile push endpoint (PUT /api/v1/profiles/{slug}).

    Covers: push -> live listing roundtrip, overwrite of an existing profile,
    traversal/symlink rejection, the size cap, bad-archive handling, and the
    critical invalidation behavior: a push drops the cached registry snapshot so
    a subsequent /match rebuilds over the new profile set.
    """

    # ----------------------------------------------------------------------
    # Roundtrip + overwrite
    # ----------------------------------------------------------------------

    def test_push_new_profile_appears_live(self, api_client):
        r = api_client.put(f"/api/v1/profiles/{OTHER}", content=_tar_of_dir(FIXTURE_DIR / OTHER))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["slug"] == OTHER
        assert body["name"]
        assert body["indexed"] is False  # fixture has no embeddings.sqlite

        slugs = [x["slug"] for x in api_client.get("/api/v1/profiles").json()["profiles"]]
        assert OTHER in slugs
        assert api_client.get(f"/api/v1/profiles/{OTHER}").status_code == 200

    def test_push_overwrites_existing_profile(
        self, api_client, fixture_profiles_root, fixture_profile
    ):
        root = fixture_profiles_root(SLUG)
        # Warm the cache with the original profile.
        orig_name = api_client.get(f"/api/v1/profiles/{SLUG}").json()["metadata"]["name"]

        staged = fixture_profile(SLUG)
        py = (staged / "profile.jsonld").read_text()
        assert orig_name in py
        (staged / "profile.jsonld").write_text(py.replace(orig_name, "Renamed Person"))

        r = api_client.put(f"/api/v1/profiles/{SLUG}", content=_tar_of_dir(staged))
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Renamed Person"
        # The cached profile object was evicted -> reads reflect the new content.
        d = api_client.get(f"/api/v1/profiles/{SLUG}").json()
        assert d["metadata"]["name"] == "Renamed Person"
        # No leftover staging/backup dirs in the profiles root or the listing.
        leftovers = [p.name for p in root.iterdir() if p.name.startswith((".upload-", ".old-"))]
        assert leftovers == []

    # ----------------------------------------------------------------------
    # Rejection paths
    # ----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "slug, make_body, detail",
        [
            ("Bad_Slug", lambda: _tar_of_dir(FIXTURE_DIR / OTHER), "invalid slug"),
            (OTHER, lambda: _tar_of_members({"notes.md": b"hello"}), "profile.jsonld"),
            (
                OTHER,
                lambda: _tar_of_members({"profile.jsonld": b"name: X\n", "../evil.txt": b"pwned"}),
                "traversal",
            ),
            (
                OTHER,
                lambda: _tar_of_members(
                    {"profile.jsonld": b"name: X\n", "/tmp/evil.txt": b"pwned"}
                ),
                None,
            ),
            (OTHER, _tar_with_symlink, "link"),
            (OTHER, lambda: b"not a tarball", None),
        ],
        ids=[
            "bad-slug",
            "missing-profile-document",
            "path-traversal",
            "absolute-path",
            "symlink-member",
            "garbage-body",
        ],
    )
    def test_push_rejects_bad_upload(self, api_client, slug, make_body, detail):
        """Every malformed or hostile upload is a 400, never a 5xx or a partial write."""
        r = api_client.put(f"/api/v1/profiles/{slug}", content=make_body())
        assert r.status_code == 400
        if detail is not None:
            assert detail in r.json()["detail"]

    def test_push_size_cap(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG), max_upload_bytes=1024)
        r = c.put(
            f"/api/v1/profiles/{OTHER}",
            content=_tar_of_dir(FIXTURE_DIR / OTHER),
        )
        assert r.status_code == 413

    def test_push_requires_token_when_configured(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG), token="sekrit")
        r = c.put(
            f"/api/v1/profiles/{OTHER}",
            content=_tar_of_dir(FIXTURE_DIR / OTHER),
        )
        assert r.status_code == 401
        r = c.put(
            f"/api/v1/profiles/{OTHER}",
            content=_tar_of_dir(FIXTURE_DIR / OTHER),
            headers={"Authorization": "Bearer sekrit"},
        )
        assert r.status_code == 200

    def test_failed_push_leaves_existing_profile_intact(self, api_client):
        orig = api_client.get(f"/api/v1/profiles/{SLUG}").json()
        r = api_client.put(
            f"/api/v1/profiles/{SLUG}",
            content=_tar_of_members({"profile.jsonld": b": not [valid yaml\n"}),
        )
        assert r.status_code == 400
        assert api_client.get(f"/api/v1/profiles/{SLUG}").json() == orig

    # ----------------------------------------------------------------------
    # Registry invalidation (the critical one)
    # ----------------------------------------------------------------------

    def test_push_moves_the_write_generation_and_drops_disk_caches(
        self, api_client, fixture_profiles_root
    ):
        """A profile pushed AFTER a /match must appear in subsequent matches.

        There is no snapshot on ``app.state`` to drop any more. The analytics
        cache on the store, stamped with its write generation, so the push has
        to move that generation (which is what rebuilds the roster) and take
        the on-disk ``.cache`` memos with it.
        """
        from researcher_profiles.api.deps import get_store

        reg_dir = fixture_profiles_root(SLUG) / ".cache"
        reg_dir.mkdir()
        (reg_dir / "centroids.npz").write_bytes(b"stale")
        (reg_dir / "topics.json").write_text("{}")

        app = api_client.app
        store = get_store(SimpleNamespace(app=app))
        before = store.generation
        roster = store._rostered()
        assert roster.slugs == [SLUG]

        r = api_client.put(
            f"/api/v1/profiles/{OTHER}",
            content=_tar_of_dir(FIXTURE_DIR / OTHER),
        )
        assert r.status_code == 200
        assert store.generation > before
        # Nobody called invalidate: the stamp moved, so the roster rebuilt.
        assert store._rostered() is not roster
        assert store._rostered().slugs == sorted([SLUG, OTHER])
        assert not (reg_dir / "centroids.npz").exists()
        assert not (reg_dir / "topics.json").exists()

    # ----------------------------------------------------------------------
    # Push goes through the spec-whitelist archive builder
    # ----------------------------------------------------------------------

    def test_push_strips_non_spec_html(self, api_client, fixture_profile):
        """A sources/html/ scrape on disk never reaches the server via push."""
        src = fixture_profile(OTHER)
        html_dir = src / "sources" / "html"
        html_dir.mkdir(parents=True, exist_ok=True)
        (html_dir / "big.html").write_text("<html>" + "x" * 200000 + "</html>")

        summary = push_profile("http://testserver", src, client=api_client)
        assert summary["slug"] == OTHER

        # The pushed-and-swapped profile on the server carries no sources/html/.
        server_profile = api_client.app.state.store.root / OTHER
        assert not (server_profile / "sources" / "html").exists()
        assert (server_profile / "profile.jsonld").is_file()


_LOCAL_RID_RE = re.compile(r"^local:[a-z0-9][a-z0-9-]*-[0-9a-f]{6}$")


class TestJsonUpsertAndMint:
    """``PUT /api/v1/profiles/{slug}`` with ``Content-Type: application/json``.

    The tarball push (``TestPush``) shares this URL; a JSON body is dispatched
    to the document-only upsert instead. Covers create/replace, validation and
    slug guards, ``If-Match`` optimistic concurrency, and server-side ``local:``
    rid minting. Backend-agnostic, so served over a fresh filesystem store.
    """

    def test_json_create_then_replace(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        body = {"name": "Ada Lovelace", "rid": "local:ada-a1b2c3", "provenance": "synthetic"}
        r = c.put("/api/v1/profiles/ada", json=body)
        assert r.status_code == 200, r.text
        payload = r.json()
        # PushResponse shape.
        assert payload["slug"] == "ada"
        assert payload["rid"] == "local:ada-a1b2c3"
        assert payload["name"] == "Ada Lovelace"
        assert "level" in payload
        assert payload["indexed"] is False  # JSON upsert never carries an index
        assert c.get("/api/v1/profiles/ada").json()["metadata"]["name"] == "Ada Lovelace"

        # Replace: same slug + rid, new name.
        body["name"] = "Ada, Countess of Lovelace"
        r = c.put("/api/v1/profiles/ada", json=body)
        assert r.status_code == 200, r.text
        assert c.get("/api/v1/profiles/ada").json()["metadata"]["name"] == (
            "Ada, Countess of Lovelace"
        )

    def test_json_invalid_document_is_422(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        # A rid is present (so the mint 400 does not fire), but it is malformed,
        # so ProfileDocument validation rejects it.
        r = c.put(
            "/api/v1/profiles/ada",
            json={"name": "Ada", "rid": "not-a-valid-rid", "provenance": "synthetic"},
        )
        assert r.status_code == 422, r.text

    def test_json_bad_slug_is_400(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put(
            "/api/v1/profiles/Bad_Slug",
            json={"name": "Ada", "rid": "local:ada-a1b2c3", "provenance": "synthetic"},
        )
        assert r.status_code == 400, r.text

    def test_if_match_conflict_returns_409_with_current_hash(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        body = {"name": "Ada Lovelace", "rid": "local:ada-a1b2c3", "provenance": "synthetic"}
        assert c.put("/api/v1/profiles/ada", json=body).status_code == 200
        current = c.get("/api/v1/profiles/ada").json()["content_hash"]

        body["name"] = "Someone Else"
        r = c.put(
            "/api/v1/profiles/ada",
            json=body,
            headers={"If-Match": "sha256:stale-and-wrong"},
        )
        assert r.status_code == 409, r.text
        assert r.headers["X-RP-Content-Hash"] == current

        # A matching If-Match is accepted.
        r = c.put("/api/v1/profiles/ada", json=body, headers={"If-Match": current})
        assert r.status_code == 200, r.text

    def test_mint_local_rid_json_flag(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put(
            "/api/v1/profiles/ada",
            json={"name": "Ada Lovelace", "mintLocalRid": True, "provenance": "synthetic"},
        )
        assert r.status_code == 200, r.text
        assert _LOCAL_RID_RE.match(r.json()["rid"])

    def test_mint_local_rid_query_param(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put(
            "/api/v1/profiles/grace?mint=local",
            json={"name": "Grace Hopper", "provenance": "synthetic"},
        )
        assert r.status_code == 200, r.text
        assert _LOCAL_RID_RE.match(r.json()["rid"])

    def test_no_rid_without_opt_in_is_400(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put("/api/v1/profiles/ada", json={"name": "Ada", "provenance": "synthetic"})
        assert r.status_code == 400, r.text

    def test_mint_with_empty_name_is_400(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put(
            "/api/v1/profiles/ada",
            json={"name": "", "mintLocalRid": True, "provenance": "synthetic"},
        )
        assert r.status_code == 400, r.text

    def test_rid_present_and_mint_flag_is_400(self, make_api_client, tmp_path):
        c = make_api_client(tmp_path)
        r = c.put(
            "/api/v1/profiles/ada",
            json={
                "name": "Ada",
                "rid": "local:ada-a1b2c3",
                "mintLocalRid": True,
                "provenance": "synthetic",
            },
        )
        assert r.status_code == 400, r.text

    def test_a_non_json_body_still_takes_the_tarball_path(self, make_api_client, tmp_path):
        """Content-Type dispatch must not break the tarball push: a gzipped-tar
        body (no application/json) still ingests as a bundle."""
        c = make_api_client(tmp_path)
        r = c.put(f"/api/v1/profiles/{OTHER}", content=_tar_of_dir(FIXTURE_DIR / OTHER))
        assert r.status_code == 200, r.text
        assert c.get(f"/api/v1/profiles/{OTHER}").status_code == 200


# --------------------------------------------------------------------------
# The archive endpoint and the local-cache install path
# --------------------------------------------------------------------------


def _captured_push_bytes(src: Path, **kw) -> bytes:
    """Run push_profile against a stub client and return the uploaded body."""
    from researcher_profiles.client import push_profile

    sent: dict[str, bytes] = {}

    class _StubResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"slug": SLUG, "name": "Jane Doe", "level": "full", "indexed": False}

    class _StubClient:
        def put(self, url, *, content, headers=None):
            sent["body"] = content
            return _StubResponse()

    push_profile("http://testserver", src, client=_StubClient(), **kw)
    return sent["body"]


class TestArchiveAndInstall:
    """The push -> serve -> install round trip, the cache oracle, and atomicity."""

    @pytest.fixture
    def server_root(self, fixture_profiles_root) -> Path:
        """A served profiles root whose one profile carries a copyrighted PDF."""
        root = fixture_profiles_root(SLUG)
        _add_fulltext_pdf(root / SLUG)
        return root

    @pytest.fixture
    def cache_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "cache"
        root.mkdir()
        return root

    def test_install_round_trip(self, make_api_client, server_root, cache_root):
        """A served profile installs into the local cache and loads from disk."""
        http = make_api_client(server_root)
        summary = install_profile(SLUG, url="http://testserver", root=cache_root, client=http)

        assert summary["status"] == "installed"
        assert summary["slug"] == SLUG
        assert summary["level"]
        installed = Path(summary["path"])
        assert installed == cache_root / SLUG
        assert (installed / "profile.jsonld").is_file()

        # It is a real profile, loadable by the normal local path.
        from researcher_profiles import ResearcherProfile

        prof = ResearcherProfile.from_files(installed)
        assert prof.slug == SLUG

    def test_fulltext_never_ships_over_http(self, make_api_client, server_root, cache_root):
        """Copyrighted fulltext is a legal floor: no operator flag can serve it.

        The archive is projected through the caller's tier by
        ``build_viewer_archive``, and ``ALWAYS_RESTRICTED_ROLES`` withholds full
        text at every tier, so there is no server setting that can turn
        redistribution on.
        """
        http = make_api_client(server_root)
        summary = install_profile(SLUG, url="http://testserver", root=cache_root, client=http)

        assert summary["archive_tier"] == "public"
        assert not (cache_root / SLUG / "sources" / "papers").exists()
        # ...but the metadata that lives alongside it still arrives.
        assert (cache_root / SLUG / "profile.jsonld").is_file()

    def test_cache_hit_skips_download(self, make_api_client, server_root, cache_root):
        """Filesystem existence is the cache oracle; a second install is a no-op."""
        http = make_api_client(server_root)
        first = install_profile(SLUG, url="http://testserver", root=cache_root, client=http)
        assert first["status"] == "installed"

        marker = cache_root / SLUG / "LOCAL_EDIT"
        marker.write_text("untouched")

        second = install_profile(SLUG, url="http://testserver", root=cache_root, client=http)

        assert second["status"] == "present"
        assert marker.read_text() == "untouched"

    def test_force_replaces_cached_profile(self, make_api_client, server_root, cache_root):
        http = make_api_client(server_root)
        install_profile(SLUG, url="http://testserver", root=cache_root, client=http)
        marker = cache_root / SLUG / "LOCAL_EDIT"
        marker.write_text("clobber me")

        again = install_profile(
            SLUG, url="http://testserver", root=cache_root, client=http, force=True
        )

        assert again["status"] == "installed"
        assert not marker.exists()

    def test_digest_mismatch_aborts_before_commit(
        self, make_api_client, server_root, cache_root, monkeypatch
    ):
        """A corrupted transfer must not land in the cache, and must clean up."""
        import researcher_profiles.api.routers.push as routes

        real = routes.build_viewer_archive

        def _corrupting(*a, **kw):
            return real(*a, **kw) + b"trailing-garbage"

        # Digest header is computed from the pre-corruption bytes, so the client
        # sees a mismatch.
        monkeypatch.setattr(routes, "build_viewer_archive", lambda *a, **kw: real(*a, **kw))

        http = make_api_client(server_root)
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

    def test_missing_profile_reports_cleanly(self, make_api_client, server_root, cache_root):
        http = make_api_client(server_root)
        with pytest.raises(RuntimeError, match="not found"):
            install_profile(
                "no-such-person",
                url="http://testserver",
                root=cache_root,
                client=http,
            )
        assert not (cache_root / "no-such-person").exists()

    def test_invalid_slug_rejected(self, cache_root):
        with pytest.raises(ValueError, match="invalid slug"):
            install_profile("../escape", url="http://testserver", root=cache_root)

    def test_no_registry_configured(self, cache_root, monkeypatch):
        monkeypatch.delenv("RESEARCHER_PROFILES_REGISTRY_URL", raising=False)
        with pytest.raises(ValueError, match="no registry URL"):
            install_profile(SLUG, root=cache_root)

    def test_seek_and_list(self, make_api_client, server_root, cache_root):
        assert list_installed(cache_root) == []
        with pytest.raises(FileNotFoundError, match="not installed"):
            seek_profile(SLUG, root=cache_root)

        http = make_api_client(server_root)
        install_profile(SLUG, url="http://testserver", root=cache_root, client=http)

        assert list_installed(cache_root) == [SLUG]
        assert seek_profile(SLUG, root=cache_root) == cache_root / SLUG

    def test_archive_endpoint_headers(self, make_api_client, server_root):
        http = make_api_client(server_root)
        resp = http.get(f"/api/v1/profiles/{SLUG}/archive")
        assert resp.status_code == 200
        assert resp.headers["X-RP-Archive-Digest"]
        assert resp.headers["X-RP-Archive-Tier"] == "public"
        assert "attachment" in resp.headers["content-disposition"]

    def test_archive_404_for_unknown_slug(self, make_api_client, server_root):
        http = make_api_client(server_root)
        assert http.get("/api/v1/profiles/nobody/archive").status_code == 404

    # ----------------------------------------------------------------------
    # Spec-whitelist archive builder (build_profile_archive)
    # ----------------------------------------------------------------------

    def test_archive_drops_non_spec_members(self, fixture_profile) -> None:
        """A stale sources/html/ (and other cruft) never reaches the tarball."""
        from researcher_profiles.api.upload import build_profile_archive

        src = fixture_profile(SLUG)
        # Scrape residue + assorted non-spec cruft on disk.
        (src / "sources" / "html").mkdir(parents=True, exist_ok=True)
        (src / "sources" / "html" / "big.html").write_text("<html>" + "x" * 100000 + "</html>")
        (src / "sources" / "works.json").write_text("[]")  # non-spec file
        (src / "scratch.txt").write_text("not a profile member")

        names = _tar_names(build_profile_archive(src, include_fulltext=True))

        # No non-spec member survives.
        assert not any(n.startswith("sources/html") for n in names)
        assert "sources/works.json" not in names
        assert "scratch.txt" not in names
        # Documented members are kept.
        assert "profile.jsonld" in names
        # Pushing a profile publishes the RECORD, not the build: the sidecar with
        # download attempts, rejection reasons, and verification bookkeeping never
        # leaves this machine.
        assert "meta/build_state.json" not in names
        assert not any(
            n.startswith("meta/") and n != "meta" for n in names if n != "meta/embeddings.sqlite"
        )
        assert any(n.startswith("sources/papers/") for n in names)
        assert "sources/papers.jsonld" in names
        assert any(n.startswith("sources/summaries/") for n in names)

    @pytest.mark.parametrize(
        "archive_kwargs, expect_papers",
        [
            ({"include_fulltext": True}, True),
            ({"include_fulltext": False}, False),
            # The shared builder's *default* is the safe one, not the permissive
            # one. Every archive that leaves this machine is built here; a caller
            # who says nothing about fulltext must not end up redistributing it.
            ({}, False),
        ],
        ids=["include-fulltext", "exclude-fulltext", "default-no-kwarg"],
    )
    def test_archive_fulltext_gate(self, fixture_profile, archive_kwargs, expect_papers) -> None:
        """The gate is copyright-scoped: it drops sources/papers/ and nothing else."""
        from researcher_profiles.api.upload import build_profile_archive

        src = fixture_profile(SLUG)

        names = _tar_names(build_profile_archive(src, **archive_kwargs))

        assert any(n.startswith("sources/papers/") for n in names) is expect_papers
        # Scoped to sources/papers/ only; summaries + metadata always remain.
        assert "profile.jsonld" in names
        assert "sources/papers.jsonld" in names
        assert any(n.startswith("sources/summaries/") for n in names)


class TestIdentityResolve:
    """POST /api/v1/identity/resolve, on both store backends.

    The matching pipeline itself (rid-first, cautious names, deterministic
    mint-on-miss) is rp-sdk's to prove and is pinned exhaustively in
    ``test_resolve.py``. What belongs here is the wire: the route dispatches
    into ``resolve_person`` and reports its outcome faithfully, the
    ``resolve`` scope gate (or, on bare rp-sdk, its operator-token fallback)
    actually runs in front of it, and ``client.resolve_rid`` round-trips
    against a real server.
    """

    JANE_ORCID = "0000-0002-1825-0097"
    RESOLVE = "/api/v1/identity/resolve"

    @pytest.fixture(params=["filesystem", "sql"])
    def store(self, request, tmp_path):
        """One profile (Jane, at her ORCID), once per backend."""
        from researcher_profiles.store import FilesystemProfileStore
        from researcher_profiles.store.sql import SqlProfileStore

        if request.param == "filesystem":
            s = FilesystemProfileStore(tmp_path)
        else:
            s = SqlProfileStore("sqlite://")
            s.create_all()
        s.create(
            ProfileDocument(name="Jane A. Doe", rid=self.JANE_ORCID, provenance="third_party"),
            slug="jane-doe",
        )
        return s

    def test_no_credential_is_401(self, make_api_client, store):
        c = make_api_client(store, token="op-secret")
        assert c.post(self.RESOLVE, json={"rid": self.JANE_ORCID}).status_code == 401

    def test_the_operator_token_falls_back_to_the_scope(self, make_api_client, store):
        """Bare rp-sdk has no consumer layer, so ``require_scope`` falls back to
        the operator token. That fallback is what this test pins, not the
        scope model itself (there is no consumer layer here to model)."""
        c = make_api_client(store, token="op-secret")
        r = c.post(
            self.RESOLVE,
            json={"rid": self.JANE_ORCID},
            headers={"Authorization": "Bearer op-secret"},
        )
        assert r.status_code == 200, r.text

    def test_an_orcid_hit_is_exact_and_creates_nothing(self, make_api_client, store):
        c = make_api_client(store)
        r = c.post(self.RESOLVE, json={"rid": self.JANE_ORCID})
        assert r.status_code == 200, r.text
        assert r.json() == {"rid": self.JANE_ORCID, "created": False, "confidence": "exact"}

    def test_a_true_miss_mints_once_and_is_idempotent(self, make_api_client, store):
        """The load-bearing property: two resolves, one rid, one stub."""
        c = make_api_client(store)
        first = c.post(self.RESOLVE, json={"name": "Grace Hopper"})
        assert first.status_code == 201, first.text
        rid = first.json()["rid"]
        assert rid.startswith("local:")
        assert first.json()["created"] is True

        second = c.post(self.RESOLVE, json={"name": "Hopper, Grace"})
        assert second.status_code == 200
        assert second.json() == {"rid": rid, "created": False, "confidence": "high"}

    def test_an_initials_only_name_defers_with_candidates(self, make_api_client, store):
        """Two full-name neighbors sharing a coarse fold, and an initials-only
        query that cannot distinguish them: the resolver defers rather than
        guessing, naming both as candidates."""
        store.create(
            ProfileDocument(
                name="John Doe",
                rid="0000-0004-4600-113X",
                provenance="third_party",
                affiliation="Example University",
            ),
            slug="doe-john",
        )
        c = make_api_client(store)
        r = c.post(self.RESOLVE, json={"name": "J. Doe"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rid"] is None
        assert body["confidence"] == "low"
        assert {(cand["rid"], cand["name"]) for cand in body["candidates"]} == {
            (self.JANE_ORCID, "Jane A. Doe"),
            ("0000-0004-4600-113X", "John Doe"),
        }
        assert all({"name", "affiliation"} <= cand.keys() for cand in body["candidates"])

    def test_neither_rid_nor_name_is_a_400(self, make_api_client, store):
        c = make_api_client(store)
        assert c.post(self.RESOLVE, json={}).status_code == 400

    def test_client_round_trip_against_a_real_server(self, make_api_client, store):
        """``resolve_rid`` is the consumer-side half; this is the test the
        deleted function never had."""
        c = make_api_client(store)

        minted = resolve_rid("http://testserver", name="Ada Lovelace", client=c)
        assert minted.rid.startswith("local:")
        assert minted.created is True

        deferred = resolve_rid("http://testserver", name="J. Doe", client=c)
        assert deferred.rid is None
        assert deferred.confidence == "low"
        assert all(isinstance(cand, Candidate) for cand in deferred.candidates)
        assert {cand.rid for cand in deferred.candidates} == {self.JANE_ORCID}


class TestPushCopyrightBoundary:
    """A publisher PDF must not travel, and the server must not rely on the client."""

    # ----------------------------------------------------------------------
    # Push-side copyright boundary
    # ----------------------------------------------------------------------

    @pytest.fixture
    def push_src(self, fixture_profile) -> Path:
        """A local profile directory carrying a copyrighted fulltext PDF."""
        src = fixture_profile(SLUG)
        _add_fulltext_pdf(src)
        return src

    @pytest.fixture
    def push_target(self, tmp_path: Path) -> Path:
        root = tmp_path / "push-target"
        root.mkdir()
        return root

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
        """Push ships the PDF only when the pushing client explicitly opts in."""
        names = _tar_names(_captured_push_bytes(push_src, **push_kwargs))

        if expect_fulltext:
            assert "sources/papers/paper-001.pdf" in names
        else:
            assert not any(n.startswith("sources/papers/") for n in names)
            assert "sources/papers/paper-001.pdf" not in names
        # The rest of the profile still goes, either way.
        assert "profile.jsonld" in names
        assert "sources/papers.jsonld" in names

    @pytest.mark.parametrize(
        "client_kwargs, expect_stored_fulltext",
        [
            # Server-side enforcement: a client that sends fulltext anyway is
            # trimmed. The boundary must not depend on the client behaving.
            ({}, False),
            # accept_fulltext=True is the operator's call: what the registry STORES.
            ({"accept_fulltext": True}, True),
        ],
        ids=["strips-on-ingest", "accepts-when-operator-opts-in"],
    )
    def test_server_ingest_fulltext_gate(
        self,
        make_api_client,
        push_src: Path,
        push_target: Path,
        client_kwargs,
        expect_stored_fulltext,
    ) -> None:
        """What the server keeps on ingest is the operator's call, not the client's."""
        from researcher_profiles.api.upload import build_profile_archive

        payload = build_profile_archive(push_src, include_fulltext=True)
        assert "sources/papers/paper-001.pdf" in _tar_names(payload)  # client sent it

        http = make_api_client(push_target, **client_kwargs)
        resp = http.put(
            f"/api/v1/profiles/{SLUG}",
            content=payload,
            headers={"Content-Type": "application/gzip"},
        )

        assert resp.status_code == 200
        stored = push_target / SLUG
        pdf = stored / "sources" / "papers" / "paper-001.pdf"
        if expect_stored_fulltext:
            assert pdf.is_file()
        else:
            assert not pdf.exists()
        assert (stored / "profile.jsonld").is_file()
        # Non-fulltext members survive either way.
        assert (stored / "sources" / "papers.jsonld").is_file()


# --------------------------------------------------------------------------
# POST /api/v1/match
# --------------------------------------------------------------------------


class _FakeMatch:
    """Duck-types ``store.match`` for the endpoint mapping test."""

    def __init__(self, matches, calls):
        self._matches = matches
        self._calls = calls

    def rank(self, text, **kwargs):
        self._calls.append((text, kwargs))
        return self._matches


class _FakeVectorStore:
    """Duck-types a ``VectorStore`` far enough for ``/match`` to run."""

    def __init__(self, matches, slugs=("jane-doe",)):
        self.calls = []
        self.match = _FakeMatch(matches, self.calls)
        self._slugs = list(slugs)

    def list_slugs(self):
        return list(self._slugs)


def _fake_match(slug="jane-doe", name="Jane Doe", orcid="0000-0002-1825-0097", score=0.83):
    # A ranked profile answers for its own privacy tier, exactly as a real
    # ResearcherProfile does: /match projects every result through it.
    prof = SimpleNamespace(
        slug=slug, name=name, orcid=orcid, metadata=SimpleNamespace(visibility="public")
    )
    ev = MatchEvidence(
        top_chunks=[
            SearchHit(
                text="a chunk",
                source_type="paper_summary",
                source_id="paper1",
                chunk_index=0,
                section=None,
                cosine=0.8,
                score=0.9,
                meta={},
            )
        ],
        top_papers=["paper1"],
        overlapping_topics=["genomics"],
        centroid_score=0.71,
    )
    return Match(profile=prof, score=score, evidence=ev)


class TestMatchEndpoint:
    """Tests for the /api/v1/match endpoint.

    The mapping + include_chunks behavior is exercised with a stub store
    injected through ``app.dependency_overrides[get_match_store]`` (no
    embeddings needed). The 503 paths are exercised by pointing the dependency
    at stores that cannot rank. A real-ranking end-to-end check lives in
    ``tests/integration/test_real_profiles.py::TestApiMatch``.
    """

    @staticmethod
    def _use(app, vstore):
        """Serve ``/match`` off ``vstore``. Returns it, for the call assertions."""
        from researcher_profiles.api.deps import get_match_store

        app.dependency_overrides[get_match_store] = lambda: vstore
        return vstore

    def test_match_maps_results_and_omits_chunks_by_default(self, api_client):
        vstore = self._use(api_client.app, _FakeVectorStore([_fake_match()]))
        r = api_client.post("/api/v1/match", json={"query": "chromatin", "k": 3})
        assert r.status_code == 200
        body = r.json()
        assert len(body["matches"]) == 1
        m = body["matches"][0]
        assert m["slug"] == "jane-doe"
        assert m["name"] == "Jane Doe"
        assert m["orcid"] == "0000-0002-1825-0097"
        assert m["score"] == pytest.approx(0.83)
        assert m["evidence"]["centroid_score"] == pytest.approx(0.71)
        assert m["evidence"]["top_papers"] == ["paper1"]
        assert m["evidence"]["overlapping_topics"] == ["genomics"]
        # include_chunks defaults to False -> chunks omitted.
        assert m["evidence"]["top_chunks"] == []
        # The manager received the forwarded ranking params.
        assert vstore.calls[0][0] == "chromatin"
        assert vstore.calls[0][1]["k"] == 3

    def test_match_includes_chunks_when_requested(self, api_client):
        self._use(api_client.app, _FakeVectorStore([_fake_match()]))
        r = api_client.post(
            "/api/v1/match",
            json={"query": "chromatin", "include_chunks": True},
        )
        assert r.status_code == 200
        chunks = r.json()["matches"][0]["evidence"]["top_chunks"]
        assert len(chunks) == 1
        assert chunks[0]["source_id"] == "paper1"
        assert chunks[0]["score"] == pytest.approx(0.9)

    def test_match_503_when_no_profile_is_indexed(self, api_client):
        """Profiles exist and none has vectors: 503, never an empty 200.

        An empty ranking here is indistinguishable from "nobody matched", which
        is exactly the failure this replaces. The fixture profiles ship no
        embedding index, so the real dependency runs and refuses.
        """
        r = api_client.post("/api/v1/match", json={"query": "x"})
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert "matching unavailable" in detail
        assert "none has a usable embedding index" in detail

    def test_match_503_when_the_store_cannot_serve_vectors(self, api_client):
        """One actionable error, carrying what to do about it."""

        class _NoVectors:
            location = "memory://metadata-only"
            root = None

            def list_slugs(self):
                return ["jane-doe"]

        api_client.app.state.store = _NoVectors()
        r = api_client.post("/api/v1/match", json={"query": "x"})
        assert r.status_code == 503
        detail = r.json()["detail"]
        assert "matching unavailable" in detail
        assert "serve vectors" in detail


# --------------------------------------------------------------------------
# StaticArtifactStorage: a published directory over HTTP
# --------------------------------------------------------------------------


BASE = "https://example.org/profiles/jane-doe"


class TestStaticClient:
    """Tests for ``StaticArtifactStorage``: ``from_url`` and URL-dispatching
    ``from_files`` against a statically hosted profile directory.

    The "static host" is an httpx.MockTransport serving the jane-doe fixture
    directory file-for-file, which is exactly what a published profile is.
    """

    @pytest.fixture
    def local(self, jane_doe_readonly_dir):
        """The directory the mock host serves: a published profile is a directory."""
        return jane_doe_readonly_dir

    @pytest.fixture
    def http(self, local) -> httpx.Client:
        return httpx.Client(transport=_static_transport(local, BASE))

    def test_from_url_factory(self, http):
        prof = ResearcherProfile.from_url(BASE, client=http)
        assert isinstance(prof.storage, StaticArtifactStorage)
        assert prof.slug == "jane-doe"
        assert "Doe" in prof.name
        assert len(prof.papers) > 0
        assert prof.expertise.strip()
        assert prof.soul.strip()

    def test_parity_with_from_files(self, http, local):
        local = ResearcherProfile.from_files(local)
        remote = ResearcherProfile.from_url(BASE, client=http)
        assert remote.name == local.name
        assert remote.rid == local.rid
        assert remote.level == local.level
        assert len(remote.papers) == len(local.papers)
        assert remote.papers[0].paper_id == local.papers[0].paper_id
        assert remote.expertise == local.expertise
        assert remote.soul == local.soul
        assert remote.citations == local.citations
        assert sorted(remote.summaries) == sorted(local.summaries)

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

    def test_local_only_methods_raise(self, http):
        prof = ResearcherProfile.from_url(BASE, client=http)
        with pytest.raises(NotImplementedError):
            prof.index.search("anything")
        with pytest.raises(NotImplementedError):
            prof.index.build()
        with pytest.raises(NotImplementedError):
            prof.validate()

    def test_read_parity_and_refusals(self, http, local):
        """The static half of the same contract: reads agree, writes refuse."""
        disk = ResearcherProfile.from_files(local)
        published = ResearcherProfile.from_url(BASE, client=http)
        assert published.content_hash() == disk.content_hash()
        assert published.metadata.rid == disk.metadata.rid
        with pytest.raises(ProfileWriteError):
            published.save_soul("# not yours\n")
        with pytest.raises(CapabilityUnavailableError):
            published.index.build()

    def test_missing_profile_jsonld_raises(self, http):
        prof = ResearcherProfile.from_url(
            "https://example.org/profiles/jane-doe/nonexistent", client=http
        )
        with pytest.raises(ProfileLoadError):
            _ = prof.metadata


# --------------------------------------------------------------------------
# POST /api/v1/profiles/{slug}/rank-works
# --------------------------------------------------------------------------


class TestRankWorksEndpoint:
    """The inverse of /match: candidate works ranked against one profile.

    Ranking is stubbed through ``routers.search._rank_works`` (the same hook the 503
    guard lives behind), so no embedding index is needed; the OpenAlex mode
    stubs ``openalex.fetch_new_works`` at its import site.
    """

    @pytest.fixture
    def rank_stub(self, monkeypatch):
        """Replace the ranking function; record what the route passed it."""
        import researcher_profiles.api.routers.search as routes_mod
        from researcher_profiles.models.results import RankedWork

        received = {}

        def fake_rank(prof, works, **kwargs):
            received["profile"] = prof
            received["works"] = works
            received["kwargs"] = kwargs
            return [RankedWork(work=works[0], score=0.91, evidence=["genomics"])]

        monkeypatch.setattr(routes_mod, "_rank_works", lambda: fake_rank)
        return received

    def test_rank_workss_supplied_works(self, api_client, rank_stub):
        r = api_client.post(
            f"/api/v1/profiles/{SLUG}/rank-works",
            json={
                "works": [{"title": "A new candidate", "year": 2026, "doi": "10.1/x"}],
                "k": 3,
                "threshold": 0.4,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["slug"] == SLUG
        assert body["rid"]
        assert len(body["works"]) == 1
        w = body["works"][0]
        assert w["title"] == "A new candidate"
        assert w["doi"] == "10.1/x"
        assert w["score"] == pytest.approx(0.91)
        assert w["evidence"] == ["genomics"]
        # The wire params reached the ranking function.
        assert rank_stub["kwargs"]["k"] == 3
        assert rank_stub["kwargs"]["threshold"] == pytest.approx(0.4)
        assert rank_stub["works"][0].title == "A new candidate"

    def test_rank_works_openalex_mode_fetches_candidates(self, api_client, rank_stub, monkeypatch):
        import researcher_profiles.openalex as oa
        from researcher_profiles.schema import PaperRecord

        fetch_calls = {}

        def fake_fetch(**kwargs):
            fetch_calls.update(kwargs)
            return [PaperRecord(title="Fetched work", year=2026)]

        monkeypatch.setattr(oa, "fetch_new_works", fake_fetch)
        r = api_client.post(
            f"/api/v1/profiles/{SLUG}/rank-works",
            json={"use_openalex": True, "since": "2026-07-01", "mailto": "who@example.org"},
        )
        assert r.status_code == 200
        assert r.json()["works"][0]["title"] == "Fetched work"
        # Seeds came from the profile document; request params were forwarded.
        assert fetch_calls["since"] == "2026-07-01"
        assert fetch_calls["mailto"] == "who@example.org"
        assert "synthetic data science" in fetch_calls["topics"]

    def test_rank_works_400_without_candidates(self, api_client, rank_stub):
        r = api_client.post(f"/api/v1/profiles/{SLUG}/rank-works", json={})
        assert r.status_code == 400

    def test_rank_works_404_for_unknown_profile(self, api_client, rank_stub):
        r = api_client.post(
            "/api/v1/profiles/nobody/rank-works", json={"works": [{"title": "x", "year": 2026}]}
        )
        assert r.status_code == 404

    def test_rank_works_503_when_embeddings_missing(self, api_client, monkeypatch):
        """A core-only server (no vectors extra) degrades, never 500s."""
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "researcher_profiles.embeddings.rank", None)
        r = api_client.post(
            f"/api/v1/profiles/{SLUG}/rank-works", json={"works": [{"title": "x", "year": 2026}]}
        )
        assert r.status_code == 503
        assert "matching unavailable" in r.json()["detail"]


# ---------------------------------------------------------------------------
# The pre-commit hook, through the HTTP surface
# ---------------------------------------------------------------------------


class TestPreCommitHookOverHTTP:
    """A failing hook is a server fault, not a bad patch.

    ``WriteHookError`` is not a ``ProfileWriteError``, so it
    propagates through ``edit.py`` uncaught instead of becoming an ``EditError``
    that the route maps to 400. This is exactly the distinction that gets lost
    during implementation, so it is pinned here.
    """

    def test_a_raising_hook_yields_500_not_400(self, make_api_client, fixture_profiles_root):
        def boom(ctx):
            raise RuntimeError("dependent state unreachable")

        root = fixture_profiles_root(SLUG)
        c = make_api_client(root, pre_commit_hooks=[boom])
        with pytest.raises(Exception) as excinfo:
            c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Genomics"})
        # TestClient re-raises server exceptions; what matters is that it is the
        # hook error and not an EditError-shaped 400.
        assert "boom" in str(excinfo.value) or "pre-commit hook" in str(excinfo.value)

    def test_a_raising_hook_yields_500_with_a_real_server(
        self, make_api_client, fixture_profiles_root
    ):
        from fastapi.testclient import TestClient

        from researcher_profiles.api.app import create_app

        def boom(ctx):
            raise RuntimeError("dependent state unreachable")

        from researcher_profiles.store import FilesystemProfileStore

        root = fixture_profiles_root(SLUG)
        app = create_app(FilesystemProfileStore(root), pre_commit_hooks=[boom])
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Genomics"})
        assert r.status_code == 500

    def test_a_bad_patch_is_still_400(self, make_api_client, fixture_profiles_root):
        seen = []
        root = fixture_profiles_root(SLUG)
        c = make_api_client(root, pre_commit_hooks=[seen.append])
        r = c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"rid": "0000-0002-1825-0097"})
        assert r.status_code == 400
        assert seen == []

    def test_edit_endpoints_fire_the_hook(self, make_api_client, fixture_profiles_root):
        seen = []
        root = fixture_profiles_root(SLUG)
        c = make_api_client(root, pre_commit_hooks=[seen.append])
        assert (
            c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Genomics"}).status_code
            == 200
        )
        assert c.put(f"/api/v1/profiles/{SLUG}/soul", json={"soul": "# soul\n"}).status_code == 200
        assert [ctx.kind for ctx in seen] == ["document", "soul"]

    def test_invalidate_after_write_no_longer_calls_any_hook(
        self, make_api_client, fixture_profiles_root
    ):
        """The cache half stayed; the hook half moved off it entirely."""
        from researcher_profiles.api import invalidate_after_write
        from researcher_profiles.api.deps import get_store

        seen = []
        root = fixture_profiles_root(SLUG)
        c = make_api_client(root, pre_commit_hooks=[seen.append])
        request = SimpleNamespace(app=c.app)
        store = get_store(request)
        before = store.generation
        invalidate_after_write(request, store, SLUG)
        assert seen == []
        # The cache half stayed, and it is the write generation now: bumping it
        # is the whole in-process invalidation for ranking.
        assert store.generation > before

    def test_a_write_that_never_touches_a_route_still_fires(self, fixture_profiles_root):
        from researcher_profiles.store import FilesystemProfileStore

        seen = []
        cache = FilesystemProfileStore(fixture_profiles_root(SLUG))
        cache.add_pre_commit_hook(seen.append)
        cache.get(SLUG).save_soul("# written with no HTTP anywhere\n")
        assert [ctx.kind for ctx in seen] == ["soul"]
        assert seen[0].request is None
