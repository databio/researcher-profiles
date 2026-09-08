"""Artifact read: ``GET /api/v1/profiles/{slug}/content/{artifact}``.

The route lives on ``public_router`` and the gate is the caller's tier, not
ownership. A hosted registry keeps the whole profile, every tier, in one
store, so an owner-only door would leave the public tier unreachable. The
owner still reads their own ``restricted`` CV, because their viewer tier is
``restricted``; nobody else does.

What the route must do, and every assertion below says it:

- serve an owner their own ``internal`` and ``restricted`` (non-floor) artifacts;
- withhold the hard floors of ``spec/privacy-tiers.md`` section 4 even from the
  owner (``paper_fulltext``, ``.cache/``/``.keys/``): a **403**, since those are
  withheld from everyone and nothing is disclosed by saying so;
- withhold an artifact above the caller's tier as a **404** indistinguishable
  from an artifact that is not in the manifest at all;
- report the *effective* tier (derivation rule applied), not the declared one.

The stub ``owner_verifier`` is the ``X-Test-User`` shape shared with
``test_edit.py``; ``deps.resolve_viewer_tier`` reads it, so "owner" resolves to
the ``restricted`` viewer tier and everyone else falls through to ``public``.
"""

import pytest
from fastapi import HTTPException

from researcher_profiles import ResearcherProfile
from researcher_profiles.schema import ArtifactRef

from .factories import ADA, build_profile_dir

SLUG = "test-researcher"


def _build_owned_profile(root):
    """Materialize a profile exercising every tier the route must distinguish.

    Public: paper summaries, works. Internal: the works list, re-tiered.
    Restricted (default): ``sources/cv.md``, ``sources/web/*.md``. Hard floors:
    a planted ``paper_fulltext`` and the ``.cache/embeddings.sqlite`` index.
    Plus a ``public``-declared artifact ``derivedFrom`` the restricted CV, whose
    effective tier is restricted: the derivation-rule probe.
    """
    pdir = root / SLUG
    build_profile_dir(
        pdir,
        rid=ADA,
        cv=True,
        web=True,
        index="sqlite",
        manifest=True,
    )

    # A copyrighted-fulltext artifact (hard floor), added after the composite
    # build, then re-recorded in the manifest.
    (pdir / "sources" / "papers").mkdir(parents=True, exist_ok=True)
    (pdir / "sources" / "papers" / "p1.md").write_text("full text\n", encoding="utf-8")
    ResearcherProfile.from_files(pdir).build_manifest(write=True)

    # Re-tier the works list to `internal`, and add a public-declared artifact
    # derived from the restricted CV (effective tier -> restricted).
    prof = ResearcherProfile.from_files(pdir)
    for p in prof.metadata.has_part:
        if p.content_url == "sources/papers.jsonld":
            p.visibility = "internal"
    (pdir / "derived-note.md").write_text("notes drawn from the CV\n", encoding="utf-8")
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
    return pdir


@pytest.fixture
def owned_client(make_api_client, tmp_path):
    """A client over the rich profile, granting ownership of SLUG to 'owner'.

    Same hook and header convention as ``test_edit.py``: 'owner' owns SLUG, any
    other value is a logged-in non-owner, absence is no session.
    """
    root = tmp_path / "profiles"
    root.mkdir(parents=True, exist_ok=True)
    _build_owned_profile(root)
    c = make_api_client(root)

    def _verifier(request, slug):
        user = request.headers.get("X-Test-User")
        if not user:
            raise HTTPException(status_code=401, detail="login required")
        if user != "owner":
            raise HTTPException(status_code=403, detail="not the owner")

    c.app.state.owner_verifier = _verifier
    return c


def _get(client, artifact, *, user="owner"):
    headers = {"X-Test-User": user} if user else {}
    return client.get(f"/api/v1/profiles/{SLUG}/content/{artifact}", headers=headers)


class TestOwnerReadsOwnPrivateArtifacts:
    """(a) The owner reads their own internal / restricted artifacts."""

    def test_owner_reads_restricted_cv(self, owned_client):
        r = _get(owned_client, "sources/cv.md")
        assert r.status_code == 200, r.text
        assert r.headers["X-RP-Effective-Tier"] == "restricted"
        assert "Education" in r.text

    def test_owner_reads_restricted_web_page(self, owned_client):
        r = _get(owned_client, "sources/web/1-lab.md")
        assert r.status_code == 200, r.text
        assert r.headers["X-RP-Effective-Tier"] == "restricted"

    def test_owner_reads_internal_works(self, owned_client):
        r = _get(owned_client, "sources/papers.jsonld")
        assert r.status_code == 200, r.text
        assert r.headers["X-RP-Effective-Tier"] == "internal"


class TestHardFloorsWithheldFromOwner:
    """(b) Even the owner cannot read a hard-floor artifact."""

    def test_paper_fulltext_denied(self, owned_client):
        r = _get(owned_client, "sources/papers/p1.md")
        assert r.status_code == 403, r.text

    def test_cache_index_denied(self, owned_client):
        r = _get(owned_client, ".cache/embeddings.sqlite")
        assert r.status_code == 403, r.text


class TestNonOwnerAndAnonymousDenied:
    """(c) A non-owner or anonymous request for a non-public artifact is denied."""

    def test_anonymous_denied(self, owned_client):
        # 404, not 401: an anonymous caller is the viewer whose tier is
        # ``public``, and a restricted artifact is not there for them. That is
        # the same answer a path outside the manifest gets (see
        # ``test_a_withheld_artifact_is_indistinguishable_from_an_unknown_one``).
        r = _get(owned_client, "sources/cv.md", user=None)
        assert r.status_code == 404, r.text

    def test_non_owner_denied(self, owned_client):
        r = _get(owned_client, "sources/cv.md", user="someone-else")
        assert r.status_code == 404, r.text

    def test_a_withheld_artifact_is_indistinguishable_from_an_unknown_one(self, owned_client):
        """The refusal must not enumerate the private half of the profile."""
        withheld = _get(owned_client, "sources/cv.md", user=None)
        unknown = _get(owned_client, "sources/no-such-file.md", user=None)
        assert withheld.status_code == unknown.status_code == 404
        assert withheld.json()["detail"].replace("sources/cv.md", "X") == unknown.json()[
            "detail"
        ].replace("sources/no-such-file.md", "X")

    def test_a_public_artifact_is_served_to_a_non_owner(self, owned_client):
        # The route is not owner-or-nothing. A public-tier artifact reached
        # through it is served to the public
        # viewer, which is what lets a browser client resolve a manifest.
        r = _get(owned_client, "sources/summaries/paperA.summary.md", user="someone-else")
        assert r.status_code == 200, r.text
        assert r.headers["X-RP-Effective-Tier"] == "public"


class TestPublicSurfaceUnchanged:
    """(d) The public read surface still serves public content anonymously."""

    def test_public_summary_readable_anonymously(self, owned_client):
        r = owned_client.get(f"/api/v1/profiles/{SLUG}/summary/paperA")
        assert r.status_code == 200, r.text
        assert r.json()["paper_id"] == "paperA"

    def test_owner_route_serves_public_artifact_to_owner(self, owned_client):
        r = _get(owned_client, "sources/summaries/paperA.summary.md")
        assert r.status_code == 200, r.text
        assert r.headers["X-RP-Effective-Tier"] == "public"


class TestManifestBaseUrl:
    """``/content/`` is a BASE URL: profile.jsonld resolves out of it too."""

    def test_profile_jsonld_through_the_content_route(self, owned_client):
        via_content = _get(owned_client, "profile.jsonld")
        direct = owned_client.get(f"/api/v1/profiles/{SLUG}/profile.jsonld")
        assert via_content.status_code == 200, via_content.text
        assert via_content.content == direct.content
        assert via_content.headers["content-type"].startswith("application/ld+json")

    def test_the_document_is_never_shared_cached_through_this_route(self, owned_client):
        r = _get(owned_client, "profile.jsonld")
        assert r.headers["Cache-Control"] == "private, no-store"


class TestDerivationRuleRespected:
    """(e) A public-declared derivative of a restricted source reads as restricted."""

    def test_derived_artifact_effective_tier_is_restricted(self, owned_client):
        r = _get(owned_client, "derived-note.md")
        # The owner is entitled to restricted, so this is served, but the
        # header reports the derived (restricted) tier, not the declared public
        # one. The tier gate uses that same derived tier, so a caller at the
        # public tier does not get it despite its ``visibility: public``.
        assert r.status_code == 200, r.text
        assert r.headers["X-RP-Effective-Tier"] == "restricted"
        assert _get(owned_client, "derived-note.md", user=None).status_code == 404


class TestUnknownArtifact:
    """A path outside the manifest (incl. traversal) is a 404, not a file read."""

    def test_unknown_path_404(self, owned_client):
        r = _get(owned_client, "sources/secret.md")
        assert r.status_code == 404, r.text

    def test_traversal_denied(self, owned_client):
        r = _get(owned_client, "../other/profile.jsonld")
        assert r.status_code == 404, r.text
