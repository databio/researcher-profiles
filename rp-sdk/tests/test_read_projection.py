"""The read projection: tier x viewer kind x artifact role, over HTTP.

Without ``privacy.effective_tiers`` on every read route, a profile whose
document says ``visibility: limited`` would be served byte-identically to one
that says ``public``, and the only thing standing between a hosted registry and
the open web would be a single site-wide switch. This file is the enforcement
half: every combination of

* a **profile-level tier** (``public`` / ``limited`` / ``private``),
* a **viewer kind** (anonymous, signed-in stranger, consumer key at ``public``
  and at ``limited``, operator, owner),
* an **artifact role** (the full manifest role set, including the hard floors),

is asserted against the live HTTP surface. A tier system without this matrix is
not enforced; it is merely written down.

Two rules run through everything here:

* 404, never 403. A profile gate refusal is byte-identical to a genuinely
  nonexistent slug. A distinguishable error is an existence oracle.
* ``?as=`` is a cap. Previewing as a stranger goes through the same handler
  and the same projection with the viewer tier lowered, so the preview cannot
  drift from reality. It is reality.
"""

import io
import tarfile
from dataclasses import replace

import pytest
from fastapi import HTTPException

from researcher_profiles import Forbidden, ResearcherProfile, Unauthenticated
from researcher_profiles.api.caller import Caller
from researcher_profiles.api.deps import ConsumerIdentity, TierFloor, default_caller_resolver
from researcher_profiles.privacy import effective_tiers
from researcher_profiles.schema import ArtifactRef

from .factories import ADA, build_profile_dir

SLUG = "test-researcher"
OPERATOR_TOKEN = "operator-token"

#: Every role the manifest can carry, mapped to the artifact that carries it in
#: the fixture below, and to the tier that role defaults to.
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

#: The role's default tier, per ``schema.ROLE_DEFAULT_VISIBILITY``.
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

#: Withheld from EVERY viewer, owner included: build-local derived state under
#: ``.cache/`` (a path-prefix floor, not a role floor). These are the ``.`` rows
#: that stay ``.`` all the way across. ``paper_fulltext`` is NOT here: it is an
#: ordinary private-default role the owner may re-tier freely.
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


#: The 11 of 18 (viewer, profile_tier) pairs where the profile gate is OPEN.
#: The other 7 are TestProfileGate's subject: it asserts the 404 for every one
#: of the 18. Generating them below produced 168 skips that could never assert
#: anything, so they are not generated.
VISIBLE_COMBOS = [
    (viewer, tier)
    for viewer in VIEWER_KINDS
    for tier in PROFILE_TIERS
    if _profile_visible(viewer, tier)
]


# ---------------------------------------------------------------------------
# Fixture: one profile carrying every role
# ---------------------------------------------------------------------------


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


@pytest.fixture
def matrix_client(make_api_client, tmp_path):
    """Factory: ``matrix_client(visibility=..., overrides=...)`` -> TestClient.

    Installs the ``X-Test-User`` edit-gate stub (the same shape ``test_edit.py`` uses)
    and a consumer verifier that mints a tier per key, so all six viewer kinds
    are reachable from one app.
    """

    def _make(*, visibility: str = "public", overrides: dict | None = None):
        root = tmp_path / f"profiles-{visibility}-{len(overrides or {})}"
        root.mkdir(parents=True, exist_ok=True)
        _build_full_profile(root, visibility=visibility, overrides=overrides)
        c = make_api_client(root, token=OPERATOR_TOKEN)

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
                    id=cred,
                    name=cred,
                    scopes=frozenset(SCOPES),
                    tier=CONSUMER_KEYS[cred],
                )
            raise HTTPException(status_code=401, detail="invalid or unknown API key")

        def _caller_resolver(request):
            # The owner reads their own profile whole; everyone else is the
            # bare-SDK caller (a consumer identity, the operator, anonymous).
            # A signed-in person carries ``user:<name>`` for the edit gate.
            user = request.headers.get("X-Test-User")
            if user == "owner":
                return Caller(tier="private", scopes=frozenset({"user:owner"}))
            caller = default_caller_resolver(request)
            return replace(caller, scopes=frozenset({f"user:{user}"})) if user else caller

        c.app.state.consumer_verifier = _consumer_verifier
        c.app.state.hooks.caller_resolver = _caller_resolver
        c.app.state.hooks.edit_gate = _edit_gate
        return c

    return _make


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


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_tier", ["public", "limited", "private"])
@pytest.mark.parametrize("viewer", VIEWER_KINDS)
class TestProfileGate:
    """Gate one: may this viewer see the profile at all?"""

    def test_detail_and_listing_agree(self, matrix_client, viewer, profile_tier):
        c = matrix_client(visibility=profile_tier)
        visible = _profile_visible(viewer, profile_tier)

        detail = _detail(c, viewer)
        listing = _get(c, "/api/v1/profiles", viewer).json()["profiles"]
        jsonld = _get(c, f"/api/v1/profiles/{SLUG}/profile.jsonld", viewer)

        assert detail.status_code == (200 if visible else 404), detail.text
        assert [p["slug"] for p in listing] == ([SLUG] if visible else [])
        assert jsonld.status_code == (200 if visible else 404)
        # The viewer tier is stamped on every response, refusals included.
        assert detail.headers["X-RP-Viewer-Tier"] == VIEWER_TIERS[viewer]


@pytest.mark.parametrize("viewer,profile_tier", VISIBLE_COMBOS)
class TestArtifactGate:
    """Gate two: which of its pieces does this viewer receive?

    Each test walks every role against one built profile; the role is named in
    the assertion message, so a failure still says which artifact leaked.
    """

    def test_manifest_reports_effective_tier_and_withholds_the_rest(
        self, matrix_client, viewer, profile_tier
    ):
        c = matrix_client(visibility=profile_tier)
        r = _files(c, viewer)
        assert r.status_code == 200, r.text
        files = {m["contentUrl"]: m for m in r.json()["files"]}
        withheld = set(r.json()["withheld"])
        for role, url in ROLE_ARTIFACTS.items():
            expected = _expected_effective(role, profile_tier)
            assert files[url]["effective_visibility"] == expected, role
            # `withheld` names exactly what was not served.
            assert (url in withheld) is not _sees(role, viewer, profile_tier), role

    def test_detail_bodies_are_served_only_when_the_tier_allows(
        self, matrix_client, viewer, profile_tier
    ):
        """The two bodies the full view carries, and their sizes in the record."""
        c = matrix_client(visibility=profile_tier)
        r = _detail(c, viewer, params={"view": "full"})
        assert r.status_code == 200, r.text
        for role in ("soul", "expertise"):
            body = r.json().get(role)
            part = r.json()["parts"][role]
            if _sees(role, viewer, profile_tier):
                assert body, role
                assert part["available"] is True and part["bytes"], role
            else:
                # Withheld is absent/None, never "": a client has to tell them apart.
                assert body is None, role
                assert part == {"available": False, "reason": "not_permitted"}, role


@pytest.mark.parametrize("declared", ["public", "limited", "private"])
@pytest.mark.parametrize("profile_tier", ["public", "limited", "private"])
class TestMostRestrictiveComposition:
    """A declared tier composes with the profile default; neither one wins alone."""

    def test_summary_tier_is_the_more_restrictive_of_the_two(
        self, matrix_client, declared, profile_tier
    ):
        c = matrix_client(visibility=profile_tier, overrides={"paper_summary": declared})
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
    def test_works_withheld_404s_the_papers_list(self, matrix_client):
        c = matrix_client(overrides={"works": "private"})
        assert _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous").status_code == 404
        assert _get(c, f"/api/v1/profiles/{SLUG}/papers", "owner").status_code == 200

    def test_summary_available_reflects_this_viewer(self, matrix_client):
        """It must not advertise a summary the caller would then 404 on."""
        c = matrix_client(overrides={"paper_summary": "limited"})
        anon = _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous").json()["items"]
        lab = _get(c, f"/api/v1/profiles/{SLUG}/papers", "consumer_lab").json()["items"]
        assert all(p["summary"] == {"available": False, "reason": "not_permitted"} for p in anon)
        assert any(p["summary"]["available"] for p in lab)
        assert not _summary_served(c, "anonymous")
        assert _summary_served(c, "consumer_lab")


# ---------------------------------------------------------------------------
# 404, never 403
# ---------------------------------------------------------------------------


class TestRefusalIsIndistinguishableFromAbsence:
    """A held-back profile answers exactly as a nonexistent one does."""

    @pytest.mark.parametrize(
        "path",
        ["/api/v1/profiles/{slug}", "/api/v1/profiles/{slug}/profile.jsonld"],
    )
    def test_bodies_are_byte_identical(self, matrix_client, path):
        c = matrix_client(visibility="private")
        held = _get(c, path.format(slug=SLUG), "anonymous")
        missing = _get(c, path.format(slug="nobody-at-all"), "anonymous")
        assert held.status_code == missing.status_code == 404
        # Same status AND same body: a distinguishable message is an oracle.
        assert held.json()["detail"].replace(SLUG, "nobody-at-all") == missing.json()["detail"]

    def test_papers_refusal_is_404(self, matrix_client):
        # The /summaries refusal on a hidden profile is asserted in
        # TestMostRestrictiveComposition.
        c = matrix_client(visibility="limited")
        assert _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous").status_code == 404


# ---------------------------------------------------------------------------
# ?as=: the preview cap
# ---------------------------------------------------------------------------


class TestPreviewCap:
    def test_owner_as_anonymous_matches_what_anonymous_gets(self, matrix_client):
        c = matrix_client()
        preview = _detail(c, "owner", params={"as": "anonymous"})
        anon = _detail(c, "anonymous")
        assert preview.status_code == 200
        assert preview.json() == anon.json()
        assert preview.headers["X-RP-Viewer-Tier"] == "public"

    def test_preview_matches_on_papers_and_summaries_too(self, matrix_client):
        c = matrix_client(overrides={"paper_summary": "limited"})
        owner_preview = _get(
            c, f"/api/v1/profiles/{SLUG}/papers", "owner", params={"as": "anonymous"}
        )
        anon = _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous")
        assert owner_preview.json() == anon.json()
        assert not _summary_served(c, "owner", params={"as": "anonymous"})

    def test_the_cap_never_widens(self, matrix_client):
        """``?as=owner`` as an anonymous caller is still the anonymous view."""
        c = matrix_client()
        widened = _detail(c, "anonymous", params={"as": "owner"})
        plain = _detail(c, "anonymous")
        assert widened.json() == plain.json()
        assert widened.headers["X-RP-Viewer-Tier"] == "public"

    def test_lab_preview_sits_between(self, matrix_client):
        c = matrix_client(overrides={"paper_summary": "limited"})
        r = _get(
            c, f"/api/v1/profiles/{SLUG}/summaries", "owner", params={"ids": "paperA", "as": "lab"}
        )
        assert "paperA" in r.json()["summaries"]
        assert r.headers["X-RP-Viewer-Tier"] == "limited"

    def test_unknown_viewer_is_a_loud_400(self, matrix_client):
        """Ignoring it would show an owner their own view believing otherwise."""
        c = matrix_client()
        r = _detail(c, "owner", params={"as": "nonsense"})
        assert r.status_code == 400
        assert "nonsense" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Leaks: the embedding index and the archive
# ---------------------------------------------------------------------------


class TestArchiveProjection:
    def _members(self, resp) -> set[str]:
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:*") as tf:
            return {m.name for m in tf.getmembers() if m.isfile()}

    def test_public_caller_gets_nothing_above_public(self, matrix_client, tmp_path):
        c = matrix_client()
        prof = ResearcherProfile.from_files(tmp_path / "profiles-public-0" / SLUG)
        excluded = {u for u, t in effective_tiers(prof.metadata).items() if t != "public"}

        r = _get(c, f"/api/v1/profiles/{SLUG}/archive", "consumer_public")
        assert r.status_code == 200
        assert r.headers["X-RP-Archive-Tier"] == "public"
        members = self._members(r)
        assert members
        for name in members:
            assert name not in excluded, f"{name} is above public but shipped"
            assert not name.startswith(".cache/")

    def test_private_defaults_ship_only_to_a_caller_entitled_to_them(self, matrix_client):
        # Paper full text is an ordinary private-default artifact: withheld
        # from a public caller, shipped to a private (operator/owner) one,
        # exactly like the CV. No role floor holds it back from everyone.
        c = matrix_client()
        public = self._members(_get(c, f"/api/v1/profiles/{SLUG}/archive", "consumer_public"))
        operator = self._members(_get(c, f"/api/v1/profiles/{SLUG}/archive", "operator"))
        assert "sources/cv.md" not in public
        assert "sources/cv.md" in operator
        assert not any(m.startswith("sources/papers/") for m in public)
        assert any(m.startswith("sources/papers/") for m in operator)


class TestSearchProjection:
    """The served sqlite index holds cv/web chunks; the API must not quote them."""

    @pytest.fixture
    def searchable(self, matrix_client, monkeypatch):
        c = matrix_client()

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


# ---------------------------------------------------------------------------
# Honest writes
# ---------------------------------------------------------------------------


class TestVisibilityWritesAreHonest:
    def test_fulltext_tier_is_choosable_and_defaults_to_private(self, matrix_client):
        # paper_fulltext defaults to private (nothing silently becomes
        # public), but the owner may raise it: a PATCH to public succeeds and
        # the report reflects the new effective tier. No floor, no `locked`.
        c = matrix_client()
        url = ROLE_ARTIFACTS["paper_fulltext"]

        before = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        ft = next(a for a in before["artifacts"] if a["content_url"] == url)
        assert ft["effective"] == "private"
        assert ft.get("locked", False) is False
        assert ft.get("lock_reason") is None

        r = c.patch(
            f"/api/v1/profiles/{SLUG}/visibility",
            json={"artifacts": [{"role": "paper_fulltext", "visibility": "public"}]},
            headers=VIEWER_HEADERS["owner"],
        )
        assert r.status_code == 200, r.text
        assert r.json()["artifacts_changed"] >= 1

        after = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        ft = next(a for a in after["artifacts"] if a["content_url"] == url)
        assert ft["declared"] == "public"
        assert ft["effective"] == "public"
        assert "anonymous" in ft["visible_to"]

    def test_a_role_selector_retiers_every_matching_artifact(self, matrix_client):
        c = matrix_client()
        before = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        summaries = [a for a in before["artifacts"] if a["role"] == "paper_summary"]
        assert len(summaries) > 1, "fixture needs several summaries to make this meaningful"

        r = c.patch(
            f"/api/v1/profiles/{SLUG}/visibility",
            json={"artifacts": [{"role": "paper_summary", "visibility": "limited"}]},
            headers=VIEWER_HEADERS["owner"],
        )
        assert r.status_code == 200, r.text
        assert r.json()["artifacts_changed"] == len(summaries)

        after = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        assert all(
            a["effective"] == "limited" for a in after["artifacts"] if a["role"] == "paper_summary"
        )

    def test_a_selector_matching_nothing_is_a_400(self, matrix_client):
        c = matrix_client()
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/visibility",
            json={"artifacts": [{"role": "no-such-role", "visibility": "limited"}]},
            headers=VIEWER_HEADERS["owner"],
        )
        assert r.status_code == 400


def _add_derived_note(client, tmp_path) -> None:
    """Declare a public ``derived-note.md`` drawn from the CV, so it is raised to private."""
    pdir = tmp_path / "profiles-public-0" / SLUG
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
    def test_counts_are_the_consequence_a_person_reads(self, matrix_client):
        c = matrix_client()
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        assert report["counts"]["anonymous"] < report["counts"]["you"]
        # The hard floors are in nobody's count, the owner's included.
        floors = {ROLE_ARTIFACTS[r] for r in HARD_FLOORS}
        for a in report["artifacts"]:
            if a["content_url"] in floors:
                assert a["visible_to"] == []

    def test_why_names_the_specific_cause_on_the_row(self, matrix_client):
        c = matrix_client(visibility="limited")
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        soul = next(a for a in report["artifacts"] if a["role"] == "soul")
        assert soul["effective"] == "limited"
        assert any("profile default" in phrase for phrase in soul["raised_by"])

    def test_a_derived_artifact_names_its_source(self, matrix_client, tmp_path):
        c = matrix_client()
        _add_derived_note(c, tmp_path)

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


# ---------------------------------------------------------------------------
# The artifact route: the read plane a browser client needs
# ---------------------------------------------------------------------------


def _content(client, viewer, artifact, **kwargs):
    return _get(client, f"/api/v1/profiles/{SLUG}/content/{artifact}", viewer, **kwargs)


@pytest.mark.parametrize("profile_tier", ["public", "limited", "private"])
@pytest.mark.parametrize("viewer", VIEWER_KINDS)
class TestContentRouteMatrix:
    """``GET /profiles/{slug}/content/{artifact}``, tier by viewer by role.

    The tier gate, not a router-level owner gate, decides this route. An
    owner-or-nothing route would leave a hosted registry with no
    artifact-serving read plane at all: a client could fetch a profile document
    and then fail on every ``contentUrl`` inside it. The matrix is the same one
    the rest of this file asserts, so the artifact route cannot drift from the
    detail route.
    """

    def test_body_is_served_only_when_the_tier_allows(self, matrix_client, viewer, profile_tier):
        c = matrix_client(visibility=profile_tier)
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

    def test_a_retiered_artifact_is_served_at_its_declared_tier(self, matrix_client):
        c = matrix_client(overrides={"works": "limited"})
        owner = _content(c, "owner", ROLE_ARTIFACTS["works"])
        assert owner.status_code == 200, owner.text
        assert owner.headers["X-RP-Effective-Tier"] == "limited"
        lab = _content(c, "consumer_lab", ROLE_ARTIFACTS["works"])
        assert lab.status_code == 200, lab.text
        assert lab.headers["X-RP-Effective-Tier"] == "limited"
        assert _content(c, "anonymous", ROLE_ARTIFACTS["works"]).status_code == 404


class TestContentRouteRefusalsAreOpaque:
    def test_withheld_and_unknown_are_byte_identical(self, matrix_client):
        c = matrix_client()
        withheld = _content(c, "anonymous", "sources/cv.md")
        unknown = _content(c, "anonymous", "sources/not-a-file.md")
        assert withheld.status_code == unknown.status_code == 404
        assert withheld.json()["detail"].replace("sources/cv.md", "X") == unknown.json()[
            "detail"
        ].replace("sources/not-a-file.md", "X")

    def test_a_hidden_profile_hides_its_artifacts_the_same_way(self, matrix_client):
        c = matrix_client(visibility="limited")
        r = _content(c, "anonymous", "personality/SOUL.md")
        missing = _get(c, "/api/v1/profiles/no-such-slug/content/personality/SOUL.md", "anonymous")
        assert r.status_code == missing.status_code == 404

    def test_traversal_is_a_404(self, matrix_client):
        c = matrix_client()
        assert _content(c, "operator", "../other/profile.jsonld").status_code == 404


# ---------------------------------------------------------------------------
# The collection bundle
# ---------------------------------------------------------------------------


class TestProfileCollection:
    """``GET /collection.json``: ``GET /profiles`` in the shape a client fetches.

    The SPA's home view fetches a *document*, not an API call. Against a hosted
    registry there was no document at that URL, so the shell fell back to an
    empty state no matter who was signed in.
    """

    def _bundle(self, client, viewer):
        r = _get(client, "/api/v1/collection.json", viewer)
        assert r.status_code == 200, r.text
        return r.json()

    @pytest.mark.parametrize("profile_tier", ["public", "limited", "private"])
    @pytest.mark.parametrize("viewer", VIEWER_KINDS)
    def test_membership_matches_the_listing_exactly(self, matrix_client, viewer, profile_tier):
        c = matrix_client(visibility=profile_tier)
        # A registry with nothing visible is an empty bundle, not an error.
        bundle = self._bundle(c, viewer)
        listing = _get(c, "/api/v1/profiles", viewer).json()["profiles"]
        assert [card["slug"] for card in bundle["cards"]] == [p["slug"] for p in listing]
        assert bundle["count"] == len(listing)

    def test_a_card_base_resolves_the_manifest(self, matrix_client):
        c = matrix_client()
        card = self._bundle(c, "anonymous")["cards"][0]
        # ABSOLUTE, not root-relative: a client parses this with a bare URL
        # constructor, which has no document to resolve a relative path against.
        assert card["base"] == f"http://testserver/api/v1/profiles/{SLUG}/content/"
        doc = c.get(card["base"] + "profile.jsonld")
        assert doc.status_code == 200
        assert doc.json()["name"]

    def test_the_base_honours_the_proxy_the_client_actually_reached(self, matrix_client):
        """Behind TLS termination, ``base_url`` is ``http://`` and a browser on
        an ``https://`` page refuses to load it."""
        c = matrix_client()
        r = c.get(
            "/api/v1/collection.json",
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "profiles.example.org"},
        )
        card = r.json()["cards"][0]
        assert card["base"] == f"https://profiles.example.org/api/v1/profiles/{SLUG}/content/"

    def test_a_card_carries_the_corpus_stats_the_shell_renders(self, matrix_client):
        c = matrix_client()
        card = self._bundle(c, "anonymous")["cards"][0]
        summary = _get(c, "/api/v1/profiles", "anonymous").json()["profiles"][0]
        for field in ("name", "level", "affiliation", "field", "paper_count", "summary_count"):
            assert card[field] == summary[field], field

    def test_it_is_stamped_with_the_viewer_tier(self, matrix_client):
        c = matrix_client()
        assert _get(c, "/api/v1/collection.json", "operator").headers["X-RP-Viewer-Tier"] == (
            "private"
        )
        assert (
            _get(c, "/api/v1/collection.json", "anonymous").headers["X-RP-Viewer-Tier"] == "public"
        )


# ---------------------------------------------------------------------------
# The host floor hook, and its reason
# ---------------------------------------------------------------------------


class TestHostFloorHook:
    """``app.state.hooks.profile_tier_floor`` returns a ``TierFloor``: tier and reason.

    Decision and explanation are one value produced by one call, so the
    sentence can never describe a rule that did not run. A separate static
    wording string on ``app.state`` could tell an owner "nobody has claimed
    this profile" about a profile they claimed last week.
    """

    def test_a_floor_narrows_without_blacking_out_and_reports_its_reason(self, matrix_client):
        c = matrix_client()
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
    def test_no_floor_reports_no_reason(self, matrix_client, hook_set):
        c = matrix_client()
        if hook_set:
            c.app.state.hooks.profile_tier_floor = lambda caller, prof, slug: TierFloor()
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        assert report["profile_floor"] is None
        assert report["profile_floor_reason"] is None


# ---------------------------------------------------------------------------
# Caching a response that depends on the caller
# ---------------------------------------------------------------------------


class TestCredentialSensitiveCaching:
    """Every tier-projected body is a function of the caller's credentials.

    The one route with a shared-cache TTL is the anonymous rendering of
    ``profile.jsonld``, and its TTL is the bound on how long an unpublished
    profile can linger in a cache somewhere. Everything else is ``no-store``,
    but ``Vary`` is correct on all of it and cheap to state.
    """

    TIER_PROJECTED = (
        "/api/v1/profiles",
        "/api/v1/collection.json",
        f"/api/v1/profiles/{SLUG}",
        f"/api/v1/profiles/{SLUG}/profile.jsonld",
        f"/api/v1/profiles/{SLUG}/content/personality/SOUL.md",
    )

    @pytest.mark.parametrize("path", TIER_PROJECTED)
    def test_every_projected_route_varies_on_credentials(self, matrix_client, path):
        c = matrix_client()
        r = _get(c, path, "anonymous")
        assert r.status_code == 200, path
        assert r.headers["Vary"] == "Authorization, Cookie", path

    @pytest.mark.parametrize(
        "path, viewer, cache_control",
        [
            # The one shareable body: the anonymous document, for a minute.
            (f"/api/v1/profiles/{SLUG}/profile.jsonld", "anonymous", "public, max-age=60"),
            # Every other tier of the same document is never stored.
            (f"/api/v1/profiles/{SLUG}/profile.jsonld", "owner", "private, no-store"),
            (f"/api/v1/profiles/{SLUG}", "anonymous", "private, no-store"),
            (
                f"/api/v1/profiles/{SLUG}/content/personality/SOUL.md",
                "anonymous",
                "private, no-store",
            ),
            # Collection membership is a function of who asked, so one caller's
            # copy must never be handed to the next.
            ("/api/v1/collection.json", "anonymous", "private, no-store"),
        ],
        ids=["anonymous-document", "owner-document", "detail", "content", "collection"],
    )
    def test_only_the_anonymous_document_is_shareable(
        self, matrix_client, path, viewer, cache_control
    ):
        c = matrix_client()
        r = _get(c, path, viewer)
        assert r.status_code == 200, r.text
        assert r.headers["Cache-Control"] == cache_control

    def test_the_document_carries_a_strong_etag_that_revalidates(self, matrix_client):
        c = matrix_client()
        path = f"/api/v1/profiles/{SLUG}/profile.jsonld"
        etag = _get(c, path, "anonymous").headers["ETag"]
        assert etag.startswith('"') and etag.endswith('"')
        assert _get(c, path, "anonymous").headers["ETag"] == etag
        # A matching ETag revalidates to an empty 304.
        r = c.get(path, headers={**VIEWER_HEADERS["anonymous"], "If-None-Match": etag})
        assert r.status_code == 304
        assert r.content == b""
