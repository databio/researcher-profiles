"""Owner-scoped interactive edits: the ``profile.edit`` helpers (on disk) and the edit endpoints.

The endpoints run through the service's edit gate: the operator-token fallback on bare
rp-sdk, and owner/non-owner/no-session behavior when a host installs ``hooks.edit_gate``.
"""

import json

import pytest

from researcher_profiles import Forbidden, ResearcherProfile, Unauthenticated
from researcher_profiles.api.caller import Caller
from researcher_profiles.profile.edit import (
    EDITABLE_METADATA_FIELDS,
    LOCKED_METADATA_FIELDS,
    EditError,
)
from researcher_profiles.schema import ProfileDocument

SLUG = "jane-doe"

CAREER_STAGE = {
    "as_of": "2026-09-30",
    "current_rank": "professor",
    "tenure_status": "tenured",
    "independence": "independent",
    "evidence": "Stated by the owner.",
    "confidence": "high",
}


def _dig(obj, dotted):
    """Walk ``obj`` along a dotted path: list indices for digits, else attribute (or dict key)."""
    for part in dotted.split("."):
        if part.isdigit():
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            obj = obj[part]
        else:
            obj = getattr(obj, part)
    return obj


#: (patch, {dotted path on the reloaded metadata: expected value})
ROUND_TRIPS = [
    pytest.param(
        {"name": "Jane Q. Doe", "field": "Genomics", "expertise": ["ATAC-seq"]},
        {"name": "Jane Q. Doe", "field": "Genomics", "expertise": ["ATAC-seq"]},
        id="name-field-expertise",
    ),
    pytest.param(
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
            "career": [{"role": "Assistant Professor", "institution": "UVA", "start_year": 2015}],
        },
        {
            "job_title": "Associate Professor",
            "training.0.kind": "degree",
            "training.1.kind": "postdoc",
            "training.0.institution": "Duke University",
            "career.0.role": "Assistant Professor",
            # An open-ended position stays open-ended rather than being defaulted.
            "career.0.end_year": None,
        },
        id="training-career-job-title",
    ),
    pytest.param(
        {"career_stage": CAREER_STAGE},
        {"career_stage.current_rank": "professor", "career_stage.source": "self_reported"},
        id="career-stage",
    ),
    pytest.param(
        {
            "recurring_positions": ["Chromatin shapes regulation"],
            "critiques": ["Peak calling hides uncertainty"],
            "collaborators": ["Ada Lovelace", {"name": "Alan Turing"}],
            "research_outputs": [
                {"type": "software", "name": "gtars", "url": "https://github.com/databio/gtars"}
            ],
        },
        {
            "recurring_positions": ["Chromatin shapes regulation"],
            "critiques": ["Peak calling hides uncertainty"],
            "collaborators": ["Ada Lovelace", {"name": "Alan Turing"}],
            "research_outputs.0.name": "gtars",
        },
        id="list-fields",
    ),
]

#: (patch, error match): each must raise and leave ``profile.jsonld`` byte-identical.
REFUSED_PATCHES = [
    pytest.param(
        {"rid": "0000-0002-1825-0097", "name": "X"},
        "not owner-editable",
        id="identity-with-editable",
    ),
    # name has min_length=1: an empty string fails schema re-validation.
    pytest.param({"name": ""}, "invalid", id="schema-revalidation"),
    pytest.param({"career": [{"institution": "UVA"}]}, r"career\[0\]", id="malformed-career"),
    pytest.param({"training": {"kind": "degree"}}, "must be a list", id="training-not-list"),
    # ``expertise`` (labels) is editable; the pipeline-written narrative is not.
    pytest.param({"expertise_md": "# mine now"}, "not owner-editable", id="narrative-locked"),
    pytest.param(
        {"career_stage": {**CAREER_STAGE, "current_rank": "grand_poobah"}},
        "career_stage",
        id="bad-career-stage",
    ),
    pytest.param(
        {"research_outputs": [{"type": "software"}]},
        r"research_outputs\[0\]",
        id="bad-research-output",
    ),
    pytest.param({"rid": "0000-0002-1825-0097"}, "not owner-editable", id="rid-locked"),
    pytest.param({"paper_stats": {"total_papers": 1}}, "not owner-editable", id="computed-locked"),
]


@pytest.mark.parametrize("patch, expected", ROUND_TRIPS)
def test_a_patch_round_trips(jane_doe_dir, patch, expected):
    ResearcherProfile.from_files(jane_doe_dir).edit.patch_metadata(patch)
    # Written to disk: a freshly loaded profile sees the change, and the write is stamped.
    meta = ResearcherProfile.from_files(jane_doe_dir).metadata
    for dotted, want in expected.items():
        assert _dig(meta, dotted) == want, dotted
    assert "dateModified" in json.loads((jane_doe_dir / "profile.jsonld").read_text())


@pytest.mark.parametrize("patch, match", REFUSED_PATCHES)
def test_a_refused_patch_raises_and_writes_nothing(jane_doe_dir, patch, match):
    before = (jane_doe_dir / "profile.jsonld").read_bytes()
    with pytest.raises(EditError, match=match):
        ResearcherProfile.from_files(jane_doe_dir).edit.patch_metadata(patch)
    assert (jane_doe_dir / "profile.jsonld").read_bytes() == before


@pytest.mark.parametrize(
    "second_patch, rank",
    [
        ({"current_rank": "associate_professor"}, "associate_professor"),
        ({"current_rank": None}, None),
    ],
    ids=["new-rank", "null-clears-only-that-key"],
)
def test_a_partial_career_stage_patch_touches_only_its_keys(
    jane_doe_dir, pinned_today, second_patch, rank
):
    ResearcherProfile.from_files(jane_doe_dir).edit.patch_metadata({"career_stage": CAREER_STAGE})
    ResearcherProfile.from_files(jane_doe_dir).edit.patch_metadata({"career_stage": second_patch})
    stage = ResearcherProfile.from_files(jane_doe_dir).metadata.career_stage
    assert stage.current_rank == rank
    # An owner edit is the owner's answer: the server stamps when and who.
    assert stage.as_of == "2026-10-20"
    assert stage.source == "self_reported"
    assert stage.tenure_status == CAREER_STAGE["tenure_status"]


@pytest.fixture
def pinned_today(monkeypatch):
    monkeypatch.setattr(
        "researcher_profiles.profile.edit.utc_now_iso", lambda: "2026-10-20T12:00:00+00:00"
    )


INFERRED_FACULTY = {
    "as_of": "2025-01-01",
    "current_rank": "professor",
    "tenure_status": "tenured",
    "independence": "independent",
    "terminal_degree_year": 2010,
    "terminal_degree_type": "research",
    "first_independent_appointment_year": 2015,
    "evidence": "ORCID employment.",
    "confidence": "medium",
    "career_level": "faculty",
}


def _write_stage(profile_dir, stage):
    path = profile_dir / "profile.jsonld"
    doc = json.loads(path.read_text())
    doc["career_stage"] = stage
    path.write_text(json.dumps(doc))


class TestCareerLevelEdit:
    def test_level_on_a_doc_without_a_stage(self, jane_doe_dir, pinned_today):
        _write_stage(jane_doe_dir, None)
        ResearcherProfile.from_files(jane_doe_dir).edit.patch_metadata(
            {"career_stage": {"career_level": "graduate_student"}}
        )
        stage = ResearcherProfile.from_files(jane_doe_dir).metadata.career_stage
        assert stage.career_level == "graduate_student"
        assert stage.source == "self_reported"
        assert stage.current_rank == "student"
        assert stage.independence == "trainee"
        assert stage.as_of == "2026-10-20"
        assert stage.evidence == "Reported by the profile owner."

    def test_level_over_an_inferred_faculty_stage(self, jane_doe_dir, pinned_today):
        _write_stage(jane_doe_dir, INFERRED_FACULTY)
        ResearcherProfile.from_files(jane_doe_dir).edit.patch_metadata(
            {"career_stage": {"career_level": "graduate_student"}}
        )
        stage = ResearcherProfile.from_files(jane_doe_dir).metadata.career_stage
        assert (stage.career_level, stage.current_rank, stage.independence) == (
            "graduate_student",
            "student",
            "trainee",
        )
        assert stage.terminal_degree_year == 2010
        assert stage.first_independent_appointment_year == 2015

    def test_withdrawn_level_goes_back_to_inferred(self, jane_doe_dir, pinned_today):
        _write_stage(jane_doe_dir, {**INFERRED_FACULTY, "source": "self_reported"})
        ResearcherProfile.from_files(jane_doe_dir).edit.patch_metadata(
            {"career_stage": {"career_level": None}}
        )
        stage = ResearcherProfile.from_files(jane_doe_dir).metadata.career_stage
        assert stage.career_level is None
        assert stage.source == "inferred"

    def test_caller_source_is_ignored(self, jane_doe_dir, pinned_today):
        _write_stage(jane_doe_dir, None)
        ResearcherProfile.from_files(jane_doe_dir).edit.patch_metadata(
            {
                "career_stage": {
                    "career_level": "postdoc",
                    "source": "inferred",
                    "confidence": "low",
                    "evidence": "made up",
                }
            }
        )
        stage = ResearcherProfile.from_files(jane_doe_dir).metadata.career_stage
        assert stage.source == "self_reported"
        assert stage.confidence == "high"
        assert stage.evidence == "Reported by the profile owner."

    def test_level_with_a_rank_correction(self, jane_doe_dir, pinned_today):
        _write_stage(jane_doe_dir, None)
        ResearcherProfile.from_files(jane_doe_dir).edit.patch_metadata(
            {"career_stage": {"career_level": "faculty", "current_rank": "associate_professor"}}
        )
        stage = ResearcherProfile.from_files(jane_doe_dir).metadata.career_stage
        assert stage.current_rank == "associate_professor"
        assert stage.independence == "independent"

    def test_bad_level_is_an_edit_error(self, jane_doe_dir, pinned_today):
        with pytest.raises(EditError, match="career_stage"):
            ResearcherProfile.from_files(jane_doe_dir).edit.patch_metadata(
                {"career_stage": {"career_level": "wizard"}}
            )


def test_metadata_patch_no_op_preserves_stamp(jane_doe_dir):
    prof = ResearcherProfile.from_files(jane_doe_dir)
    prof.edit.patch_metadata({"name": prof.name})
    first_stamp = json.loads((jane_doe_dir / "profile.jsonld").read_text()).get("dateModified")
    prof2 = ResearcherProfile.from_files(jane_doe_dir)
    prof2.edit.patch_metadata({"name": prof2.name})
    doc2 = json.loads((jane_doe_dir / "profile.jsonld").read_text())
    assert doc2.get("dateModified") == first_stamp


def test_set_soul_writes_markdown(jane_doe_dir):
    ResearcherProfile.from_files(jane_doe_dir).edit.set_soul("# New soul\nI think in graphs.")
    assert (jane_doe_dir / "personality" / "SOUL.md").read_text().startswith("# New soul")
    assert ResearcherProfile.from_files(jane_doe_dir).soul.startswith("# New soul")


def test_set_profile_visibility(jane_doe_dir):
    ResearcherProfile.from_files(jane_doe_dir).edit.set_visibility(profile_visibility="limited")
    assert ResearcherProfile.from_files(jane_doe_dir).metadata.visibility == "limited"
    assert "dateModified" in json.loads((jane_doe_dir / "profile.jsonld").read_text())


def test_set_visibility_rejects_bad_tier(jane_doe_dir):
    prof = ResearcherProfile.from_files(jane_doe_dir)
    with pytest.raises(EditError, match="invalid visibility"):
        prof.edit.set_visibility(profile_visibility="secret")


@pytest.mark.parametrize(
    "kwargs, collection, role, tier",
    [
        (
            {"artifacts": [{"role": "soul", "visibility": "limited"}]},
            "subject_of",
            "soul",
            "limited",
        ),
        # paper_fulltext defaults to private, but that is a default, not a floor.
        (
            {"artifacts": [{"role": "paper_fulltext", "visibility": "public"}]},
            "has_part",
            "paper_fulltext",
            "public",
        ),
        # SOUL is a manifest artifact: a section tier must re-tier every `soul` part, or it is a dead knob.
        (
            {"sections": [{"section": "soul", "visibility": "limited"}]},
            "subject_of",
            "soul",
            "limited",
        ),
    ],
    ids=["artifact-soul", "artifact-fulltext", "section-soul"],
)
def test_set_visibility_retiers_parts(jane_doe_dir, kwargs, collection, role, tier):
    _doc, changed = ResearcherProfile.from_files(jane_doe_dir).edit.set_visibility(**kwargs)
    assert changed >= 1
    meta = ResearcherProfile.from_files(jane_doe_dir).metadata
    parts = [p for p in getattr(meta, collection) if p.role == role]
    assert parts and all(p.visibility == tier for p in parts)
    if "sections" in kwargs:
        assert {x.section: x.visibility for x in meta.section_visibility}["soul"] == "limited"


class TestWorkEdits:
    """``patch_work`` / ``add_work`` / ``remove_work``: field-level writes to one record; a refused write changes nothing."""

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

    def test_a_scope_gated_agent_is_refused(self, make_api_client, fixture_profiles_root):
        """``hooks.write_scope`` reaches the works routes with the paper and fields named."""
        c = make_api_client(fixture_profiles_root(SLUG))
        seen: list[tuple[str, dict]] = []

        def _write_scope(caller, prof, action, detail):  # noqa: ARG001
            seen.append((action, detail))
            raise Forbidden("scope profile:works required")

        c.app.state.hooks.write_scope = _write_scope
        r = c.patch(f"/api/v1/profiles/{SLUG}/works/{self.PAPER}", json={"doi": "10.1/x"})
        assert r.status_code == 403, r.text
        assert seen == [("works", {"paper_id": self.PAPER, "fields": ["doi"]})]
        assert self._work(c, self.PAPER)["fields"].get("doi") is None


class TestEditEndpointsOwnerScoped:
    """A management host installs an edit gate -> owner-scoped auth."""

    @pytest.fixture
    def owned_client(self, make_api_client, fixture_profiles_root):
        """The stub resolver reads ``X-Test-User`` onto the scopes; the gate says only 'owner' owns SLUG."""
        c = make_api_client(fixture_profiles_root(SLUG))

        def _caller(request):
            user = request.headers.get("X-Test-User")
            return Caller(scopes=frozenset({f"user:{user}"}) if user else frozenset())

        def _gate(caller, prof, *, read_ok=False, ref=None):  # noqa: ARG001
            if not caller.scopes:
                raise Unauthenticated("login required")
            if "user:owner" not in caller.scopes:
                raise Forbidden("not the owner")

        c.app.state.hooks.caller_resolver = _caller
        c.app.state.hooks.edit_gate = _gate
        return c

    @pytest.mark.parametrize(
        "headers, status",
        [({"X-Test-User": "owner"}, 200), ({"X-Test-User": "someone-else"}, 403), ({}, 401)],
        ids=["owner", "non-owner", "no-session"],
    )
    def test_the_edit_gate(self, owned_client, headers, status):
        r = owned_client.patch(
            f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Genomics"}, headers=headers
        )
        assert r.status_code == status, r.text


class TestHeldBackProfileIsNotDisclosedByEdits:
    """An anonymous edit is a 401 before any lookup; a held-back and a missing profile must answer alike (else 401-vs-404 is an existence oracle)."""

    PAPER = "doe2016example"
    MISSING = "nobody-here"
    ROUTES = [
        ("patch", "/metadata", {"field": "Genomics"}),
        ("get", "/visibility", None),
        ("patch", "/visibility", {"profile_visibility": "public"}),
        ("post", "/works", {"paper_id": "doe2026new", "name": "New"}),
        ("patch", f"/works/{PAPER}", {"doi": "10.1/x"}),
        ("delete", f"/works/{PAPER}", None),
    ]

    @pytest.fixture
    def client(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG), token="op-token")
        store = c.app.state.service.store
        store.get(SLUG).edit.set_visibility(profile_visibility="private")
        store.evict(SLUG)
        return c

    @staticmethod
    def _call(c, method, path, body, **kw):
        if body is None:
            return getattr(c, method)(path, **kw)
        return getattr(c, method)(path, json=body, **kw)

    @pytest.mark.parametrize("method,suffix,body", ROUTES)
    def test_anonymous_private_and_missing_answer_alike(self, client, method, suffix, body):
        hidden = self._call(client, method, f"/api/v1/profiles/{SLUG}{suffix}", body)
        missing = self._call(client, method, f"/api/v1/profiles/{self.MISSING}{suffix}", body)
        assert hidden.status_code == missing.status_code == 401, (hidden.text, missing.text)
        assert hidden.json() == missing.json()

    def test_operator_still_tells_them_apart(self, client):
        auth = {"Authorization": "Bearer op-token"}
        hidden = client.get(f"/api/v1/profiles/{SLUG}/visibility", headers=auth)
        missing = client.get(f"/api/v1/profiles/{self.MISSING}/visibility", headers=auth)
        assert hidden.status_code == 200, hidden.text
        assert missing.status_code == 404


class TestInsufficientAccessHint:
    def test_hint_is_a_sentence_when_the_error_has_none(
        self, make_api_client, fixture_profiles_root
    ):
        from researcher_profiles.errors import InsufficientScope

        c = make_api_client(fixture_profiles_root(SLUG))

        def _write_scope(caller, prof, action, detail):  # noqa: ARG001
            raise InsufficientScope(frozenset({"profile:write"}), missing=["summary"])

        c.app.state.hooks.write_scope = _write_scope
        r = c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Genomics"})
        assert r.status_code == 403, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "insufficient_access"
        assert detail["hint"] == "This needs Write on summary."


class TestAiWrittenFieldsAreEditable:
    """Nothing the build AI writes is locked from its owner (an outdated ``current_rank`` needs no rebuild)."""

    def test_every_field_is_editable_or_locked_not_both(self):
        fields = set(ProfileDocument.model_fields)
        assert EDITABLE_METADATA_FIELDS | LOCKED_METADATA_FIELDS == fields
        assert not EDITABLE_METADATA_FIELDS & LOCKED_METADATA_FIELDS


def _hash(c):
    return c.get(f"/api/v1/profiles/{SLUG}").json()["content_hash"]


class TestConcurrencyToken:
    """``base_hash`` -> 409. Opt-in: no token means last-writer-wins."""

    @pytest.mark.parametrize(
        "intervene, stale_path, stale_body",
        [
            pytest.param(
                {"field": "Somebody Else's Edit"}, "metadata", {"field": "Genomics"}, id="metadata"
            ),
            # The digest spans document + SOUL, so a soul write stales a pending metadata patch.
            pytest.param(
                {"soul": "a new voice"},
                "metadata",
                {"field": "Genomics"},
                id="soul-shares-the-clock",
            ),
            pytest.param(
                {"field": "Somebody Else's Edit"}, "metadata", {"soul": "mine"}, id="stale-soul"
            ),
            pytest.param(
                {"field": "Somebody Else's Edit"},
                "visibility",
                {"profile_visibility": "limited"},
                id="stale-visibility",
            ),
        ],
    )
    def test_a_stale_base_hash_is_a_409_and_changes_nothing(
        self, make_api_client, fixture_profiles_root, intervene, stale_path, stale_body
    ):
        c = make_api_client(fixture_profiles_root(SLUG))
        stale = _hash(c)
        assert c.patch(f"/api/v1/profiles/{SLUG}/metadata", json=intervene).status_code == 200
        current = _hash(c)
        assert current != stale
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/{stale_path}", json={**stale_body, "base_hash": stale}
        )
        assert r.status_code == 409, r.text
        assert r.headers["X-RP-Content-Hash"].startswith("sha256:")
        assert _hash(c) == current

    def test_the_conflict_names_the_current_hash(self, make_api_client, fixture_profiles_root):
        c = make_api_client(fixture_profiles_root(SLUG))
        stale = _hash(c)
        c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Somebody Else's Edit"})
        r = c.patch(
            f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Genomics", "base_hash": stale}
        )
        assert r.json()["detail"]["error"] == "conflict"
        assert r.json()["detail"]["current"] == r.headers["X-RP-Content-Hash"]
        full = c.get(f"/api/v1/profiles/{SLUG}", params={"view": "full"}).json()
        assert full["fields"]["field"] == "Somebody Else's Edit"

    @pytest.mark.parametrize(
        "path, body, updated",
        [
            ("metadata", {"field": "Genomics"}, ["field"]),
            ("visibility", {"profile_visibility": "limited"}, ["visibility"]),
        ],
    )
    def test_a_matching_base_hash_is_accepted(
        self, make_api_client, fixture_profiles_root, path, body, updated
    ):
        c = make_api_client(fixture_profiles_root(SLUG))
        before = _hash(c)
        assert before.startswith("sha256:")
        r = c.patch(f"/api/v1/profiles/{SLUG}/{path}", json={**body, "base_hash": before})
        assert r.status_code == 200, r.text
        after = r.json()["content_hash"]
        assert after.startswith("sha256:") and after != before
        # base_hash is bookkeeping, not a field: it must not be reported as one.
        assert r.json()["updated"] == updated

    def test_omitting_base_hash_stays_last_writer_wins(
        self, make_api_client, fixture_profiles_root
    ):
        c = make_api_client(fixture_profiles_root(SLUG))
        c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "First"})
        r = c.patch(f"/api/v1/profiles/{SLUG}/metadata", json={"field": "Second"})
        assert r.status_code == 200, r.text
        full = c.get(f"/api/v1/profiles/{SLUG}", params={"view": "full"}).json()
        assert full["fields"]["field"] == "Second"


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
        # The document's own tier is readable but not settable here.
        assert md["visibility"] in {"public", "limited", "private"}


@pytest.mark.parametrize(
    "method, path, body, status, detail_fragment",
    [
        ("patch", "works/doe2016example", {"cited_by_count": 9000}, 400, "not owner-editable"),
        ("patch", "works/nobody2099", {"doi": "10.1/x"}, 404, None),
        ("post", "works", {"paper_id": "doe2026new", "name": 123}, 400, "name"),
        ("post", "works", {"name": "No id"}, 400, None),
        # The one place that governs who may read a profile stays one place.
        ("patch", "metadata", {"visibility": "public"}, 400, "not owner-editable"),
        (
            "patch",
            "metadata",
            {"training": [{"kind": "sabbatical", "degree": "x", "institution": "y"}]},
            400,
            "training[0]",
        ),
    ],
    ids=[
        "disallowed-field",
        "unknown-paper",
        "malformed-add",
        "add-without-id",
        "visibility-via-metadata",
        "bad-training",
    ],
)
def test_a_refused_write_is_a_4xx_and_changes_nothing(
    make_api_client, fixture_profiles_root, method, path, body, status, detail_fragment
):
    c = make_api_client(fixture_profiles_root(SLUG))
    before = _hash(c)
    r = getattr(c, method)(f"/api/v1/profiles/{SLUG}/{path}", json=body)
    assert r.status_code == status, r.text
    if detail_fragment:
        assert detail_fragment in r.json()["detail"]
    assert _hash(c) == before
