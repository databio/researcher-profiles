"""The read projection: tier x viewer kind x artifact role, over HTTP.

Without ``privacy.effective_tiers`` on every read route, a profile whose
document says ``visibility: internal`` would be served byte-identically to one
that says ``public``, and the only thing standing between a hosted registry and
the open web would be a single site-wide switch. This file is the enforcement
half: every combination of

* a **profile-level tier** (``public`` / ``internal`` / ``restricted``),
* a **viewer kind** (anonymous, signed-in stranger, consumer key at ``public``
  and at ``internal``, operator, owner),
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

import pytest
from fastapi import HTTPException

from researcher_profiles import ResearcherProfile
from researcher_profiles.api.deps import ConsumerIdentity, TierFloor
from researcher_profiles.privacy import publishignore_lines
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
    "cv": "restricted",
    "web": "restricted",
    "embedding_index_sqlite": "restricted",
    "paper_fulltext": "restricted",
}

#: Withheld from EVERY viewer, owner included: a legal floor and build-local
#: derived state. These are the ``.`` rows that stay ``.`` all the way across.
HARD_FLOORS = frozenset({"embedding_index_sqlite", "paper_fulltext"})

VIEWER_TIERS = {
    "anonymous": "public",
    "stranger": "public",
    "consumer_public": "public",
    "consumer_lab": "internal",
    "operator": "restricted",
    "owner": "restricted",
}
VIEWER_KINDS = list(VIEWER_TIERS)

#: Tier ordering, lowest first. The one place it is written down.
TIER_ORDER = ("public", "internal", "restricted")

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

    Installs the ``X-Test-User`` owner-verifier stub from ``test_owner_read.py``
    and a consumer verifier that mints a tier per key, so all six viewer kinds
    are reachable from one app.
    """

    def _make(*, visibility: str = "public", overrides: dict | None = None):
        root = tmp_path / f"profiles-{visibility}-{len(overrides or {})}"
        root.mkdir(parents=True, exist_ok=True)
        _build_full_profile(root, visibility=visibility, overrides=overrides)
        c = make_api_client(root, token=OPERATOR_TOKEN)

        def _owner_verifier(request, slug):
            user = request.headers.get("X-Test-User")
            if not user:
                raise HTTPException(status_code=401, detail="login required")
            if user != "owner":
                raise HTTPException(status_code=403, detail="not the owner")

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

        c.app.state.owner_verifier = _owner_verifier
        c.app.state.consumer_verifier = _consumer_verifier
        return c

    return _make


SCOPES = ("read", "match", "persona", "push")
CONSUMER_KEYS = {"rpk_public": "public", "rpk_lab": "internal"}

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


def _expected_effective(role: str, profile_tier: str, declared: str | None = None) -> str:
    """The tier the matrix says an artifact resolves to. The rule, restated."""
    tiers = [profile_tier, declared or ROLE_DEFAULT[role]]
    if role == "paper_fulltext":
        tiers.append("restricted")
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


@pytest.mark.parametrize("profile_tier", ["public", "internal", "restricted"])
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

    def test_viewer_tier_is_stamped_on_every_response(self, matrix_client, viewer, profile_tier):
        c = matrix_client(visibility=profile_tier)
        r = _detail(c, viewer)
        assert r.headers["X-RP-Viewer-Tier"] == VIEWER_TIERS[viewer]


@pytest.mark.parametrize("viewer,profile_tier", VISIBLE_COMBOS)
@pytest.mark.parametrize("role", sorted(ROLE_ARTIFACTS))
class TestArtifactGate:
    """Gate two: which of its pieces does this viewer receive?"""

    def test_manifest_reports_the_effective_tier(self, matrix_client, viewer, profile_tier, role):
        c = matrix_client(visibility=profile_tier)
        r = _detail(c, viewer)
        assert r.status_code == 200, r.text
        entry = next(m for m in r.json()["manifest"] if m["contentUrl"] == ROLE_ARTIFACTS[role])
        assert entry["effective_visibility"] == _expected_effective(role, profile_tier)

    def test_withheld_names_exactly_what_was_not_served(
        self, matrix_client, viewer, profile_tier, role
    ):
        c = matrix_client(visibility=profile_tier)
        r = _detail(c, viewer)
        assert r.status_code == 200, r.text
        withheld = set(r.json()["withheld"])
        assert (ROLE_ARTIFACTS[role] in withheld) is not _sees(role, viewer, profile_tier)


@pytest.mark.parametrize("viewer,profile_tier", VISIBLE_COMBOS)
@pytest.mark.parametrize("role", ["soul", "expertise"])
class TestBodiesOnProfileDetail:
    """The two bodies ``ProfileDetail`` actually carries."""

    def test_body_is_served_only_when_the_tier_allows(
        self, matrix_client, viewer, profile_tier, role
    ):
        c = matrix_client(visibility=profile_tier)
        r = _detail(c, viewer)
        assert r.status_code == 200, r.text
        body = r.json()[role]
        if _sees(role, viewer, profile_tier):
            assert body
        else:
            # Withheld is None, never "": a client has to tell them apart.
            assert body is None


@pytest.mark.parametrize("declared", ["public", "internal", "restricted"])
@pytest.mark.parametrize("profile_tier", ["public", "internal", "restricted"])
class TestMostRestrictiveComposition:
    """A declared tier composes with the profile default; neither one wins alone."""

    def test_summary_tier_is_the_more_restrictive_of_the_two(
        self, matrix_client, declared, profile_tier
    ):
        c = matrix_client(visibility=profile_tier, overrides={"paper_summary": declared})
        r = _detail(c, "owner")
        entry = next(
            m for m in r.json()["manifest"] if m["contentUrl"] == ROLE_ARTIFACTS["paper_summary"]
        )
        assert entry["effective_visibility"] == _expected_effective(
            "paper_summary", profile_tier, declared
        )

    def test_an_anonymous_viewer_gets_it_only_when_both_are_public(
        self, matrix_client, declared, profile_tier
    ):
        c = matrix_client(visibility=profile_tier, overrides={"paper_summary": declared})
        r = _get(c, f"/api/v1/profiles/{SLUG}/summary/paperA", "anonymous")
        both_public = declared == "public" and profile_tier == "public"
        assert (r.status_code == 200) is both_public


class TestPapersAndSummaries:
    def test_works_withheld_404s_the_papers_list(self, matrix_client):
        c = matrix_client(overrides={"works": "restricted"})
        assert _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous").status_code == 404
        assert _get(c, f"/api/v1/profiles/{SLUG}/papers", "owner").status_code == 200

    def test_summary_available_reflects_this_viewer(self, matrix_client):
        """It must not advertise a summary the caller would then 404 on."""
        c = matrix_client(overrides={"paper_summary": "internal"})
        anon = _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous").json()
        lab = _get(c, f"/api/v1/profiles/{SLUG}/papers", "consumer_lab").json()
        assert all(p["summary_available"] is False for p in anon)
        assert any(p["summary_available"] for p in lab)
        assert _get(c, f"/api/v1/profiles/{SLUG}/summary/paperA", "anonymous").status_code == 404
        assert _get(c, f"/api/v1/profiles/{SLUG}/summary/paperA", "consumer_lab").status_code == 200


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
        c = matrix_client(visibility="restricted")
        held = _get(c, path.format(slug=SLUG), "anonymous")
        absent = _get(c, path.format(slug=SLUG), "anonymous")
        missing = _get(c, path.format(slug="nobody-at-all"), "anonymous")
        assert held.status_code == 404
        assert absent.status_code == 404
        # Same status AND same body: a distinguishable message is an oracle.
        assert held.json()["detail"].replace(SLUG, "nobody-at-all") == missing.json()["detail"]

    def test_papers_and_summary_refusals_are_404(self, matrix_client):
        c = matrix_client(visibility="internal")
        assert _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous").status_code == 404
        r = _get(c, f"/api/v1/profiles/{SLUG}/summary/paperA", "anonymous")
        assert r.status_code == 404


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
        c = matrix_client(overrides={"paper_summary": "internal"})
        owner_preview = _get(
            c, f"/api/v1/profiles/{SLUG}/papers", "owner", params={"as": "anonymous"}
        )
        anon = _get(c, f"/api/v1/profiles/{SLUG}/papers", "anonymous")
        assert owner_preview.json() == anon.json()
        assert (
            _get(
                c,
                f"/api/v1/profiles/{SLUG}/summary/paperA",
                "owner",
                params={"as": "anonymous"},
            ).status_code
            == 404
        )

    def test_the_cap_never_widens(self, matrix_client):
        """``?as=owner`` as an anonymous caller is still the anonymous view."""
        c = matrix_client()
        widened = _detail(c, "anonymous", params={"as": "owner"})
        plain = _detail(c, "anonymous")
        assert widened.json() == plain.json()
        assert widened.headers["X-RP-Viewer-Tier"] == "public"

    def test_lab_preview_sits_between(self, matrix_client):
        c = matrix_client(overrides={"paper_summary": "internal"})
        r = _get(c, f"/api/v1/profiles/{SLUG}/summary/paperA", "owner", params={"as": "lab"})
        assert r.status_code == 200
        assert r.headers["X-RP-Viewer-Tier"] == "internal"

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

    def test_public_caller_gets_nothing_in_the_publishignore(self, matrix_client, tmp_path):
        c = matrix_client()
        prof = ResearcherProfile.from_files(tmp_path / "profiles-public-0" / SLUG)
        excluded = set(publishignore_lines(prof.metadata))

        r = _get(c, f"/api/v1/profiles/{SLUG}/archive", "consumer_public")
        assert r.status_code == 200
        assert r.headers["X-RP-Archive-Tier"] == "public"
        members = self._members(r)
        assert members
        for name in members:
            assert name not in excluded, f"{name} is in .publishignore but shipped"
            assert not name.startswith(".cache/")

    def test_the_cv_ships_only_to_a_caller_entitled_to_it(self, matrix_client):
        c = matrix_client()
        public = self._members(_get(c, f"/api/v1/profiles/{SLUG}/archive", "consumer_public"))
        operator = self._members(_get(c, f"/api/v1/profiles/{SLUG}/archive", "operator"))
        assert "sources/cv.md" not in public
        assert "sources/cv.md" in operator

    def test_fulltext_ships_to_nobody(self, matrix_client):
        c = matrix_client()
        for viewer in ("consumer_public", "consumer_lab", "operator", "owner"):
            r = _get(c, f"/api/v1/profiles/{SLUG}/archive", viewer)
            if r.status_code != 200:
                continue
            assert not any(m.startswith("sources/papers/") for m in self._members(r)), (
                f"fulltext shipped to {viewer}"
            )


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

        def _search(self, query, k=5, filter=None):
            captured["filter"] = filter
            return [_Hit(t) for t in ("paper_summary", "cv", "web", "grant")]

        from researcher_profiles.profile.index import IndexManager

        monkeypatch.setattr(IndexManager, "search", _search, raising=False)
        return c, captured

    @pytest.mark.parametrize(
        "viewer, leaks_allowed",
        [("consumer_public", False), ("consumer_lab", False), ("operator", True)],
    )
    def test_restricted_chunks_are_dropped(self, searchable, viewer, leaks_allowed):
        c, _captured = searchable
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
            assert not (types & {"cv", "web", "grant"}), f"{viewer} was shown restricted chunks"

    def test_the_restriction_is_pushed_into_the_query(self, searchable):
        c, captured = searchable
        c.post(
            f"/api/v1/profiles/{SLUG}/search",
            json={"query": "anything"},
            headers=VIEWER_HEADERS["consumer_public"],
        )
        assert "cv" not in captured["filter"]["source_type"]


# ---------------------------------------------------------------------------
# Honest writes
# ---------------------------------------------------------------------------


class TestVisibilityWritesAreHonest:
    def test_loosening_fulltext_is_a_400_naming_the_floor(self, matrix_client):
        c = matrix_client()
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/visibility",
            json={"artifacts": [{"role": "paper_fulltext", "visibility": "public"}]},
            headers=VIEWER_HEADERS["owner"],
        )
        assert r.status_code == 400
        assert "legal floor" in r.json()["detail"]
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        fulltext = next(
            a for a in report["artifacts"] if a["content_url"] == ROLE_ARTIFACTS["paper_fulltext"]
        )
        assert fulltext["effective"] == "restricted"
        assert fulltext["locked"] is True

    def test_a_role_selector_retiers_every_matching_artifact(self, matrix_client):
        c = matrix_client()
        before = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        summaries = [a for a in before["artifacts"] if a["role"] == "paper_summary"]
        assert len(summaries) > 1, "fixture needs several summaries to make this meaningful"

        r = c.patch(
            f"/api/v1/profiles/{SLUG}/visibility",
            json={"artifacts": [{"role": "paper_summary", "visibility": "internal"}]},
            headers=VIEWER_HEADERS["owner"],
        )
        assert r.status_code == 200, r.text
        assert r.json()["artifacts_changed"] == len(summaries)

        after = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        assert all(
            a["effective"] == "internal" for a in after["artifacts"] if a["role"] == "paper_summary"
        )

    def test_a_selector_matching_nothing_is_a_400(self, matrix_client):
        c = matrix_client()
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/visibility",
            json={"artifacts": [{"role": "no-such-role", "visibility": "internal"}]},
            headers=VIEWER_HEADERS["owner"],
        )
        assert r.status_code == 400


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
        c = matrix_client(visibility="internal")
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        soul = next(a for a in report["artifacts"] if a["role"] == "soul")
        assert soul["effective"] == "internal"
        assert any("profile default" in phrase for phrase in soul["raised_by"])

    def test_a_derived_artifact_names_its_source(self, matrix_client, tmp_path):
        c = matrix_client()
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
        c.app.state.store.evict(SLUG)

        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        note = next(a for a in report["artifacts"] if a["content_url"] == "derived-note.md")
        assert note["declared"] == "public"
        assert note["effective"] == "restricted"
        assert any("sources/cv.md" in phrase for phrase in note["raised_by"])
        # And an anonymous reader never receives it.
        assert "derived-note.md" in set(_detail(c, "anonymous").json()["withheld"])


# ---------------------------------------------------------------------------
# The artifact route: the read plane a browser client needs
# ---------------------------------------------------------------------------


def _content(client, viewer, artifact, **kwargs):
    return _get(client, f"/api/v1/profiles/{SLUG}/content/{artifact}", viewer, **kwargs)


@pytest.mark.parametrize("profile_tier", ["public", "internal", "restricted"])
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

    @pytest.mark.parametrize("role", list(ROLE_ARTIFACTS))
    def test_body_is_served_only_when_the_tier_allows(
        self, matrix_client, viewer, profile_tier, role
    ):
        c = matrix_client(visibility=profile_tier)
        order = ("public", "internal", "restricted")
        profile_ok = order.index(profile_tier) <= order.index(VIEWER_TIERS[viewer])
        r = _content(c, viewer, ROLE_ARTIFACTS[role])

        if role in HARD_FLOORS:
            # 403 whether or not the caller may see the profile at all, except
            # that the profile gate runs first, so a caller who cannot see the
            # profile gets the profile's 404 and learns nothing.
            assert r.status_code == (403 if profile_ok else 404), r.text
            return
        if not profile_ok:
            assert r.status_code == 404
            return
        if _sees(role, viewer, profile_tier):
            assert r.status_code == 200, r.text
            assert r.headers["X-RP-Effective-Tier"] == _expected_effective(role, profile_tier)
        else:
            assert r.status_code == 404, r.text

    def test_the_document_resolves_from_the_content_base(self, matrix_client, viewer, profile_tier):
        """``/content/`` is a base URL: ``profile.jsonld`` resolves out of it.

        This is the single thing that makes one explorer build work against a
        hosted registry and a rendered static site alike. It points at a base
        and follows relative ``contentUrl``s from the document it finds there.
        """
        c = matrix_client(visibility=profile_tier)
        order = ("public", "internal", "restricted")
        profile_ok = order.index(profile_tier) <= order.index(VIEWER_TIERS[viewer])
        via_content = _content(c, viewer, "profile.jsonld")
        direct = _get(c, f"/api/v1/profiles/{SLUG}/profile.jsonld", viewer)
        assert via_content.status_code == direct.status_code == (200 if profile_ok else 404)
        assert via_content.content == direct.content


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
        c = matrix_client(visibility="internal")
        r = _content(c, "anonymous", "personality/SOUL.md")
        missing = _get(c, "/api/v1/profiles/no-such-slug/content/personality/SOUL.md", "anonymous")
        assert r.status_code == missing.status_code == 404

    def test_never_shared_cached(self, matrix_client):
        c = matrix_client()
        r = _content(c, "anonymous", "personality/SOUL.md")
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == "private, no-store"

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

    @pytest.mark.parametrize("profile_tier", ["public", "internal", "restricted"])
    @pytest.mark.parametrize("viewer", VIEWER_KINDS)
    def test_membership_matches_the_listing_exactly(self, matrix_client, viewer, profile_tier):
        c = matrix_client(visibility=profile_tier)
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

    def test_an_empty_registry_is_an_empty_bundle_not_an_error(self, matrix_client):
        c = matrix_client(visibility="restricted")
        bundle = self._bundle(c, "anonymous")
        assert bundle["cards"] == []
        assert bundle["count"] == 0

    def test_it_is_never_shared_cached(self, matrix_client):
        # Membership is a function of who asked, so one caller's copy must never
        # be handed to the next.
        c = matrix_client()
        r = _get(c, "/api/v1/collection.json", "anonymous")
        assert r.headers["Cache-Control"] == "private, no-store"

    def test_it_is_stamped_with_the_viewer_tier(self, matrix_client):
        c = matrix_client()
        assert _get(c, "/api/v1/collection.json", "operator").headers["X-RP-Viewer-Tier"] == (
            "restricted"
        )
        assert (
            _get(c, "/api/v1/collection.json", "anonymous").headers["X-RP-Viewer-Tier"] == "public"
        )


# ---------------------------------------------------------------------------
# The host floor hook, and its reason
# ---------------------------------------------------------------------------


class TestHostFloorHook:
    """``app.state.profile_tier_floor`` returns a ``TierFloor``: tier and reason.

    Decision and explanation are one value produced by one call, so the
    sentence can never describe a rule that did not run. A separate static
    wording string on ``app.state`` could tell an owner "nobody has claimed
    this profile" about a profile they claimed last week.
    """

    def test_a_floor_hides_a_public_profile_from_an_anonymous_caller(self, matrix_client):
        c = matrix_client()
        assert _detail(c, "anonymous").status_code == 200

        c.app.state.profile_tier_floor = lambda request, prof, slug: TierFloor(
            "internal", "held back"
        )
        assert _detail(c, "anonymous").status_code == 404
        assert _get(c, "/api/v1/profiles", "anonymous").json()["profiles"] == []

    def test_the_floor_is_a_narrowing_not_a_blackout(self, matrix_client):
        c = matrix_client()
        c.app.state.profile_tier_floor = lambda request, prof, slug: TierFloor(
            "internal", "held back"
        )
        assert _detail(c, "consumer_lab").status_code == 200
        assert _detail(c, "owner").status_code == 200

    def test_the_report_carries_the_reason_the_hook_returned(self, matrix_client):
        c = matrix_client()
        c.app.state.profile_tier_floor = lambda request, prof, slug: TierFloor(
            "internal", "you have not published this profile."
        )
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        assert report["profile_floor"] == "internal"
        assert report["profile_floor_reason"] == "you have not published this profile."

    def test_the_reason_varies_with_the_profile(self, matrix_client):
        """One static string could not do this, which is why it is gone."""
        c = matrix_client()
        c.app.state.profile_tier_floor = lambda request, prof, slug: TierFloor(
            "internal", f"{slug} is held back"
        )
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        assert report["profile_floor_reason"] == f"{SLUG} is held back"

    def test_no_floor_reports_no_reason(self, matrix_client):
        c = matrix_client()
        c.app.state.profile_tier_floor = lambda request, prof, slug: TierFloor()
        report = _get(c, f"/api/v1/profiles/{SLUG}/visibility", "owner").json()
        assert report["profile_floor"] is None
        assert report["profile_floor_reason"] is None

    def test_an_unset_hook_is_an_empty_floor(self, matrix_client):
        c = matrix_client()
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

    def test_the_anonymous_document_is_shareable_for_a_minute(self, matrix_client):
        c = matrix_client()
        r = _get(c, f"/api/v1/profiles/{SLUG}/profile.jsonld", "anonymous")
        assert r.headers["Cache-Control"] == "public, max-age=60"

    def test_every_other_tier_is_never_stored(self, matrix_client):
        c = matrix_client()
        r = _get(c, f"/api/v1/profiles/{SLUG}/profile.jsonld", "owner")
        assert r.headers["Cache-Control"] == "private, no-store"

    def test_the_document_carries_a_strong_etag_over_its_bytes(self, matrix_client):
        c = matrix_client()
        r = _get(c, f"/api/v1/profiles/{SLUG}/profile.jsonld", "anonymous")
        etag = r.headers["ETag"]
        assert etag.startswith('"') and etag.endswith('"')
        again = _get(c, f"/api/v1/profiles/{SLUG}/profile.jsonld", "anonymous")
        assert again.headers["ETag"] == etag

    def test_a_matching_etag_revalidates_to_304(self, matrix_client):
        c = matrix_client()
        etag = _get(c, f"/api/v1/profiles/{SLUG}/profile.jsonld", "anonymous").headers["ETag"]
        r = c.get(
            f"/api/v1/profiles/{SLUG}/profile.jsonld",
            headers={**VIEWER_HEADERS["anonymous"], "If-None-Match": etag},
        )
        assert r.status_code == 304
        assert r.content == b""

    def test_the_detail_route_is_never_stored(self, matrix_client):
        c = matrix_client()
        assert _detail(c, "anonymous").headers["Cache-Control"] == "private, no-store"
