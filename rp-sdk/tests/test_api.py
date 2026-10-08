"""The served profile's HTTP app: the read, generative, search, identity and edit routes,
auth and tier projection, and the registry proofs attached at read time.

Push and archive live in test_push.py; the clients that speak to this app live in
test_client.py.
"""

import io
import json
import tarfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from researcher_profiles import LLMClient, ResearcherProfile
from researcher_profiles.client import (
    resolve_rid,
)
from researcher_profiles.embeddings.cache import SearchHit
from researcher_profiles.models.results import Match, MatchEvidence
from researcher_profiles.resolve import Candidate
from researcher_profiles.schema import ProfileDocument, Proof
from researcher_profiles.store.db import ProfileRow
from researcher_profiles.store.sql import SqlProfileStore

from .factories import (
    build_profile_dir,
    fake_llm_response,
    remote_from_app,
)

# --------------------------------------------------------------------------
# The server: routes, auth, and tier projection
# --------------------------------------------------------------------------


SLUG = "jane-doe"
_PROOF_ORCID = "0000-0002-1825-0097"
_PROOF_LOCAL = "local:lee-local-abc123"
_PROOF_ISSUER = "https://registry.example"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    """The HTTP surface itself: routes, auth, and error mapping."""

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
        assert d["view"] == "record"
        assert d["fields"]["name"]
        # The record surfaces the depth tier and the edit version...
        assert d["fields"]["level"] == "full"
        assert d["content_hash"].startswith("sha256:")
        # ...and sizes, never bodies or the manifest.
        assert d["parts"]["soul"]["available"] is True
        assert "soul" not in d and "manifest" not in d
        assert "has_part" not in d["fields"]
        full = api_client.get(f"/api/v1/profiles/{SLUG}", params={"view": "full"}).json()
        assert isinstance(full["expertise"], str)
        assert isinstance(full["soul"], str)
        assert "has_part" not in full["fields"]

    def test_profile_detail_revalidates_on_its_version(self, api_client):
        """One stored-column read answers "has it changed?" with a 304."""
        r = api_client.get(f"/api/v1/profiles/{SLUG}")
        etag = r.headers["ETag"]
        assert etag.startswith('W/"')
        again = api_client.get(f"/api/v1/profiles/{SLUG}", headers={"If-None-Match": etag})
        assert again.status_code == 304
        full = api_client.get(
            f"/api/v1/profiles/{SLUG}", params={"view": "full"}, headers={"If-None-Match": etag}
        )
        assert full.status_code == 200

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
        page = client.get(f"/api/v1/profiles/{SLUG}/papers").json()
        by_id = {p["paper_id"]: p for p in page["items"]}
        served = by_id["doe2016example"]
        assert served["doi"] == "10.1234/example.2016"
        assert served["openalex_id"] == "W2000000001"
        record = client.get(
            f"/api/v1/profiles/{SLUG}/papers/doe2016example", params={"view": "full"}
        ).json()
        assert record["fields"]["pmid"] == "27000000"
        assert record["fields"]["author"] == ["Jane A. Doe", "John Roe"]

        # ...and the SDK client rebuilds a PaperRecord that still carries them,
        # so a consumer reading a hosted profile is not worse off than one
        # reading the directory.
        remote = remote_from_app(client.app)
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
        page = r.json()
        papers = page["items"]
        # The jane-doe fixture carries 8 papers, 5 of which have a summary file
        # in sources/summaries/. Asserting the join unconditionally is the point:
        # `if papers:` let this pass on an empty response.
        by_id = {p["paper_id"]: p for p in papers}
        assert len(by_id) == 8
        assert (page["total"], page["limit_applied"], page["has_more"]) == (8, 20, False)
        assert by_id["doe2016example"]["title"] == (
            "ExampleOverlap: enrichment analysis of example region sets"
        )
        assert by_id["doe2016example"]["year"] == 2016
        # The summary size is a real join against sources/summaries/, not a
        # constant: 5 of the 8 papers have one.
        assert by_id["doe2016example"]["summary"]["available"] is True
        assert by_id["doe2016example"]["summary"]["bytes"] > 0
        assert by_id["roe2018toolkit"]["summary"] == {"available": False, "reason": "none"}
        assert sum(p["summary"]["available"] for p in papers) == 5
        assert all(len(p["version"]) == 16 for p in papers)

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
        # widens the caller's tier to ``private``; it does not unlock a door.
        c = make_api_client(fixture_profiles_root(SLUG), token="secret-token")
        anon = c.get("/api/v1/profiles")
        assert anon.status_code == 200
        assert anon.headers["X-RP-Viewer-Tier"] == "public"
        operator = c.get(
            "/api/v1/profiles",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert operator.status_code == 200
        assert operator.headers["X-RP-Viewer-Tier"] == "private"

    def test_a_private_profile_is_withheld_from_anonymous_but_not_the_operator(
        self, make_api_client, fixture_profiles_root, tmp_path
    ):
        # The private-registry posture, expressed per profile: the document
        # itself declares ``limited``, so an anonymous caller gets the same 404
        # a nonexistent slug gets, and the operator gets the profile.
        import shutil

        root = tmp_path / "private-profiles"
        root.mkdir()
        shutil.copytree(fixture_profiles_root(SLUG) / SLUG, root / SLUG)
        prof = ResearcherProfile.from_files(root / SLUG)
        doc = prof.metadata
        doc.visibility = "limited"
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
        # Stub the server's chunk search.
        import researcher_profiles.api._semantic as semantic
        from researcher_profiles.embeddings.cache import SearchHit

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
        monkeypatch.setattr(
            semantic, "search_chunks", lambda *a, **kw: (fake_hits[: kw["k"]], None)
        )

        with TestClient(api_client.app) as c:
            r = c.post(
                f"/api/v1/profiles/{SLUG}/search",
                json={"query": "chromatin", "k": 300},
            )
        assert r.status_code == 200
        j = r.json()
        assert len(j["hits"]) == 1
        assert j["hits"][0]["source_id"] == "paper1"
        assert j["k_applied"] == 20

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

    @pytest.mark.parametrize(
        "path, body, response_text",
        [(p, {**b, "k": 500}, t) for p, b, t in _persona_params(SLUG)],
        ids=PERSONA_IDS,
    )
    def test_persona_k_is_clamped_and_reported(self, api_client, path, body, response_text):
        _stub_llm_slug(api_client.app, SLUG, response_text)
        with TestClient(api_client.app) as c:
            r = c.post(path, json=body)
        assert r.status_code == 200, r.text
        assert r.json()["k_applied"] == 20

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


def _proof(when: str = "2026-09-01T12:00:00+00:00") -> Proof:
    return Proof(kind="orcid_login", issuer=_PROOF_ISSUER, orcid=_PROOF_ORCID, verifiedAt=when)


def _login_proofs(doc: dict, key: str = "proof") -> list[dict]:
    return [p for p in doc.get(key, []) if p["kind"] == "orcid_login"]


class TestRegistryProofsServed:
    """Registry-issued proofs are attached when a document is served.

    ``app.state.hooks.registry_proofs`` computes them per read. Both ``profile.jsonld``
    URLs and the detail payload carry them; the archive does not; a stored copy is
    never served; without the hook the stored bytes go out verbatim.
    """

    @pytest.fixture
    def store(self):
        s = SqlProfileStore("sqlite://")
        s.create_all()
        s.create(
            ProfileDocument(name="Ada Lovelace", rid=_PROOF_ORCID, provenance="third_party"),
            slug="ada",
        )
        return s

    @pytest.fixture
    def calls(self):
        return []

    @pytest.fixture
    def client(self, store, make_api_client, calls):
        c = make_api_client(store)
        state = {"when": "2026-09-01T12:00:00+00:00"}

        def hook(rid):
            calls.append(rid)
            return [_proof(state["when"])] if rid == _PROOF_ORCID else []

        c.app.state.hooks.registry_proofs = hook
        c.hook_state = state
        return c

    def test_all_three_reads_carry_the_proof_once(self, client):
        for url in (
            "/api/v1/profiles/ada/profile.jsonld",
            "/api/v1/profiles/ada/content/profile.jsonld",
        ):
            r = client.get(url)
            assert r.status_code == 200, r.text
            proofs = _login_proofs(r.json())
            assert proofs == [
                {
                    "kind": "orcid_login",
                    "issuer": _PROOF_ISSUER,
                    "orcid": _PROOF_ORCID,
                    "verifiedAt": "2026-09-01T12:00:00+00:00",
                }
            ]
        detail = client.get("/api/v1/profiles/ada", params={"view": "full"}).json()
        assert [p["kind"] for p in detail["fields"]["proof"]] == ["orcid_login"]

    def test_both_urls_same_bytes_and_etag_and_304(self, client):
        a = client.get("/api/v1/profiles/ada/profile.jsonld")
        b = client.get("/api/v1/profiles/ada/content/profile.jsonld")
        assert a.content == b.content
        assert a.headers["etag"] == b.headers["etag"]
        r = client.get(
            "/api/v1/profiles/ada/content/profile.jsonld",
            headers={"If-None-Match": a.headers["etag"]},
        )
        assert r.status_code == 304

    def test_hook_change_changes_etag(self, client):
        before = client.get("/api/v1/profiles/ada/profile.jsonld").headers["etag"]
        client.hook_state["when"] = "2026-09-02T12:00:00+00:00"
        after = client.get("/api/v1/profiles/ada/profile.jsonld").headers["etag"]
        assert before != after

    def test_stored_copy_is_not_served(self, client, store):
        forged = {**_proof().model_dump(mode="json"), "issuer": "https://evil.example"}
        with store.session() as s:
            row = s.get(ProfileRow, _PROOF_ORCID)
            row.document = {**row.document, "proof": [forged]}
            s.add(row)
            s.commit()
        store.evict("ada")
        doc = client.get("/api/v1/profiles/ada/profile.jsonld").json()
        assert [p["issuer"] for p in _login_proofs(doc)] == [_PROOF_ISSUER]

    def test_no_hook_serves_stored_bytes_verbatim(self, store, make_api_client):
        c = make_api_client(store)
        r = c.get("/api/v1/profiles/ada/profile.jsonld")
        assert r.content == store.document_bytes("ada")
        assert c.get("/api/v1/profiles/ada/content/profile.jsonld").content == r.content

    def test_no_hook_still_strips_a_stored_copy(self, store, make_api_client):
        forged = {**_proof().model_dump(mode="json"), "issuer": "https://evil.example"}
        with store.session() as s:
            row = s.get(ProfileRow, _PROOF_ORCID)
            row.document = {**row.document, "proof": [forged]}
            s.add(row)
            s.commit()
        store.evict("ada")
        c = make_api_client(store)
        for url in (
            "/api/v1/profiles/ada/profile.jsonld",
            "/api/v1/profiles/ada/content/profile.jsonld",
        ):
            r = c.get(url)
            assert r.status_code == 200, r.text
            assert _login_proofs(r.json()) == []

    def test_raising_hook_is_a_200_without_proof(self, store, make_api_client):
        c = make_api_client(store)

        def boom(rid):
            raise RuntimeError("nope")

        c.app.state.hooks.registry_proofs = boom
        r = c.get("/api/v1/profiles/ada/profile.jsonld")
        assert r.status_code == 200
        assert _login_proofs(r.json()) == []
        assert c.get("/api/v1/profiles/ada").status_code == 200

    def test_archive_carries_no_registry_proof(self, client):
        r = client.get("/api/v1/profiles/ada/archive")
        assert r.status_code == 200, r.text
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
            doc = json.loads(tf.extractfile("profile.jsonld").read())
        assert _login_proofs(doc) == []

    def test_section_projection_keeps_the_proof(self, client, store):
        from researcher_profiles.schema import SectionVisibility

        prof = store.get("ada")
        doc = prof.metadata
        doc.summary = "A private summary."
        doc.section_visibility = [SectionVisibility(section="summary", visibility="private")]
        prof.save_profile(doc)
        store.evict("ada")
        for url in (
            "/api/v1/profiles/ada/profile.jsonld",
            "/api/v1/profiles/ada/content/profile.jsonld",
        ):
            served = client.get(url).json()
            assert "summary" not in served
            assert len(_login_proofs(served)) == 1

    def test_retired_alias_serves_the_survivor_proof(self, tmp_path, make_api_client, calls):
        store = SqlProfileStore("sqlite://")
        store.create_all()
        store.create(
            ProfileDocument(name="Lee", rid=_PROOF_LOCAL, provenance="synthetic"), slug="lee"
        )
        store.create(
            ProfileDocument(name="Ada", rid=_PROOF_ORCID, provenance="third_party"), slug="ada"
        )
        staged = build_profile_dir(
            tmp_path / "stage",
            name="Ada",
            rid=_PROOF_ORCID,
            level="lite",
            papers=False,
            personality=False,
            summaries=False,
        )
        store.merge_into(
            "lee", staged, survivor_rid=_PROOF_ORCID, survivor_slug="ada", build_missing_index=False
        )
        c = make_api_client(store)
        c.app.state.hooks.registry_proofs = lambda rid: (
            calls.append(rid) or ([_proof()] if rid == _PROOF_ORCID else [])
        )
        doc = c.get("/api/v1/profiles/lee/profile.jsonld").json()
        assert doc["rid"] == _PROOF_ORCID
        assert len(_login_proofs(doc)) == 1
        assert calls and set(calls) == {_PROOF_ORCID}


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

    def test_match_counts_are_clamped_and_reported(self, api_client):
        """Clamp and report: an oversized ``k`` runs at the cap, never a 422."""
        vstore = self._use(api_client.app, _FakeVectorStore([_fake_match()]))
        r = api_client.post(
            "/api/v1/match",
            json={"query": "chromatin", "k": 10_000, "prefilter": 10_000, "topk_chunks": 99},
        )
        assert r.status_code == 200, r.text
        assert (r.json()["k_applied"], r.json()["prefilter_applied"]) == (50, 200)
        sent = vstore.calls[0][1]
        assert (sent["k"], sent["prefilter"], sent["topk_chunks"]) == (50, 200, 10)

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

    def test_match_forwards_interests_and_reports_matched_topics(self, api_client):
        match = _fake_match()
        match.evidence.matched_topics = ["T10222"]
        vstore = self._use(api_client.app, _FakeVectorStore([match]))
        interest = {
            "concept": {
                "@id": "https://openalex.org/T10222",
                "system": "https://openalex.org/topics",
                "code": "T10222",
                "display": "Genomics and Chromatin Dynamics",
            },
            "weight": 1,
            "method": "declared",
            "generator": "user",
            "assertedAt": "2026-09-25T12:00:00Z",
        }
        r = api_client.post("/api/v1/match", json={"query": "chromatin", "interests": [interest]})
        assert r.status_code == 200
        assert r.json()["matches"][0]["evidence"]["matched_topics"] == ["T10222"]
        [forwarded] = vstore.calls[0][1]["interests"]
        assert forwarded.concept.code == "T10222" and forwarded.weight == 1
        assert vstore.calls[0][1]["topic_alpha"] == pytest.approx(0.2)

    def test_match_refuses_malformed_interests(self, api_client):
        self._use(api_client.app, _FakeVectorStore([_fake_match()]))
        r = api_client.post(
            "/api/v1/match",
            json={"query": "chromatin", "interests": [{"concept": {"label": "x"}}]},
        )
        assert r.status_code == 422

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

        def fake_fetch(client, **kwargs):
            fetch_calls["client"] = client
            fetch_calls.update(kwargs)
            return [PaperRecord(title="Fetched work", year=2026)]

        monkeypatch.setattr(oa, "fetch_new_works", fake_fetch)
        monkeypatch.setenv("OPENALEX_API_KEY", "test-key-123")
        r = api_client.post(
            f"/api/v1/profiles/{SLUG}/rank-works",
            json={"use_openalex": True, "since": "2026-07-01"},
        )
        assert r.status_code == 200
        assert r.json()["works"][0]["title"] == "Fetched work"
        # Seeds came from the profile document; request params were forwarded.
        assert fetch_calls["since"] == "2026-07-01"
        assert "synthetic data science" in fetch_calls["topics"]
        # The server key went into the client, never into the call.
        assert fetch_calls["client"].has_key
        assert "test-key-123" not in repr(fetch_calls)

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
        assert (
            c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"soul": "# soul\n"}).status_code
            == 200
        )
        assert [ctx.kind for ctx in seen] == ["document", "soul"]
        # Both halves in one patch are one write: the hooks run once.
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Biology", "soul": "# again\n"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["updated"] == ["field", "soul"]
        assert [ctx.kind for ctx in seen] == ["document", "soul", "soul"]

    def test_invalidate_after_write_no_longer_calls_any_hook(
        self, make_api_client, fixture_profiles_root
    ):
        """The cache half stayed; the hook half moved off it entirely."""
        from researcher_profiles.api import invalidate_after_write

        seen = []
        root = fixture_profiles_root(SLUG)
        c = make_api_client(root, pre_commit_hooks=[seen.append])
        request = SimpleNamespace(app=c.app)
        store = c.app.state.service.store
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


class TestServedLinksResolve:
    """Every URL that serves ``profile.jsonld`` is a base its links resolve from.

    A consumer given a profile URL appends ``/profile.jsonld`` and resolves each
    relative ``contentUrl`` against where the document came from (spec 3.3, Base
    conformance rule 6). ``/api/v1/profiles/{slug}`` and its ``profile.jsonld``
    answered 200 while every link resolved under them 404ed: the live Prosopia
    profile page showed "Failed to load works ... HTTP 404".
    """

    BASE = "http://testserver/api/v1/profiles/jane-doe"

    @staticmethod
    def _dead(client, url):
        from researcher_profiles.validate import served_dead_links

        return {(d.content_url, d.status) for d in served_dead_links(client.get, url)}

    def test_the_content_base_has_no_dead_link_but_withheld_fulltext(self, api_client):
        dead = self._dead(api_client, f"{self.BASE}/content/")
        assert dead and all(url.startswith("sources/papers/") for url, _ in dead)

    @pytest.mark.parametrize("entry", ["", "/", "/profile.jsonld"])
    def test_the_api_url_reads_like_the_content_base(self, api_client, entry):
        assert self._dead(api_client, self.BASE + entry) == self._dead(
            api_client, f"{self.BASE}/content/"
        )

    def test_unknown_paths_and_writes_are_unchanged(self, api_client):
        assert api_client.get(f"{self.BASE}/no/such/file.md").status_code == 404
        assert api_client.get(f"{self.BASE}/content/no/such/file.md").status_code == 404
        assert api_client.post(f"{self.BASE}/personality/SOUL.md").status_code in (404, 405)
        assert (
            api_client.get("http://testserver/api/v1/nope/personality/SOUL.md").status_code == 404
        )
