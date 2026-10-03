"""Owner-scoped interactive edits: the ``edit`` helpers and the edit endpoints.

Two layers:

- :mod:`researcher_profiles.profile.edit`: the on-disk mutation helpers (patch metadata,
  set soul, set visibility, patch/add/remove one work), including the
  re-validation guarantee and the legal ``paper_fulltext`` private pin.
- the ``PATCH/PUT /api/v1/profiles/{slug}/...`` endpoints via ``require_owner``:
  the operator-token fallback on bare rp-sdk, and the owner/non-owner/no-session
  behavior when a management host installs an ``owner_verifier``.
"""

import pytest

from researcher_profiles import ResearcherProfile
from researcher_profiles.profile.edit import (
    EDITABLE_METADATA_FIELDS,
    LOCKED_METADATA_FIELDS,
    EditError,
)
from researcher_profiles.schema import ProfileDocument

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
        prof.edit.set_visibility(profile_visibility="limited")
        assert ResearcherProfile.from_files(jane_doe_dir).metadata.visibility == "limited"

    def test_set_visibility_rejects_bad_tier(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        with pytest.raises(EditError, match="invalid visibility"):
            prof.edit.set_visibility(profile_visibility="secret")

    def test_artifact_visibility_by_role(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.set_visibility(artifacts=[{"role": "soul", "visibility": "limited"}])
        reloaded = ResearcherProfile.from_files(jane_doe_dir)
        soul_ref = [p for p in reloaded.metadata.subject_of if p.role == "soul"][0]
        assert soul_ref.visibility == "limited"

    def test_paper_fulltext_tier_is_choosable(self, jane_doe_dir):
        """paper_fulltext is an ordinary role: the owner may raise it to public.

        It defaults to private, but that is a default, not a floor. Setting
        it to public succeeds and the change survives a reload.
        """
        prof = ResearcherProfile.from_files(jane_doe_dir)
        _doc, changed = prof.edit.set_visibility(
            artifacts=[{"role": "paper_fulltext", "visibility": "public"}]
        )
        assert changed >= 1
        reloaded = ResearcherProfile.from_files(jane_doe_dir)
        ft = [p for p in reloaded.metadata.has_part if p.role == "paper_fulltext"]
        assert ft and all(p.visibility == "public" for p in ft)

    def test_soul_section_retiers_the_soul_artifact(self, jane_doe_dir):
        """The `soul` section is the single owner-facing knob for SOUL.md.

        SOUL is a manifest artifact, not an inline field, so a section tier
        that only wrote `section_visibility` would be a dead knob: the file
        would keep whatever tier its ArtifactRef declared. Setting the section
        must therefore re-tier every `soul` part to match.
        """
        prof = ResearcherProfile.from_files(jane_doe_dir)
        _doc, changed = prof.edit.set_visibility(
            sections=[{"section": "soul", "visibility": "limited"}]
        )
        assert changed >= 1
        reloaded = ResearcherProfile.from_files(jane_doe_dir)
        soul_refs = [p for p in reloaded.metadata.subject_of if p.role == "soul"]
        assert soul_refs and all(p.visibility == "limited" for p in soul_refs)
        declared = {x.section: x.visibility for x in reloaded.metadata.section_visibility}
        assert declared["soul"] == "limited"

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
        prof.edit.set_visibility(profile_visibility="limited")
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        assert "dateModified" in doc


class TestWorkEdits:
    """``patch_work`` / ``add_work`` / ``remove_work``: one record at a time.

    The corpus used to be reachable only through ``rp push``, which replaces
    the whole profile directory. These are the field-level writes, and what
    they owe is the same thing ``patch_metadata`` owes: a refused patch leaves
    the file exactly as it was.
    """

    PAPER = "doe2016example"

    @staticmethod
    def _work(c, paper_id):
        """One work as the public read serves it (full view), or ``None`` on a 404."""
        r = c.get(f"/api/v1/profiles/{SLUG}/papers/{paper_id}", params={"view": "full"})
        return None if r.status_code == 404 else r.json()

    def test_patch_reports_the_fields_it_applied(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/works/{self.PAPER}",
            json={"doi": "10.1038/s41586-023-06000-1", "datePublished": 2017},
        )
        assert r.status_code == 200, r.text
        assert r.json()["updated"] == ["datePublished", "doi"]
        record = self._work(c, self.PAPER)
        assert record["fields"]["doi"] == "10.1038/s41586-023-06000-1"
        assert record["fields"]["datePublished"] == "2017"
        # The reply carries the paper's new version, the next edit's token.
        assert r.json()["version"] == record["version"]

    def test_disallowed_field_is_a_400(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        r = c.patch(f"/api/v1/profiles/{SLUG}/works/{self.PAPER}", json={"cited_by_count": 9000})
        assert r.status_code == 400, r.text
        assert "not owner-editable" in r.json()["detail"]

    def test_unknown_paper_is_a_404(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        r = c.patch(f"/api/v1/profiles/{SLUG}/works/nobody2099", json={"doi": "10.1/x"})
        assert r.status_code == 404, r.text
        assert self._work(c, "nobody2099") is None

    def test_stale_base_version_is_a_409_and_changes_nothing(
        self, make_api_client, fixture_profiles_root
    ):
        """The paper's own version guards it, whatever else changed on the profile."""
        c = make_api_client(fixture_profiles_root(SLUG))
        stale = self._work(c, self.PAPER)["version"]
        c.patch(f"/api/v1/profiles/{SLUG}/works/{self.PAPER}", json={"citation": "Someone else"})
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/works/{self.PAPER}",
            json={"doi": "10.1/mine", "base_version": stale},
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "conflict"
        current = self._work(c, self.PAPER)
        assert r.headers["X-RP-Paper-Version"] == current["version"]
        assert current["fields"].get("doi") is None
        r = c.delete(f"/api/v1/profiles/{SLUG}/works/{self.PAPER}", params={"base_version": stale})
        assert r.status_code == 409
        assert self._work(c, self.PAPER) is not None

    def test_add_then_delete_one_record(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        r = c.post(
            f"/api/v1/profiles/{SLUG}/works",
            json={"paper_id": "doe2026new", "name": "A new work", "type": "authored"},
        )
        assert r.status_code == 201, r.text
        added = self._work(c, "doe2026new")
        assert added["fields"]["name"] == "A new work"
        assert r.json()["version"] == added["version"]
        r = c.delete(
            f"/api/v1/profiles/{SLUG}/works/doe2026new",
            params={"base_version": added["version"]},
        )
        assert r.status_code == 200
        assert self._work(c, "doe2026new") is None

    def test_add_never_overwrites(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        before = self._work(c, self.PAPER)
        r = c.post(f"/api/v1/profiles/{SLUG}/works", json={"paper_id": self.PAPER, "name": "X"})
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "conflict"
        assert self._work(c, self.PAPER) == before

    def test_a_malformed_add_body_is_a_400_naming_the_field(
        self, make_api_client, fixture_profiles_root
    ):
        c = make_api_client(fixture_profiles_root(SLUG))
        r = c.post(f"/api/v1/profiles/{SLUG}/works", json={"paper_id": "doe2026new", "name": 123})
        assert r.status_code == 400, r.text
        assert "name" in r.json()["detail"]
        r = c.post(f"/api/v1/profiles/{SLUG}/works", json={"name": "No id"})
        assert r.status_code == 400

    def test_a_scope_gated_agent_is_refused(self, make_api_client, fixture_profiles_root):
        """``check_write_scope`` reaches the works routes with the fields named.

        A host that hands out a narrow agent key needs the paper and the fields
        in the detail, or its verifier can only say yes or no to "edits works
        at all".
        """
        from fastapi import HTTPException

        c = make_api_client(fixture_profiles_root(SLUG))
        seen: list[tuple[str, dict]] = []

        def _verifier(request, action, detail):  # noqa: ARG001
            seen.append((action, detail))
            raise HTTPException(status_code=403, detail="scope profile:works required")

        c.app.state.write_scope_verifier = _verifier
        r = c.patch(f"/api/v1/profiles/{SLUG}/works/{self.PAPER}", json={"doi": "10.1/x"})
        assert r.status_code == 403, r.text
        assert seen == [("works", {"paper_id": self.PAPER, "fields": ["doi"]})]
        assert self._work(c, self.PAPER)["fields"].get("doi") is None


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


class TestAiWrittenFieldsAreEditable:
    """Nothing the build AI writes is locked from its owner.

    An owner fixing an outdated ``current_rank`` must not need a rebuild. Only
    identity, code-computed facts, the manifest, bookkeeping, and visibility
    are locked.
    """

    CAREER_STAGE = {
        "as_of": "2026-09-30",
        "current_rank": "professor",
        "tenure_status": "tenured",
        "independence": "independent",
        "evidence": "Stated by the owner.",
        "confidence": "high",
    }

    def test_every_field_is_editable_or_locked_not_both(self):
        fields = set(ProfileDocument.model_fields)
        assert EDITABLE_METADATA_FIELDS | LOCKED_METADATA_FIELDS == fields
        assert not EDITABLE_METADATA_FIELDS & LOCKED_METADATA_FIELDS

    def test_career_stage_round_trips(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.patch_metadata({"career_stage": self.CAREER_STAGE})
        stage = ResearcherProfile.from_files(jane_doe_dir).metadata.career_stage
        assert stage is not None
        assert stage.current_rank == "professor"
        assert stage.as_of == "2026-09-30"

    def test_partial_career_stage_keeps_the_other_facts(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.patch_metadata({"career_stage": self.CAREER_STAGE})
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.patch_metadata({"career_stage": {"current_rank": "associate_professor"}})
        stage = ResearcherProfile.from_files(jane_doe_dir).metadata.career_stage
        assert stage.current_rank == "associate_professor"
        assert stage.as_of == "2026-09-30"
        assert stage.tenure_status == self.CAREER_STAGE["tenure_status"]

    def test_null_key_in_career_stage_clears_only_that_key(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.patch_metadata({"career_stage": self.CAREER_STAGE})
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.patch_metadata({"career_stage": {"current_rank": None}})
        stage = ResearcherProfile.from_files(jane_doe_dir).metadata.career_stage
        assert stage.current_rank is None
        assert stage.as_of == "2026-09-30"

    def test_invalid_career_stage_is_rejected_and_writes_nothing(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        before = (jane_doe_dir / "profile.jsonld").read_bytes()
        with pytest.raises(EditError, match="career_stage"):
            prof.edit.patch_metadata(
                {"career_stage": {**self.CAREER_STAGE, "current_rank": "grand_poobah"}}
            )
        assert (jane_doe_dir / "profile.jsonld").read_bytes() == before

    def test_list_fields_persist(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.edit.patch_metadata(
            {
                "recurring_positions": ["Chromatin shapes regulation"],
                "critiques": ["Peak calling hides uncertainty"],
                "collaborators": ["Ada Lovelace", {"name": "Alan Turing"}],
                "research_outputs": [
                    {"type": "software", "name": "gtars", "url": "https://github.com/databio/gtars"}
                ],
            }
        )
        meta = ResearcherProfile.from_files(jane_doe_dir).metadata
        assert meta.recurring_positions == ["Chromatin shapes regulation"]
        assert meta.critiques == ["Peak calling hides uncertainty"]
        assert meta.collaborators == ["Ada Lovelace", {"name": "Alan Turing"}]
        assert [o.name for o in meta.research_outputs] == ["gtars"]

    def test_invalid_research_output_is_rejected(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        with pytest.raises(EditError, match=r"research_outputs\[0\]"):
            prof.edit.patch_metadata({"research_outputs": [{"type": "software"}]})

    @pytest.mark.parametrize(
        "patch",
        [{"rid": "0000-0002-1825-0097"}, {"paper_stats": {"total_papers": 1}}],
    )
    def test_locked_fields_stay_locked(self, jane_doe_dir, patch):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        with pytest.raises(EditError, match="not owner-editable"):
            prof.edit.patch_metadata(patch)


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
        assert r.json()["detail"]["error"] == "conflict"
        assert r.json()["detail"]["current"] == r.headers["X-RP-Content-Hash"]
        assert r.headers["X-RP-Content-Hash"].startswith("sha256:")
        assert c.get(f"/api/v1/profiles/{SLUG}", params={"view": "full"}).json()["fields"][
            "field"
        ] == ("Somebody Else's Edit")

    def test_omitting_base_hash_stays_last_writer_wins(
        self, make_api_client, fixture_profiles_root
    ):
        c = make_api_client(fixture_profiles_root(SLUG))
        c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "First"})
        r = c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Second"})
        assert r.status_code == 200, r.text
        assert (
            c.get(f"/api/v1/profiles/{SLUG}", params={"view": "full"}).json()["fields"]["field"]
            == "Second"
        )

    def test_soul_and_metadata_share_one_clock(self, make_api_client, fixture_profiles_root):
        """The digest spans document + SOUL, so a soul write stales a pending metadata patch."""
        c = make_api_client(fixture_profiles_root(SLUG))
        stale = c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"]
        r = c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"soul": "a new voice"})
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
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/metadata",
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
            json={"profile_visibility": "limited", "base_hash": stale},
        )
        assert r.status_code == 409, r.text
        assert r.headers["X-RP-Content-Hash"].startswith("sha256:")
        assert c.get(f"/api/v1/profiles/{SLUG}/visibility").json()["profile_visibility"] != (
            "limited"
        )

    def test_matching_visibility_base_hash_is_accepted(
        self, make_api_client, fixture_profiles_root
    ):
        c = make_api_client(fixture_profiles_root(SLUG))
        before = c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"]
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/visibility",
            json={"profile_visibility": "limited", "base_hash": before},
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
        md = c.get(f"/api/v1/profiles/{SLUG}", params={"view": "full"}).json()["fields"]
        assert md["job_title"] == "Associate Professor"
        assert md["interests"] == ["single-cell"]
        assert md["not_interests"] == ["grant admin"]
        assert md["same_as"] == ["https://example.org/jane"]
        assert md["training"][0]["degree"] == "PhD"
        assert md["career"][0]["role"] == "PI"
        # The document's own tier is readable, so an editor can say what it is
        # without guessing. It is not settable here.
        assert md["visibility"] in {"public", "limited", "private"}

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
