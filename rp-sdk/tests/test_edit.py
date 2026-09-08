"""Owner-scoped interactive edits: the ``edit`` helpers and the edit endpoints.

Two layers:

- :mod:`researcher_profiles.profile.edit`: the on-disk mutation helpers (patch metadata,
  set soul, set visibility), including the re-validation guarantee and the
  legal ``paper_fulltext`` restricted pin.
- the ``PATCH/PUT /api/v1/profiles/{slug}/...`` endpoints via ``require_owner``:
  the operator-token fallback on bare rp-sdk, and the owner/non-owner/no-session
  behavior when a management host installs an ``owner_verifier``.
"""

from pathlib import Path

import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.errors import ProfileWriteError
from researcher_profiles.profile.edit import (
    EditError,
)
from researcher_profiles.profile.storage import DirectoryArtifactStorage

SLUG = "jane-doe"


# ---------------------------------------------------------------------------
# edit.py helpers (on-disk, no HTTP)
# ---------------------------------------------------------------------------


class TestEditHelpers:
    def test_patch_metadata_persists_and_revalidates(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.patch_metadata(
            {"name": "Jane Q. Doe", "field": "Genomics", "expertise": ["ATAC-seq"]}
        )
        # Written to disk: a freshly loaded profile sees the change.
        reloaded = ResearcherProfile.from_files(jane_doe_dir)
        assert reloaded.name == "Jane Q. Doe"
        assert reloaded.field == "Genomics"
        assert reloaded.metadata.expertise == ["ATAC-seq"]

    def test_patch_rejects_non_editable_field(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        with pytest.raises(EditError, match="not owner-editable"):
            prof.edit.patch_metadata({"rid": "0000-0002-1825-0097", "name": "X"})
        # Nothing written: name unchanged on disk.
        assert ResearcherProfile.from_files(jane_doe_dir).name == prof.name

    def test_patch_invalid_value_leaves_file_untouched(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        original = (jane_doe_dir / "profile.jsonld").read_text()
        # name has min_length=1: empty string fails schema re-validation.
        with pytest.raises(EditError, match="invalid"):
            prof.edit.patch_metadata({"name": ""})
        assert (jane_doe_dir / "profile.jsonld").read_text() == original

    def test_set_soul_writes_markdown(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.set_soul("# New soul\nI think in graphs.")
        assert (jane_doe_dir / "personality" / "SOUL.md").read_text().startswith("# New soul")
        assert ResearcherProfile.from_files(jane_doe_dir).soul.startswith("# New soul")

    def test_set_profile_visibility(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.set_visibility(profile_visibility="internal")
        assert ResearcherProfile.from_files(jane_doe_dir).metadata.visibility == "internal"

    def test_set_visibility_rejects_bad_tier(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        with pytest.raises(EditError, match="invalid visibility"):
            prof.edit.set_visibility(profile_visibility="secret")

    def test_artifact_visibility_by_role(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.set_visibility(artifacts=[{"role": "soul", "visibility": "internal"}])
        reloaded = ResearcherProfile.from_files(jane_doe_dir)
        soul_ref = [p for p in reloaded.metadata.subject_of if p.role == "soul"][0]
        assert soul_ref.visibility == "internal"

    def test_paper_fulltext_cannot_be_loosened(self, jane_doe_dir):
        """The legal floor refuses rather than accepting and silently re-pinning.

        Returning success and letting the schema quietly re-pin the tier on
        re-validation would make the API report a change it had not made. A
        floor that lies about itself is worse than no floor.
        """
        prof = ResearcherProfile.from_files(jane_doe_dir)
        with pytest.raises(EditError, match="legal floor"):
            prof.edit.set_visibility(artifacts=[{"role": "paper_fulltext", "visibility": "public"}])
        reloaded = ResearcherProfile.from_files(jane_doe_dir)
        ft = [p for p in reloaded.metadata.has_part if p.role == "paper_fulltext"]
        assert ft and all(p.visibility == "restricted" for p in ft)

    def test_metadata_patch_stamps_date_modified(self, jane_doe_dir):
        import json

        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.patch_metadata({"field": "Epigenomics"})
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        assert "dateModified" in doc

    def test_metadata_patch_no_op_preserves_stamp(self, jane_doe_dir):
        import json

        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.patch_metadata({"name": prof.name})
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        first_stamp = doc.get("dateModified")
        prof2 = ResearcherProfile.from_files(jane_doe_dir)
        prof2.edit.patch_metadata({"name": prof2.name})
        doc2 = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        assert doc2.get("dateModified") == first_stamp

    def test_visibility_change_stamps_date_modified(self, jane_doe_dir):
        import json

        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.set_visibility(profile_visibility="internal")
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        assert "dateModified" in doc


class TestEditAgainstAReadOnlyBackend:
    """Writes against a read-only backend raise instead of touching the disk.

    An HTTP-backed profile has no filesystem home. Without the storage layer
    in the way, ``set_soul`` on one would run
    ``Path("/remote/<slug>/personality").mkdir(parents=True)``, an attempt to
    create directories at the filesystem root, with nothing raised.
    """

    def _static(self):
        return ResearcherProfile.from_url("https://example.org/profiles/jane-doe")

    def test_set_soul_raises_and_creates_nothing(self):
        prof = self._static()
        with pytest.raises(EditError):
            prof.edit.set_soul("# a soul nobody asked for\n")
        assert not Path("/static/jane-doe").exists()

    def _read_only_over(self, path):
        """Reads work, writes are refused: the write half of the storage swapped.

        A ``StaticArtifactStorage`` would need a live host to answer the read these two
        operations do first, so the refusal is modelled locally.
        """

        class ReadOnlyProfile(ResearcherProfile):
            def write_unit(self, kind):
                raise ProfileWriteError(self.locate(kind), "read-only view")

        return ReadOnlyProfile(DirectoryArtifactStorage(path))

    def test_metadata_patch_raises_and_writes_nothing(self, jane_doe_dir):
        prof = self._read_only_over(jane_doe_dir)
        before = (jane_doe_dir / "profile.jsonld").read_text()
        with pytest.raises(EditError):
            prof.edit.patch_metadata({"name": "Jane Q. Doe"})
        assert (jane_doe_dir / "profile.jsonld").read_text() == before

    def test_set_visibility_raises_and_writes_nothing(self, jane_doe_dir):
        prof = self._read_only_over(jane_doe_dir)
        before = (jane_doe_dir / "profile.jsonld").read_text()
        with pytest.raises(EditError):
            prof.edit.set_visibility(profile_visibility="internal")
        assert (jane_doe_dir / "profile.jsonld").read_text() == before


# ---------------------------------------------------------------------------
# Edit endpoints via require_owner
# ---------------------------------------------------------------------------


class TestEditEndpointsOperatorFallback:
    """Bare rp-sdk (no owner_verifier): edit endpoints fall back to the token."""

    def test_edit_open_when_no_token_configured(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        r = c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Genomics"})
        assert r.status_code == 200, r.text
        assert r.json()["updated"] == ["field"]

    def test_edit_requires_operator_token_when_configured(
        self, make_api_client, fixture_profiles_root
    ):
        c = make_api_client(fixture_profiles_root(SLUG), token="op-secret")
        r = c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Genomics"})
        assert r.status_code == 401
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/metadata",
            json={"field": "Genomics"},
            headers={"Authorization": "Bearer op-secret"},
        )
        assert r.status_code == 200, r.text

    def test_metadata_patch_rejects_non_editable(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/metadata",
            json={"rid": "0000-0002-1825-0097"},
        )
        # rid is not a MetadataPatch field -> extra=allow lets it through the
        # model, but the edit whitelist rejects it as a 400.
        assert r.status_code == 400
        assert "not owner-editable" in r.json()["detail"]

    def test_soul_and_visibility_endpoints(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        r = c.put(f"/api/v1/profiles/{SLUG}/soul", json={"soul": "new soul body"})
        assert r.status_code == 200, r.text
        assert r.json()["updated"] == ["soul"]
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/visibility",
            json={"profile_visibility": "internal"},
        )
        assert r.status_code == 200, r.text
        assert "visibility" in r.json()["updated"]


class TestEditEndpointsOwnerScoped:
    """A management host installs an owner_verifier -> owner-scoped auth."""

    @pytest.fixture
    def owned_client(self, make_api_client, fixture_profiles_root):
        """A client whose app grants ownership of SLUG to 'owner', nobody else.

        The stub verifier reads an ``X-Test-User`` header: 'owner' owns SLUG,
        any other value is a logged-in non-owner, and its absence is no session.
        """
        from fastapi import HTTPException

        c = make_api_client(fixture_profiles_root(SLUG))

        def _verifier(request, slug):
            user = request.headers.get("X-Test-User")
            if not user:
                raise HTTPException(status_code=401, detail="login required")
            if user != "owner":
                raise HTTPException(status_code=403, detail="not the owner")

        c.app.state.owner_verifier = _verifier
        return c

    def test_owner_can_edit(self, owned_client):
        r = owned_client.patch(
            f"/api/v1/profiles/{SLUG}/metadata",
            json={"field": "Genomics"},
            headers={"X-Test-User": "owner"},
        )
        assert r.status_code == 200, r.text

    def test_non_owner_forbidden(self, owned_client):
        r = owned_client.patch(
            f"/api/v1/profiles/{SLUG}/metadata",
            json={"field": "Genomics"},
            headers={"X-Test-User": "someone-else"},
        )
        assert r.status_code == 403

    def test_no_session_unauthorized(self, owned_client):
        r = owned_client.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Genomics"})
        assert r.status_code == 401


class TestAuthoredHistoryIsEditable:
    """``training`` / ``career`` / ``job_title``: the first-run gap.

    Nothing in the pipeline supplies them, the viewer renders them
    prominently, and until they joined the whitelist a person filling in a
    brand-new profile could not enter their own degrees.
    """

    def test_training_and_career_round_trip(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.patch_metadata(
            {
                "job_title": "Associate Professor",
                "training": [
                    {
                        "kind": "degree",
                        "degree": "PhD",
                        "institution": "Duke University",
                        "year_end": 2011,
                    },
                    {
                        "kind": "postdoc",
                        "degree": "Postdoctoral Fellow",
                        "institution": "Broad Institute",
                        "year_start": 2011,
                        "year_end": 2015,
                    },
                ],
                "career": [
                    {"role": "Assistant Professor", "institution": "UVA", "start_year": 2015},
                ],
            },
        )
        reloaded = ResearcherProfile.from_files(jane_doe_dir)
        assert reloaded.metadata.job_title == "Associate Professor"
        assert [t.kind for t in reloaded.metadata.training] == ["degree", "postdoc"]
        assert reloaded.metadata.training[0].institution == "Duke University"
        assert reloaded.metadata.career[0].role == "Assistant Professor"
        # An open-ended position stays open-ended rather than being defaulted.
        assert reloaded.metadata.career[0].end_year is None

    def test_malformed_career_entry_is_rejected_and_writes_nothing(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        before = (jane_doe_dir / "profile.jsonld").read_bytes()
        with pytest.raises(EditError, match=r"career\[0\]"):
            prof.edit.patch_metadata({"career": [{"institution": "UVA"}]})
        assert (jane_doe_dir / "profile.jsonld").read_bytes() == before

    def test_training_must_be_a_list(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        with pytest.raises(EditError, match="must be a list"):
            prof.edit.patch_metadata({"training": {"kind": "degree"}})

    def test_expertise_narrative_is_still_not_editable(self, jane_doe_dir):
        """``metadata.expertise`` (labels) is editable; the narrative is not.

        Widening the whitelist must not be read as widening it to the
        pipeline-synthesized ``personality/expertise.md``.
        """
        prof = ResearcherProfile.from_files(jane_doe_dir)
        with pytest.raises(EditError, match="not owner-editable"):
            prof.edit.patch_metadata({"expertise_md": "# mine now"})


class TestConcurrencyToken:
    """``base_hash`` -> 409. Opt-in: no token means last-writer-wins."""

    def test_detail_carries_a_content_hash(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        detail = c.get(f"/api/v1/profiles/{SLUG}").json()
        assert detail["content_hash"].startswith("sha256:")

    def test_matching_base_hash_is_accepted_and_returns_the_new_hash(
        self, make_api_client, fixture_profiles_root
    ):
        c = make_api_client(fixture_profiles_root(SLUG))
        before = c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"]
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/metadata",
            json={"field": "Genomics", "base_hash": before},
        )
        assert r.status_code == 200, r.text
        after = r.json()["content_hash"]
        assert after and after != before
        assert c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"] == after
        # base_hash is bookkeeping, not a field: it must not be reported as one.
        assert r.json()["updated"] == ["field"]

    def test_stale_base_hash_is_a_409_and_changes_nothing(
        self, make_api_client, fixture_profiles_root
    ):
        c = make_api_client(fixture_profiles_root(SLUG))
        stale = c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"]
        c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Somebody Else's Edit"})
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/metadata",
            json={"field": "Genomics", "base_hash": stale},
        )
        assert r.status_code == 409, r.text
        assert "changed since you loaded it" in r.json()["detail"]
        assert r.headers["X-RP-Content-Hash"].startswith("sha256:")
        assert c.get(f"/api/v1/profiles/{SLUG}").json()["metadata"]["field"] == (
            "Somebody Else's Edit"
        )

    def test_omitting_base_hash_stays_last_writer_wins(
        self, make_api_client, fixture_profiles_root
    ):
        c = make_api_client(fixture_profiles_root(SLUG))
        c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "First"})
        r = c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Second"})
        assert r.status_code == 200, r.text
        assert c.get(f"/api/v1/profiles/{SLUG}").json()["metadata"]["field"] == "Second"

    def test_soul_and_metadata_share_one_clock(self, make_api_client, fixture_profiles_root):
        """The digest spans document + SOUL, so a soul write stales a pending metadata patch."""
        c = make_api_client(fixture_profiles_root(SLUG))
        stale = c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"]
        r = c.put(f"/api/v1/profiles/{SLUG}/soul", json={"soul": "a new voice"})
        assert r.status_code == 200, r.text
        assert r.json()["content_hash"] != stale
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/metadata",
            json={"field": "Genomics", "base_hash": stale},
        )
        assert r.status_code == 409

    def test_stale_soul_write_is_a_409(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        stale = c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"]
        c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Somebody Else's Edit"})
        r = c.put(
            f"/api/v1/profiles/{SLUG}/soul",
            json={"soul": "mine", "base_hash": stale},
        )
        assert r.status_code == 409

    def test_stale_visibility_patch_is_a_409_and_changes_nothing(
        self, make_api_client, fixture_profiles_root
    ):
        c = make_api_client(fixture_profiles_root(SLUG))
        stale = c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"]
        c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Somebody Else's Edit"})
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/visibility",
            json={"profile_visibility": "internal", "base_hash": stale},
        )
        assert r.status_code == 409, r.text
        assert r.headers["X-RP-Content-Hash"].startswith("sha256:")
        assert c.get(f"/api/v1/profiles/{SLUG}/visibility").json()["profile_visibility"] != (
            "internal"
        )

    def test_matching_visibility_base_hash_is_accepted(
        self, make_api_client, fixture_profiles_root
    ):
        c = make_api_client(fixture_profiles_root(SLUG))
        before = c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"]
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/visibility",
            json={"profile_visibility": "internal", "base_hash": before},
        )
        assert r.status_code == 200, r.text
        assert r.json()["updated"] == ["visibility"]
        assert r.json()["content_hash"] != before


class TestAuthoredHistoryOverHttp:
    def test_patch_and_read_back_typed(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/metadata",
            json={
                "job_title": "Associate Professor",
                "interests": ["single-cell"],
                "not_interests": ["grant admin"],
                "same_as": ["https://example.org/jane"],
                "training": [
                    {"kind": "degree", "degree": "PhD", "institution": "Duke", "year_end": 2011}
                ],
                "career": [{"role": "PI", "institution": "UVA", "start_year": 2015}],
            },
        )
        assert r.status_code == 200, r.text
        md = c.get(f"/api/v1/profiles/{SLUG}").json()["metadata"]
        assert md["job_title"] == "Associate Professor"
        assert md["interests"] == ["single-cell"]
        assert md["not_interests"] == ["grant admin"]
        assert md["same_as"] == ["https://example.org/jane"]
        assert md["training"][0]["degree"] == "PhD"
        assert md["career"][0]["role"] == "PI"
        # The document's own tier is readable, so an editor can say what it is
        # without guessing. It is not settable here.
        assert md["visibility"] in {"public", "internal", "restricted"}

    def test_malformed_training_over_http_is_a_400(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        before = c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"]
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/metadata",
            json={"training": [{"kind": "sabbatical", "degree": "x", "institution": "y"}]},
        )
        assert r.status_code == 400, r.text
        assert "training[0]" in r.json()["detail"]
        assert c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"] == before

    def test_visibility_cannot_be_set_through_a_metadata_patch(
        self, make_api_client, fixture_profiles_root
    ):
        """The one place that governs who may read a profile stays one place."""
        c = make_api_client(fixture_profiles_root(SLUG))
        r = c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"visibility": "public"})
        assert r.status_code == 400
        assert "not owner-editable" in r.json()["detail"]
