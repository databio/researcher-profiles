"""The read projection: tier x viewer kind x artifact role, over HTTP.

The exhaustive policy matrix, asserted against the live HTTP surface: every
profile tier (public/limited/private) x viewer kind (anonymous, stranger,
consumer key at public/limited, operator, owner) x artifact role (including the
hard floors). Two rules run through it:

* 404, never 403: a gate refusal is byte-identical to a nonexistent slug.
* ``?as=`` is a cap: a preview lowers the viewer tier through the same handler.
"""

import io
import tarfile
from contextlib import ExitStack
from dataclasses import replace

import pytest
from fastapi import HTTPException

from researcher_profiles import Forbidden, ResearcherProfile, Unauthenticated
from researcher_profiles.api.caller import Caller
from researcher_profiles.api.deps import ConsumerIdentity, TierFloor, default_caller_resolver
from researcher_profiles.privacy import effective_tiers
from researcher_profiles.schema import ArtifactRef

from .factories import ADA, build_profile_dir, tree_digest

SLUG = "test-researcher"
OPERATOR_TOKEN = "operator-token"

#: Every manifest role, mapped to the artifact that carries it in the fixture.
ROLE_ARTIFACTS: dict[str, str] = {
    "soul": "personality/SOUL.md",
    "expertise": "personality/expertise.md",
    "agent_entry_point": "SKILL.md",
    "works": "sources/papers.jsonld",
    "grants": "sources/grants.jsonld",
    "paper_summary": "sources/summaries/paperA.summary.md",
    "embedding_index": "embeddings/index.json",
    "cv": "sources/cv.md",
    "web": "sources/web/1-lab.md",
    "embedding_index_sqlite": ".cache/embeddings.sqlite",
    "paper_fulltext": "sources/papers/paperA.md",
}

#: Each role's default tier, per ``schema.ROLE_DEFAULT_VISIBILITY``.
ROLE_DEFAULT: dict[str, str] = {
    "soul": "public",
    "expertise": "public",
    "agent_entry_point": "public",
    "works": "public",
    "grants": "public",
    "paper_summary": "public",
    "embedding_index": "public",
    "cv": "private",
    "web": "private",
    "embedding_index_sqlite": "private",
    "paper_fulltext": "private",
}

#: Withheld from every viewer, owner included: ``.cache/`` is a path-prefix floor.
HARD_FLOORS = frozenset({"embedding_index_sqlite"})

VIEWER_TIERS = {
    "anonymous": "public",
    "stranger": "public",
    "consumer_public": "public",
    "consumer_lab": "limited",
    "operator": "private",
    "owner": "private",
}
VIEWER_KINDS = list(VIEWER_TIERS)

#: Tier ordering, lowest first. The one place it is written down.
TIER_ORDER = ("public", "limited", "private")

PROFILE_TIERS = list(TIER_ORDER)


def _profile_visible(viewer: str, profile_tier: str) -> bool:
    """Whether gate one opens: may this viewer see the profile at all."""
    return TIER_ORDER.index(profile_tier) <= TIER_ORDER.index(VIEWER_TIERS[viewer])


#: The 11 of 18 (viewer, profile_tier) pairs where the profile gate is open (TestProfileGate owns the 404s).
VISIBLE_COMBOS = [
    (viewer, tier)
    for viewer in VIEWER_KINDS
    for tier in PROFILE_TIERS
    if _profile_visible(viewer, tier)
]


def _build_full_profile(root, *, visibility: str = "public", overrides: dict | None = None):
    """A profile with an artifact for every role in :data:`ROLE_ARTIFACTS`."""
    pdir = root / SLUG
    build_profile_dir(
        pdir,
        rid=ADA,
        # Papers whose ids match the default summaries, so `summary_available`
        # is a real question rather than always False.
        papers=[
            {"paper_id": "paperA", "title": "A Paper", "year": 2020},
            {"paper_id": "paperB", "title": "B Paper", "year": 2021},
        ],
        cv=True,
        web=True,
        grants=True,
        index="both",
        manifest=True,
    )
    (pdir / "SKILL.md").write_text("# Agent entry point\n", encoding="utf-8")
    (pdir / "sources" / "papers").mkdir(parents=True, exist_ok=True)
    (pdir / "sources" / "papers" / "paperA.md").write_text("full text\n", encoding="utf-8")
    ResearcherProfile.from_files(pdir).build_manifest(write=True)

    prof = ResearcherProfile.from_files(pdir)
    doc = prof.metadata
    doc.visibility = visibility  # type: ignore[assignment]
    for role, tier in (overrides or {}).items():
        for part in list(doc.has_part) + list(doc.subject_of):
            if part.role == role:
                part.visibility = tier
    prof.save_profile(doc)
    return pdir


def _install_viewer_stubs(c):
    """Install the ``X-Test-User`` edit gate, a consumer verifier minting a tier per key, and the caller resolver."""

    def _edit_gate(caller, prof, *, read_ok=False, ref=None):  # noqa: ARG001
        if not caller.scopes:
            raise Unauthenticated("login required")
        if "user:owner" not in caller.scopes:
            raise Forbidden("not the owner")

    def _consumer_verifier(request, scope):
        header = request.headers.get("authorization") or ""
        cred = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""
        if cred == OPERATOR_TOKEN:
            return ConsumerIdentity(
                id="operator", name="operator", scopes=frozenset(SCOPES), is_operator=True
            )
        if cred in CONSUMER_KEYS:
            return ConsumerIdentity(
                id=cred, name=cred, scopes=frozenset(SCOPES), tier=CONSUMER_KEYS[cred]
            )
        raise HTTPException(status_code=401, detail="invalid or unknown API key")

    def _caller_resolver(request):
        # The owner reads their own profile whole; a signed-in person carries
        # ``user:<name>`` for the edit gate; the rest is the bare-SDK caller.
        user = request.headers.get("X-Test-User")
        if user == "owner":
            return Caller(tier="private", scopes=frozenset({"user:owner"}))
        caller = default_caller_resolver(request)
        return replace(caller, scopes=frozenset({f"user:{user}"})) if user else caller

    c.app.state.consumer_verifier = _consumer_verifier
    c.app.state.hooks.caller_resolver = _caller_resolver
    c.app.state.hooks.edit_gate = _edit_gate


@pytest.fixture
def fresh_client(make_api_client, tmp_path):
    """``fresh_client(visibility=..., overrides=...)`` -> its own app and profile, for tests that write."""

    def _make(*, visibility: str = "public", overrides: dict | None = None):
        root = tmp_path / "profiles"
        root.mkdir(parents=True, exist_ok=True)
        _build_full_profile(root, visibility=visibility, overrides=overrides)
        c = make_api_client(root, token=OPERATOR_TOKEN)
        _install_viewer_stubs(c)
        c.profiles_root = root
        return c

    return _make


@pytest.fixture(scope="module")
def shared_client(tmp_path_factory):
    """``shared_client(visibility=..., overrides=...)`` -> a read-only app, built once per distinct key.

    Built lazily inside a test body (so the autouse env isolation is active). At
    module teardown every profile tree must be byte-identical to when it was
    built, so a test that writes through a shared client fails loudly.
    """
    from fastapi.testclient import TestClient

    from researcher_profiles.api.app import create_app
    from researcher_profiles.store import FilesystemProfileStore

    stack = ExitStack()
    clients: dict = {}
    digests: dict = {}

    def _get_client(*, visibility: str = "public", overrides: dict | None = None):
        key = (visibility, tuple(sorted((overrides or {}).items())))
        if key not in clients:
            root = tmp_path_factory.mktemp("shared")
            _build_full_profile(root, visibility=visibility, overrides=overrides)
            c = stack.enter_context(
                TestClient(create_app(FilesystemProfileStore(root), token=OPERATOR_TOKEN))
            )
            _install_viewer_stubs(c)
            c.profiles_root = root
            clients[key] = c
            digests[root] = tree_digest(root)
        return clients[key]

    yield _get_client
    stack.close()
    for root, digest in digests.items():
        assert tree_digest(root) == digest, f"a test wrote through a shared client: {root}"


SCOPES = ("read", "match", "persona", "push")
CONSUMER_KEYS = {"rpk_public": "public", "rpk_lab": "limited"}

#: How each viewer kind authenticates.
VIEWER_HEADERS: dict[str, dict[str, str]] = {
    "anonymous": {},
    "stranger": {"X-Test-User": "someone-else"},
    "consumer_public": {"Authorization": "Bearer rpk_public"},
    "consumer_lab": {"Authorization": "Bearer rpk_lab"},
    "operator": {"Authorization": f"Bearer {OPERATOR_TOKEN}"},
    "owner": {"X-Test-User": "owner"},
}


def _get(client, path, viewer, **kwargs):
    return client.get(path, headers=VIEWER_HEADERS[viewer], **kwargs)


def _detail(client, viewer, **kwargs):
    return _get(client, f"/api/v1/profiles/{SLUG}", viewer, **kwargs)


def _files(client, viewer, **kwargs):
    return _get(client, f"/api/v1/profiles/{SLUG}/files", viewer, **kwargs)


def _summary_served(client, viewer, paper_id="paperA", **kwargs) -> bool:
    """Whether ``/summaries`` hands this viewer the summary's text."""
    r = _get(
        client,
        f"/api/v1/profiles/{SLUG}/summaries",
        viewer,
        params={"ids": paper_id, **(kwargs.get("params") or {})},
    )
    assert r.status_code == 200, r.text
    return paper_id in r.json()["summaries"]


def _expected_effective(role: str, profile_tier: str, declared: str | None = None) -> str:
    """The tier the matrix says an artifact resolves to. The rule, restated."""
    tiers = [profile_tier, declared or ROLE_DEFAULT[role]]
    return TIER_ORDER[max(TIER_ORDER.index(t) for t in tiers)]


def _sees(role: str, viewer: str, profile_tier: str, declared: str | None = None) -> bool:
    """Whether ``viewer`` receives this artifact's body."""
    if role in HARD_FLOORS:
        return False
    effective = _expected_effective(role, profile_tier, declared)
    return TIER_ORDER.index(effective) <= TIER_ORDER.index(VIEWER_TIERS[viewer])


@pytest.mark.parametrize("profile_tier", PROFILE_TIERS)
@pytest.mark.parametrize("viewer", VIEWER_KINDS)
class TestProfileGate:
    """Gate one: may this viewer see the profile at all?"""

    def test_detail_listing_and_collection_agree(self, shared_client, viewer, profile_tier):
        c = shared_client(visibility=profile_tier)
        visible = _profile_visible(viewer, profile_tier)

        detail = _detail(c, viewer)
        listing = _get(c, "/api/v1/profiles", viewer).json()["profiles"]
        jsonld = _get(c, f"/api/v1/profiles/{SLUG}/profile.jsonld", viewer)
        collection = _get(
            c, "/api/v1/collection.json", viewer
        )  # nothing visible is an empty bundle
        bundle = collection.json()

        assert detail.status_code == (200 if visible else 404), detail.text
        assert [p["slug"] for p in listing] == ([SLUG] if visible else [])
        assert jsonld.status_code == (200 if visible else 404)
        assert [card["slug"] for card in bundle["cards"]] == [p["slug"] for p in listing]
        assert bundle["count"] == len(listing)
        # The viewer tier is stamped on every response, refusals included.
        assert detail.headers["X-RP-Viewer-Tier"] == VIEWER_TIERS[viewer]
        assert collection.headers["X-RP-Viewer-Tier"] == VIEWER_TIERS[viewer]


@pytest.mark.parametrize("viewer,profile_tier", VISIBLE_COMBOS)
class TestArtifactGate:
    """Gate two: which pieces does this viewer receive? The role is named in each assertion."""

    def test_manifest_and_bodies_follow_the_tier(self, shared_client, viewer, profile_tier):
        c = shared_client(visibility=profile_tier)
        r = _files(c, viewer)
        assert r.status_code == 200, r.text
        files = {m["contentUrl"]: m for m in r.json()["files"]}
        withheld = set(r.json()["withheld"])
        for role, url in ROLE_ARTIFACTS.items():
            expected = _expected_effective(role, profile_tier)
            assert files[url]["effective_visibility"] == expected, role
            # `withheld` names exactly what was not served.
            assert (url in withheld) is not _sees(role, viewer, profile_tier), role

        # The two bodies the full view carries, and their sizes in the record.
        full = _detail(c, viewer, params={"view": "full"})
        assert full.status_code == 200, full.text
        for role in ("soul", "expertise"):
            body = full.json().get(role)
            part = full.json()["parts"][role]
            if _sees(role, viewer, profile_tier):
                assert body, role
                assert part["available"] is True and part["bytes"], role
            else:
                # Withheld is absent/None, never "": a client has to tell them apart.
                assert body is None, role
                assert part == {"available": False, "reason": "not_permitted"}, role


@pytest.mark.parametrize("declared", PROFILE_TIERS)
@pytest.mark.parametrize("profile_tier", PROFILE_TIERS)
class TestMostRestrictiveComposition:
    """A declared tier composes with the profile default; neither one wins alone."""

    def test_summary_tier_is_the_more_restrictive_of_the_two(
        self, shared_client, declared, profile_tier
    ):
        c = shared_client(visibility=profile_tier, overrides={"paper_summary": declared})
        r = _files(c, "owner")
        entry = next(
            m for m in r.json()["files"] if m["contentUrl"] == ROLE_ARTIFACTS["paper_summary"]
        )
        assert entry["effective_visibility"] == _expected_effective(
            "paper_summary", profile_tier, declared
        )

        # An anonymous viewer gets it only when both are public.
        both_public = declared == "public" and profile_tier == "public"
        if profile_tier != "public":
            # The profile itself is hidden from an anonymous caller.
            r = _get(c, f"/api/v1/profiles/{SLUG}/summaries", "anonymous", params={"ids": "paperA"})
            assert r.status_code == 404
            return
        assert _summary_served(c, "anonymous") is both_public


class TestPapersAndSummaries:
    def test_works_withheld_404s_the_papers_list(self, shared_client):
        c = shared_client(overrides={"works": "private"})
        assert _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous").status_code == 404
        assert _get(c, f"/api/v1/profiles/{SLUG}/papers", "owner").status_code == 200

    def test_summary_available_reflects_this_viewer(self, shared_client):
        """It must not advertise a summary the caller would then 404 on."""
        c = shared_client(overrides={"paper_summary": "limited"})
        anon = _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous").json()["items"]
        lab = _get(c, f"/api/v1/profiles/{SLUG}/papers", "consumer_lab").json()["items"]
        assert all(p["summary"] == {"available": False, "reason": "not_permitted"} for p in anon)
        assert any(p["summary"]["available"] for p in lab)
        assert not _summary_served(c, "anonymous")
        assert _summary_served(c, "consumer_lab")


class TestRefusalIsIndistinguishableFromAbsence:
    """A held-back profile answers exactly as a nonexistent one does."""

    @pytest.mark.parametrize(
        "path",
        ["/api/v1/profiles/{slug}", "/api/v1/profiles/{slug}/profile.jsonld"],
    )
    def test_bodies_are_byte_identical(self, shared_client, path):
        c = shared_client(visibility="private")
        held = _get(c, path.format(slug=SLUG), "anonymous")
        missing = _get(c, path.format(slug="nobody-at-all"), "anonymous")
        assert held.status_code == missing.status_code == 404
        # Same status AND same body: a distinguishable message is an oracle.
        assert held.json()["detail"].replace(SLUG, "nobody-at-all") == missing.json()["detail"]

    def test_papers_refusal_is_404(self, shared_client):
        # The /summaries refusal on a hidden profile is asserted in
        # TestMostRestrictiveComposition.
        c = shared_client(visibility="limited")
        assert _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous").status_code == 404


class TestPreviewCap:
    @pytest.mark.parametrize("viewer, as_", [("owner", "anonymous"), ("anonymous", "owner")])
    def test_preview_equals_the_anonymous_view(self, shared_client, viewer, as_):
        """``?as=owner`` from an anonymous caller never widens."""
        c = shared_client()
        preview = _detail(c, viewer, params={"as": as_})
        assert preview.status_code == 200
        assert preview.json() == _detail(c, "anonymous").json()
        assert preview.headers["X-RP-Viewer-Tier"] == "public"

    def test_preview_matches_on_papers_and_summaries_too(self, shared_client):
        c = shared_client(overrides={"paper_summary": "limited"})
        owner_preview = _get(
            c, f"/api/v1/profiles/{SLUG}/papers", "owner", params={"as": "anonymous"}
        )
        anon = _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous")
        assert owner_preview.json() == anon.json()
        assert not _summary_served(c, "owner", params={"as": "anonymous"})

    def test_lab_preview_sits_between(self, shared_client):
        c = shared_client(overrides={"paper_summary": "limited"})
        r = _get(
            c, f"/api/v1/profiles/{SLUG}/summaries", "owner", params={"ids": "paperA", "as": "lab"}
        )
        assert "paperA" in r.json()["summaries"]
        assert r.headers["X-RP-Viewer-Tier"] == "limited"

    def test_unknown_viewer_is_a_loud_400(self, shared_client):
        """Ignoring it would show an owner their own view believing otherwise."""
        c = shared_client()
        r = _detail(c, "owner", params={"as": "nonsense"})
        assert r.status_code == 400
        assert "nonsense" in r.json()["detail"]


class TestArchiveProjection:
    def _members(self, resp) -> set[str]:
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:*") as tf:
            return {m.name for m in tf.getmembers() if m.isfile()}

    def test_public_caller_gets_nothing_above_public(self, shared_client):
        c = shared_client()
        prof = ResearcherProfile.from_files(c.profiles_root / SLUG)
        excluded = {u for u, t in effective_tiers(prof.metadata).items() if t != "public"}

        r = _get(c, f"/api/v1/profiles/{SLUG}/archive", "consumer_public")
        assert r.status_code == 200
        assert r.headers["X-RP-Archive-Tier"] == "public"
        members = self._members(r)
        assert members
        for name in members:
            assert name not in excluded, f"{name} is above public but shipped"
            assert not name.startswith(".cache/")

    def test_private_defaults_ship_only_to_a_caller_entitled_to_them(self, shared_client):
        # Paper full text is an ordinary private-default artifact, like the CV.
        c = shared_client()
        public = self._members(_get(c, f"/api/v1/profiles/{SLUG}/archive", "consumer_public"))
        operator = self._members(_get(c, f"/api/v1/profiles/{SLUG}/archive", "operator"))
        assert "sources/cv.md" not in public
        assert "sources/cv.md" in operator
        assert not any(m.startswith("sources/papers/") for m in public)
        assert any(m.startswith("sources/papers/") for m in operator)


class TestSearchProjection:
    """The served sqlite index holds cv/web chunks; the API must not quote them."""

    @pytest.fixture
    def searchable(self, shared_client, monkeypatch):
        c = shared_client()

        class _Hit:
            def __init__(self, source_type):
                self.text = f"verbatim {source_type} text"
                self.source_type = source_type
                self.source_id = "s1"
                self.chunk_index = 0
                self.section = None
                self.cosine = 0.8
                self.score = 0.9
                self.meta = {}

        captured: dict = {}

        def _search(store, prof, viewer, query, *, source_types, k):
            captured["source_types"] = list(source_types)
            # Ignores the restriction on purpose: the route's own drop is the guarantee.
            return [_Hit(t) for t in ("paper_summary", "cv", "web", "grant")], None

        import researcher_profiles.api._semantic as semantic

        monkeypatch.setattr(semantic, "search_chunks", _search)
        return c, captured

    @pytest.mark.parametrize(
        "viewer, leaks_allowed",
        [("consumer_public", False), ("consumer_lab", False), ("operator", True)],
    )
    def test_private_chunks_are_dropped(self, searchable, viewer, leaks_allowed):
        c, captured = searchable
        r = c.post(
            f"/api/v1/profiles/{SLUG}/search",
            json={"query": "anything"},
            headers=VIEWER_HEADERS[viewer],
        )
        assert r.status_code == 200, r.text
        types = {h["source_type"] for h in r.json()["hits"]}
        assert "paper_summary" in types
        if leaks_allowed:
            assert {"cv", "web", "grant"} <= types
        else:
            assert not (types & {"cv", "web", "grant"}), f"{viewer} was shown private chunks"
            # The restriction is also pushed into the query itself.
            assert "cv" not in captured["source_types"]


class TestVisibilityWritesAreHonest:
    @pytest.mark.parametrize(
        "role, tier, n_rows", [("paper_fulltext", "public", 1), ("paper_summary", "limited", 2)]
    )
    def test_a_role_selector_retiers_every_matching_artifact(
        self, fresh_client, role, tier, n_rows
    ):
        # paper_fulltext defaults to private but the owner may raise it: no floor, no `locked`.
        c = fresh_client()
        url = f"/api/v1/profiles/{SLUG}/visibility"
        before = [a for a in _get(c, url, "owner").json()["artifacts"] if a["role"] == role]
        assert len(before) == n_rows
        for a in before:
            assert a["effective"] == ROLE_DEFAULT[role]
            assert a.get("locked", False) is False
            assert a.get("lock_reason") is None

        r = c.patch(
            url,
            json={"artifacts": [{"role": role, "visibility": tier}]},
            headers=VIEWER_HEADERS["owner"],
        )
        assert r.status_code == 200, r.text
        assert r.json()["artifacts_changed"] == n_rows

        after = [a for a in _get(c, url, "owner").json()["artifacts"] if a["role"] == role]
        for a in after:
            assert a["declared"] == a["effective"] == tier
            assert ("anonymous" in a["visible_to"]) is (tier == "public")

    def test_a_selector_matching_nothing_is_a_400(self, fresh_client):
        c = fresh_client()
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/visibility",
            json={"artifacts": [{"role": "no-such-role", "visibility": "limited"}]},
            headers=VIEWER_HEADERS["owner"],
        )
        assert r.status_code == 400


def _add_derived_note(client) -> None:
    """Declare a public ``derived-note.md`` drawn from the CV, so it is raised to private."""
    pdir = client.profiles_root / SLUG
    prof = ResearcherProfile.from_files(pdir)
    (pdir / "derived-note.md").write_text("drawn from the CV\n", encoding="utf-8")
    prof.metadata.has_part.append(
        ArtifactRef(
            name="Derived note",
            encodingFormat="text/markdown",
            contentUrl="derived-note.md",
            role="custom",
            derivedFrom=["cv"],
            visibility="public",
        )
    )
    prof.save_profile()
    client.app.state.store.evict(SLUG)


class TestVisibilityReport:
    def test_counts_are_the_consequence_a_person_reads(self, shared_client):
        c = shared_client()
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        assert report["counts"]["anonymous"] < report["counts"]["you"]
        # The hard floors are in nobody's count, the owner's included.
        floors = {ROLE_ARTIFACTS[r] for r in HARD_FLOORS}
        for a in report["artifacts"]:
            if a["content_url"] in floors:
                assert a["visible_to"] == []

    def test_why_names_the_specific_cause_on_the_row(self, shared_client):
        c = shared_client(visibility="limited")
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        soul = next(a for a in report["artifacts"] if a["role"] == "soul")
        assert soul["effective"] == "limited"
        assert any("profile default" in phrase for phrase in soul["raised_by"])

    def test_a_derived_artifact_names_its_source(self, fresh_client):
        c = fresh_client()
        _add_derived_note(c)

        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        note = next(a for a in report["artifacts"] if a["content_url"] == "derived-note.md")
        assert note["declared"] == "public"
        assert note["effective"] == "private"
        assert any("sources/cv.md" in phrase for phrase in note["raised_by"])
        # And an anonymous reader never receives it.
        assert "derived-note.md" in set(_files(c, "anonymous").json()["withheld"])
        # The content route gates it at its derived tier too, not its declared one.
        owner = _content(c, "owner", "derived-note.md")
        assert owner.status_code == 200, owner.text
        assert owner.headers["X-RP-Effective-Tier"] == "private"
        assert _content(c, "anonymous", "derived-note.md").status_code == 404
        assert _content(c, "stranger", "derived-note.md").status_code == 404


def _content(client, viewer, artifact, **kwargs):
    return _get(client, f"/api/v1/profiles/{SLUG}/content/{artifact}", viewer, **kwargs)


@pytest.mark.parametrize("profile_tier", ["public", "limited", "private"])
@pytest.mark.parametrize("viewer", VIEWER_KINDS)
class TestContentRouteMatrix:
    """``GET /profiles/{slug}/content/{artifact}``: the tier gate, not an owner gate, decides, so a hosted registry can serve every ``contentUrl``."""

    def test_body_is_served_only_when_the_tier_allows(self, shared_client, viewer, profile_tier):
        c = shared_client(visibility=profile_tier)
        profile_ok = _profile_visible(viewer, profile_tier)
        for role, url in ROLE_ARTIFACTS.items():
            r = _content(c, viewer, url)
            if role in HARD_FLOORS:
                # 403 whether or not the caller may see the profile at all,
                # except that the profile gate runs first, so a caller who
                # cannot see the profile gets the profile's 404 and learns nothing.
                assert r.status_code == (403 if profile_ok else 404), (role, r.text)
            elif profile_ok and _sees(role, viewer, profile_tier):
                assert r.status_code == 200, (role, r.text)
                expected = _expected_effective(role, profile_tier)
                assert r.headers["X-RP-Effective-Tier"] == expected, role
            else:
                assert r.status_code == 404, (role, r.text)

        # ``/content/`` is a base URL: ``profile.jsonld`` resolves out of it.
        # This is the single thing that makes one explorer build work against a
        # hosted registry and a rendered static site alike. It points at a base
        # and follows relative ``contentUrl``s from the document it finds there.
        via_content = _content(c, viewer, "profile.jsonld")
        direct = _get(c, f"/api/v1/profiles/{SLUG}/profile.jsonld", viewer)
        assert via_content.status_code == direct.status_code == (200 if profile_ok else 404)
        assert via_content.content == direct.content
        if profile_ok:
            assert via_content.headers["Cache-Control"] == direct.headers["Cache-Control"]
            assert via_content.headers["content-type"].startswith("application/ld+json")


class TestContentRouteEffectiveTier:
    """The content route gates on the effective tier, not the role default."""

    def test_a_retiered_artifact_is_served_at_its_declared_tier(self, shared_client):
        c = shared_client(overrides={"works": "limited"})
        owner = _content(c, "owner", ROLE_ARTIFACTS["works"])
        assert owner.status_code == 200, owner.text
        assert owner.headers["X-RP-Effective-Tier"] == "limited"
        lab = _content(c, "consumer_lab", ROLE_ARTIFACTS["works"])
        assert lab.status_code == 200, lab.text
        assert lab.headers["X-RP-Effective-Tier"] == "limited"
        assert _content(c, "anonymous", ROLE_ARTIFACTS["works"]).status_code == 404


class TestContentRouteRefusalsAreOpaque:
    def test_withheld_and_unknown_are_byte_identical(self, shared_client):
        c = shared_client()
        withheld = _content(c, "anonymous", "sources/cv.md")
        unknown = _content(c, "anonymous", "sources/not-a-file.md")
        assert withheld.status_code == unknown.status_code == 404
        assert withheld.json()["detail"].replace("sources/cv.md", "X") == unknown.json()[
            "detail"
        ].replace("sources/not-a-file.md", "X")

    def test_a_hidden_profile_hides_its_artifacts_the_same_way(self, shared_client):
        c = shared_client(visibility="limited")
        r = _content(c, "anonymous", "personality/SOUL.md")
        missing = _get(c, "/api/v1/profiles/no-such-slug/content/personality/SOUL.md", "anonymous")
        assert r.status_code == missing.status_code == 404

    def test_traversal_is_a_404(self, shared_client):
        c = shared_client()
        assert _content(c, "operator", "../other/profile.jsonld").status_code == 404


class TestProfileCollection:
    """``GET /collection.json``: the listing as a document the SPA home view fetches."""

    def _bundle(self, client, viewer):
        r = _get(client, "/api/v1/collection.json", viewer)
        assert r.status_code == 200, r.text
        return r.json()

    def test_a_card_resolves_the_manifest_and_carries_the_stats(self, shared_client):
        c = shared_client()
        card = self._bundle(c, "anonymous")["cards"][0]
        # ABSOLUTE, not root-relative: a client parses this with a bare URL
        # constructor, which has no document to resolve a relative path against.
        assert card["base"] == f"http://testserver/api/v1/profiles/{SLUG}/content/"
        doc = c.get(card["base"] + "profile.jsonld")
        assert doc.status_code == 200
        assert doc.json()["name"]
        summary = _get(c, "/api/v1/profiles", "anonymous").json()["profiles"][0]
        for field in ("name", "level", "affiliation", "field", "paper_count", "summary_count"):
            assert card[field] == summary[field], field

    def test_the_base_honours_the_proxy_the_client_actually_reached(self, shared_client):
        """Behind TLS termination, ``base_url`` is ``http://`` and a browser on
        an ``https://`` page refuses to load it."""
        c = shared_client()
        r = c.get(
            "/api/v1/collection.json",
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "profiles.example.org"},
        )
        card = r.json()["cards"][0]
        assert card["base"] == f"https://profiles.example.org/api/v1/profiles/{SLUG}/content/"


class TestHostFloorHook:
    """``profile_tier_floor`` returns a ``TierFloor`` (tier and reason in one value, so the reason cannot describe a rule that did not run)."""

    def test_a_floor_narrows_without_blacking_out_and_reports_its_reason(self, fresh_client):
        c = fresh_client()
        assert _detail(c, "anonymous").status_code == 200

        # The reason varies with the profile: one static string could not do
        # this, which is why it is gone.
        c.app.state.hooks.profile_tier_floor = lambda caller, prof, slug: TierFloor(
            "limited", f"{slug} is held back"
        )
        # A public profile is hidden from an anonymous caller...
        assert _detail(c, "anonymous").status_code == 404
        assert _get(c, "/api/v1/profiles", "anonymous").json()["profiles"] == []
        # ...but the floor is a narrowing, not a blackout.
        assert _detail(c, "consumer_lab").status_code == 200
        assert _detail(c, "owner").status_code == 200
        # The owner's report carries the reason the hook returned.
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        assert report["profile_floor"] == "limited"
        assert report["profile_floor_reason"] == f"{SLUG} is held back"

    @pytest.mark.parametrize("hook_set", [True, False], ids=["empty-floor", "unset-hook"])
    def test_no_floor_reports_no_reason(self, fresh_client, hook_set):
        c = fresh_client()
        if hook_set:
            c.app.state.hooks.profile_tier_floor = lambda caller, prof, slug: TierFloor()
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        assert report["profile_floor"] is None
        assert report["profile_floor_reason"] is None


class TestCredentialSensitiveCaching:
    """Every tier-projected body varies on credentials; only the anonymous ``profile.jsonld`` is shareable (its TTL bounds how long an unpublished profile lingers in a cache)."""

    @pytest.mark.parametrize(
        "path, viewer, cache_control",
        [
            ("/api/v1/profiles", "anonymous", None),
            # Collection membership depends on who asked, so one caller's copy must never reach the next.
            ("/api/v1/collection.json", "anonymous", "private, no-store"),
            (f"/api/v1/profiles/{SLUG}", "anonymous", "private, no-store"),
            # The one shareable body: the anonymous document, for a minute.
            (f"/api/v1/profiles/{SLUG}/profile.jsonld", "anonymous", "public, max-age=60"),
            (f"/api/v1/profiles/{SLUG}/profile.jsonld", "owner", "private, no-store"),
            (
                f"/api/v1/profiles/{SLUG}/content/personality/SOUL.md",
                "anonymous",
                "private, no-store",
            ),
        ],
        ids=["profiles", "collection", "detail", "anonymous-document", "owner-document", "content"],
    )
    def test_caching_headers(self, shared_client, path, viewer, cache_control):
        r = _get(shared_client(), path, viewer)
        assert r.status_code == 200, r.text
        assert r.headers["Vary"] == "Authorization, Cookie"
        if cache_control:
            assert r.headers["Cache-Control"] == cache_control

    def test_the_document_carries_a_strong_etag_that_revalidates(self, shared_client):
        c = shared_client()
        path = f"/api/v1/profiles/{SLUG}/profile.jsonld"
        etag = _get(c, path, "anonymous").headers["ETag"]
        assert etag.startswith('"') and etag.endswith('"')
        assert _get(c, path, "anonymous").headers["ETag"] == etag
        # A matching ETag revalidates to an empty 304.
        r = c.get(path, headers={**VIEWER_HEADERS["anonymous"], "If-None-Match": etag})
        assert r.status_code == 304
        assert r.content == b""
