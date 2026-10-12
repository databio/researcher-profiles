"""What must not leak, and what must not be claimed.

Three invariants that all fail the same way: a profile saying more than it is
entitled to say. Tiers and ``.publishignore`` decide what reaches a published
directory; the persona guard refuses to speak as a researcher whose persona was
never written; and the refusal contract keeps an answer from outrunning its
evidence.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from researcher_profiles import (
    PersonaUnavailableError,
    ProfileError,
    ResearcherProfile,
    privacy,
)
from researcher_profiles.schema import (
    ROLE_DEFAULT_VISIBILITY,
    ArtifactRef,
    ProfileDocument,
    most_restrictive,
    role_default_visibility,
)

from .factories import stub_llm


def _patch_profile_json(profile_dir: Path, **fields) -> Path:
    """Read/patch/write ``profile.jsonld`` raw, without re-validating it."""
    path = profile_dir / "profile.jsonld"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _profile(**parts) -> ProfileDocument:
    base = {
        "name": "X",
        "rid": "local:x-a1b2c3",
        "provenance": "self_published",
    }
    base.update(parts)
    return ProfileDocument.model_validate(base)


@pytest.mark.parametrize(
    "tiers, expected",
    [
        (("public", "limited"), "limited"),
        (("public", "private"), "private"),
        (("limited", "private"), "private"),
        (("public", "limited", "private"), "private"),
        ((), "public"),
        ((None, "public"), "public"),
    ],
)
def test_tier_order_is_public_limited_private(tiers, expected):
    assert most_restrictive(*tiers) == expected


@pytest.mark.parametrize(("old", "new"), [("internal", "limited"), ("restricted", "private")])
def test_retired_tier_names_are_rejected_with_their_replacement(old, new):
    msg = f"visibility '{old}' was renamed to '{new}'"
    with pytest.raises(ValueError, match=msg):
        _profile(visibility=old)
    with pytest.raises(ValueError, match=msg):
        ArtifactRef(role="cv", contentUrl="sources/cv.md", visibility=old)
    with pytest.raises(ValueError, match=msg):
        _profile(**{"rp:sectionVisibility": [{"section": "methods", "visibility": old}]})


def test_context_expands_visibility_to_tier_iris():
    """`visibility` values expand to rp:Public/Limited/Private, each linked by
    skos:closeMatch to the EU access-right vocabulary."""
    from researcher_profiles.schema.jsonld import context_document_text

    doc = json.loads(context_document_text())
    term = doc["@context"]["visibility"]
    assert term["@type"] == "@vocab"
    assert term["@context"] == {
        "public": "rp:Public",
        "limited": "rp:Limited",
        "private": "rp:Private",
    }
    eu = "http://publications.europa.eu/resource/authority/access-right/"
    matches = {node["@id"]: {m["@id"] for m in node["skos:closeMatch"]} for node in doc["@graph"]}
    assert eu + "PUBLIC" in matches["rp:Public"]
    assert eu + "RESTRICTED" in matches["rp:Limited"]
    assert eu + "NON_PUBLIC" in matches["rp:Private"]


@pytest.mark.parametrize(
    "content_url, role, declared, expected",
    [
        ("sources/cv.md", "cv", None, "private"),
        ("w.md", "web", None, "private"),
        ("sources/papers.jsonld", "works", None, "public"),
        ("sources/papers/p1.md", "paper_fulltext", None, "private"),
        ("sources/papers/p1.md", "paper_fulltext", "public", "public"),
        ("w.md", "web", "limited", "limited"),
    ],
    ids=[
        "cv-role-default",
        "web-role-default",
        "works-role-default",
        "fulltext-role-default",
        "fulltext-tier-is-choosable",
        "explicit-tier-overrides-role-default",
    ],
)
def test_part_ref_visibility(content_url, role, declared, expected):
    """Role defaults apply at model level; an explicit tier overrides them for
    every role, `paper_fulltext` included (its private default is a default,
    not a floor).
    """
    data = {"contentUrl": content_url, "role": role}
    if declared is not None:
        data["visibility"] = declared
    assert ArtifactRef.model_validate(data).visibility == expected


@pytest.fixture
def built_profile_dir(tmp_path):
    """A manifest-built profile (cv, web, both indexes, one paper full text)
    alone under a ``profiles/`` root."""
    from .factories import ADA, build_profile_dir

    pdir = build_profile_dir(
        tmp_path / "profiles" / "drift-check",
        rid=ADA,
        cv=True,
        web=True,
        index="both",
        manifest=True,
    )
    (pdir / "sources" / "papers").mkdir(parents=True, exist_ok=True)
    (pdir / "sources" / "papers" / "p1.md").write_text("full text\n", encoding="utf-8")
    ResearcherProfile.from_files(pdir).build_manifest(write=True)
    return pdir


def test_static_deploy_and_anonymous_http_publish_the_same_set(built_profile_dir, make_api_client):
    """What ``rp publish --who public`` ships is exactly what anonymous HTTP serves (the two mechanisms must not drift)."""
    from researcher_profiles.publish import plan_profile_export

    pdir = built_profile_dir
    prof = ResearcherProfile.from_files(pdir)
    all_artifacts = set(privacy.effective_tiers(prof.metadata))
    on_disk = {url for url in all_artifacts if (pdir / url).is_file()}

    client = make_api_client(pdir.parent)
    detail = client.get("/api/v1/profiles/drift-check/files")
    assert detail.status_code == 200
    withheld = set(detail.json()["withheld"])

    static_publishes = set(plan_profile_export(pdir, "public").files)
    http_publishes = (all_artifacts - withheld) & on_disk
    assert static_publishes == http_publishes


def test_explain_tiers_is_the_only_implementation_of_effective_tiers():
    prof = _profile(
        visibility="limited",
        hasPart=[
            {"contentUrl": "sources/cv.md", "role": "cv"},
            {"contentUrl": "derived.json", "role": "custom", "derivedFrom": ["cv"]},
            {"contentUrl": "sources/papers.jsonld", "role": "works"},
        ],
    )
    explained = privacy.explain_tiers(prof)
    assert {k: v.effective for k, v in explained.items()} == privacy.effective_tiers(prof)


_CV = {"contentUrl": "sources/cv.md", "role": "cv"}
_WORKS = {"contentUrl": "sources/papers.jsonld", "role": "works"}
_FULLTEXT = {"contentUrl": "sources/papers/p1.md", "role": "paper_fulltext"}


@pytest.mark.parametrize(
    "profile_kwargs, url, effective, raised_by, declared",
    [
        # The profile default holds an otherwise-public artifact back, and names itself.
        (
            {"visibility": "limited", "hasPart": [_WORKS]},
            _WORKS["contentUrl"],
            "limited",
            ["the profile default (limited)"],
            None,
        ),
        # A public artifact has nothing to explain.
        ({"hasPart": [_WORKS]}, _WORKS["contentUrl"], "public", [], None),
        # The cause named is the specific source, not the rule.
        (
            {
                "hasPart": [
                    _CV,
                    {"contentUrl": "derived.json", "role": "custom", "derivedFrom": ["cv"]},
                ]
            },
            "derived.json",
            "private",
            "sources/cv.md",
            "public",
        ),
        # No role floor: fulltext defaults to private, and an explicit public declaration wins.
        ({"hasPart": [_FULLTEXT]}, _FULLTEXT["contentUrl"], "private", None, None),
        (
            {"hasPart": [{**_FULLTEXT, "visibility": "public"}]},
            _FULLTEXT["contentUrl"],
            "public",
            None,
            None,
        ),
    ],
    ids=[
        "profile-default",
        "public-nothing-to-explain",
        "derived-names-source",
        "fulltext-default",
        "fulltext-raised",
    ],
)
def test_explain_tiers_row(profile_kwargs, url, effective, raised_by, declared):
    prof = _profile(**profile_kwargs)
    note = privacy.explain_tiers(prof)[url]
    assert note.effective == effective == privacy.effective_tiers(prof)[url]
    if declared is not None:
        assert note.declared == declared
    if isinstance(raised_by, list):
        assert note.raised_by == raised_by
    elif raised_by is not None:
        assert any(raised_by in phrase for phrase in note.raised_by)


def test_derivation_restriction_is_transitive():
    """A public derivative of a public derivative of a private CV is not public: the rule walks the chain."""
    prof = _profile(
        hasPart=[
            {"contentUrl": "sources/cv.md", "role": "cv"},  # private
            {"contentUrl": "digest.md", "role": "digest", "derivedFrom": ["cv"]},
            {"contentUrl": "blurb.md", "role": "blurb", "derivedFrom": ["digest"]},
        ]
    )
    eff = privacy.effective_tiers(prof)
    assert eff["sources/cv.md"] == "private"
    assert eff["digest.md"] == "private"
    assert eff["blurb.md"] == "private"
    assert any("digest.md" in p for p in privacy.explain_tiers(prof)["blurb.md"].raised_by)


def test_a_derivation_cycle_raises_rather_than_guessing():
    prof = _profile(
        hasPart=[
            {"contentUrl": "a.md", "role": "a", "derivedFrom": ["b"]},
            {"contentUrl": "b.md", "role": "b", "derivedFrom": ["a"]},
        ]
    )
    with pytest.raises(privacy.DerivationCycleError, match="cycle"):
        privacy.explain_tiers(prof)
    # The validator reports it instead of raising, so `rp validate` names the problem.
    assert "cycle" in "".join(privacy.derivation_errors(prof))


def test_an_unresolvable_derivedFrom_is_a_validation_error_not_a_public_default():
    prof = _profile(
        hasPart=[{"contentUrl": "derived.json", "role": "custom", "derivedFrom": ["nowhere"]}]
    )
    errors = privacy.derivation_errors(prof)
    assert len(errors) == 1
    assert "nowhere" in errors[0]
    assert privacy.explain_tiers(prof)["derived.json"].unresolved == ["nowhere"]


@pytest.mark.parametrize(
    "viewer, artifact, allowed",
    [
        ("public", "public", True),
        ("public", "limited", False),
        ("public", "private", False),
        ("limited", "public", True),
        ("limited", "limited", True),
        ("limited", "private", False),
        ("private", "private", True),
    ],
)
def test_tier_allows(viewer, artifact, allowed):
    assert privacy.tier_allows(viewer, artifact) is allowed


def test_narrow_viewer_is_a_cap_not_the_artifact_rule():
    """The two rules move opposite ways; using the wrong one un-caps a preview."""
    assert privacy.narrow_viewer("private", "public") == "public"
    assert privacy.narrow_viewer("public", "private") == "public"
    assert privacy.narrow_viewer("private", "limited") == "limited"
    # ...whereas most_restrictive, the ARTIFACT rule, would have said otherwise.
    assert most_restrictive("private", "public") == "private"


@pytest.mark.parametrize(
    "viewer, floor, visible",
    [("public", None, True), ("public", "limited", False), ("limited", "limited", True)],
)
def test_profile_visible_folds_in_a_host_floor(viewer, floor, visible):
    prof = _profile(visibility="public")
    kwargs = {"floor": floor} if floor else {}
    assert privacy.profile_visible(prof, viewer, **kwargs) is visible


def test_no_built_manifest_role_declares_derivedFrom(built_profile_dir):
    """Authored syntheses (summaries, expertise.md, SOUL.md) do not inherit their sources' tier; spec section 3.

    The absence is load-bearing: a summary inheriting its full text's tier would
    collapse every profile's public surface. A role that REPRODUCES a private source gets ``derivedFrom``.
    """
    prof = ResearcherProfile.from_files(built_profile_dir)
    parts = list(prof.metadata.has_part) + list(prof.metadata.subject_of)
    assert parts, "sanity: the fixture has a manifest"
    assert all(not p.derived_from for p in parts)


@pytest.mark.parametrize(
    "role, tier", [("grant", "private"), ("grants", "public"), ("interview", "private")]
)
def test_role_default_visibility(role, tier):
    # Grant-derived embedding text is private though the grants collection is public; the interview digest is the owner's alone.
    if role in ROLE_DEFAULT_VISIBILITY:
        assert ROLE_DEFAULT_VISIBILITY[role] == tier
    assert role_default_visibility(role) == tier


def test_the_interview_digest_does_not_hold_back_the_public_documents():
    ref = ArtifactRef(content_url="sources/interview.md", role="interview")
    assert ref.visibility == "private"
    # There is no derivedFrom link, only the disclosure line inside the derived documents.
    prof = _profile(
        hasPart=[{"contentUrl": "sources/interview.md", "role": "interview"}],
        subjectOf=[
            {"contentUrl": "personality/expertise.md", "role": "expertise"},
            {"contentUrl": "personality/SOUL.md", "role": "soul"},
        ],
    )
    tiers = privacy.effective_tiers(prof)
    assert tiers["sources/interview.md"] == "private"
    assert tiers["personality/expertise.md"] == "public"
    assert tiers["personality/SOUL.md"] == "public"


def test_chunk_source_tiers_tolerates_missing_profile():
    # None profile: default treated as public, role defaults still filter.
    tiers = privacy.chunk_source_tiers(None, [("cv", "cv"), ("soul", "soul")])
    assert tiers[("cv", "cv")] == "private"
    assert tiers[("soul", "soul")] == "public"


class TestPersonaGuard:
    """Persona methods raise ``PersonaUnavailableError`` and never call the LLM when the persona was never synthesized."""

    @pytest.fixture
    def incomplete(self, fixture_profile):
        """A profile whose persona documents were never synthesized."""
        return ResearcherProfile.from_files(fixture_profile("incomplete-profile"))

    @pytest.fixture
    def lite_jane(self, jane_doe_dir):
        """The complete fixture with its level flipped to ``lite`` on disk."""
        _patch_profile_json(jane_doe_dir, level="lite")
        return ResearcherProfile.from_files(jane_doe_dir)

    def test_has_persona_true_for_complete(self, jane_doe_readonly):
        assert jane_doe_readonly.has_persona is True

    def test_persona_unavailable_is_profile_error(self):
        assert issubclass(PersonaUnavailableError, ProfileError)

    def test_lite_level_gates_persona_off_even_with_content(self, lite_jane):
        # SOUL/expertise are present on disk, but level=lite gates persona off.
        assert lite_jane.level == "lite"
        assert lite_jane.expertise.strip()
        assert lite_jane.soul.strip()

    @pytest.mark.parametrize("profile", ["incomplete", "lite_jane"])
    def test_ask_raises_persona_unavailable(self, request, profile):
        prof = request.getfixturevalue(profile)
        assert prof.has_persona is False
        fake = stub_llm(prof)
        with pytest.raises(PersonaUnavailableError):
            prof.persona.ask("q")
        fake.complete.assert_not_called()


class _Hit:
    def __init__(self, sid, score, text="t", source_type="paper_summary"):
        self.source_id = sid
        self.source_type = source_type
        self.text = text
        self.score = score
        self.meta = {}


class TestRefusalContract:
    """A below-threshold corpus refuses without calling the LLM."""

    @pytest.mark.parametrize(
        "method, payload, score, kwargs, expect_refused",
        [
            ("ask", "anything?", 0.1, {"strict_corpus": True}, True),
            ("ask", "anything?", 0.9, {"strict_corpus": True}, False),
            (
                "ask",
                "q?",
                0.5,
                {"strict_corpus": True, "refusal_threshold": 0.9},
                True,
            ),
            (
                "review",
                "some draft",
                0.0,
                {"strict_corpus": True, "focus": "methods"},
                True,
            ),
        ],
        ids=[
            "strict_corpus_refuses_when_low_score",
            "strict_corpus_proceeds_when_high_score",
            "refusal_threshold_kwarg",
            "review_refusal_on_low_score",
        ],
    )
    def test_refusal_gate(self, jane_doe, method, payload, score, kwargs, expect_refused):
        """The refusal payload is inert (no citations, model or tokens), so it cannot pass for a cheap answer."""
        fake = stub_llm(jane_doe, "ok")
        jane_doe.index.search = MagicMock(return_value=[_Hit("a", score)])  # type: ignore[method-assign]

        r = getattr(jane_doe.persona, method)(payload, **kwargs)
        assert r.refused is expect_refused
        if expect_refused:
            assert r.refusal_reason and "below threshold" in r.refusal_reason
            assert r.citations == []
            assert r.model == "<none>"
            assert r.usage["input_tokens"] == 0
            fake.complete.assert_not_called()
        else:
            assert r.text == "ok"
            fake.complete.assert_called_once()

    def test_paper_summary_chunks_become_citation_refs(self, jane_doe):
        """Regression: the store's real ``source_type="paper_summary"`` must yield citations."""
        stub_llm(jane_doe, "ans")
        jane_doe.index.search = MagicMock(  # type: ignore[method-assign]
            return_value=[
                _Hit("doe2016example", 0.8, source_type="paper_summary"),
                _Hit("doe2019methods", 0.6, source_type="paper_summary"),
            ]
        )
        r = jane_doe.persona.ask("q?")
        assert r.citations, "paper_summary chunks must yield citations"
        assert r.citations[0].paper_id == "doe2016example"
        relevances = {c.paper_id: c.relevance for c in r.citations}
        assert relevances["doe2016example"] == 0.8

    @pytest.mark.parametrize(
        "llm_text, expect_grounded",
        [
            ("no inline citations here", True),
            ("I built on [totally_unknown_paper_xyz] for this.", False),
        ],
        ids=["no-inline-citations", "unknown-paper-id"],
    )
    def test_grounded_flag_from_inline_citations(self, jane_doe, llm_text, expect_grounded):
        stub_llm(jane_doe, llm_text)
        jane_doe.index.search = MagicMock(return_value=[])  # type: ignore[method-assign]
        r = jane_doe.persona.ask("q?")
        assert r.grounded is expect_grounded

    def test_history_param_threads_into_messages(self, jane_doe):
        fake = stub_llm(jane_doe, "ok")
        jane_doe.index.search = MagicMock(return_value=[])  # type: ignore[method-assign]

        history = [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ]
        jane_doe.persona.ask("new q?", history=history)
        kwargs = fake.complete.call_args.kwargs
        msgs = kwargs["messages"]
        assert msgs[:2] == history
        assert "new q?" in msgs[-1]["content"]


def test_inline_section_projection_removes_private_values():
    from researcher_profiles.privacy import project_document
    from researcher_profiles.schema import ProfileDocument, SectionVisibility

    doc = ProfileDocument(
        name="Private Methods",
        rid="local:private-methods-a1b2c3",
        provenance="self_published",
        summary="public summary",
        methodological_commitments=["secret assay"],
        section_visibility=[SectionVisibility(section="methods", visibility="private")],
    )
    public = project_document(doc, "public")
    assert public.summary == "public summary"
    assert public.methodological_commitments == []
    assert project_document(doc, "private").methodological_commitments == ["secret assay"]


#: The three private values planted in the fixture below, one per section.
_SECRETS = ("secret assay", "pediatric ward overflow", "IND 12345")


@pytest.fixture
def leak_profile_root(tmp_path):
    """A profile whose methods/site/clinical sections are private; the summary repeats one secret, as a generated blurb might."""
    from researcher_profiles.schema import SectionVisibility, SiteCapabilities

    from .factories import build_profile_dir

    root = tmp_path / "profiles"
    pdir = root / "leaky"
    build_profile_dir(pdir, rid="local:leaky-a1b2c3", manifest=True)
    prof = ResearcherProfile.from_files(pdir)
    doc = prof.metadata
    doc.summary = f"Runs trials. {_SECRETS[0]}."
    doc.methodological_commitments = [_SECRETS[0]]
    doc.site_capabilities = SiteCapabilities(patient_populations=[_SECRETS[1]])
    doc.regulatory_experience = [_SECRETS[2]]
    doc.section_visibility = [
        SectionVisibility(section="summary", visibility="private"),
        SectionVisibility(section="methods", visibility="private"),
        SectionVisibility(section="site_capabilities", visibility="private"),
        SectionVisibility(section="regulatory_experience", visibility="private"),
    ]
    prof.save_profile(doc)
    return root


def _assert_clean(blob: str) -> None:
    for secret in _SECRETS:
        assert secret not in blob


class TestInlineSectionsCannotEscape:
    """One private value, every public surface: a tier that holds on one read and not the export is a coincidence."""

    def test_http_reads_at_every_tier(self, leak_profile_root, make_api_client):
        client = make_api_client(leak_profile_root, token="op")
        for path in (
            "/api/v1/profiles",
            "/api/v1/profiles/leaky",
            "/profiles/leaky/profile.jsonld",
        ):
            _assert_clean(client.get(path).text)
        # The projection caps the viewer; it deletes nothing, so the owner reads it back.
        owner = client.get("/api/v1/profiles/leaky", headers={"Authorization": "Bearer op"}).json()
        assert owner["fields"]["methodological_commitments"] == [_SECRETS[0]]

    def test_static_publisher_output(self, leak_profile_root):
        from researcher_profiles.publish import publish_collection, render_profile

        pdir = leak_profile_root / "leaky"
        render_profile(pdir)
        _assert_clean((pdir / "index.html").read_text(encoding="utf-8"))
        out = leak_profile_root.parent / "site"
        publish_collection(leak_profile_root, out)
        for f in out.rglob("*"):
            if f.is_file() and f.suffix in (".json", ".jsonld", ".html", ".md"):
                _assert_clean(f.read_text(encoding="utf-8"))

    def test_viewer_archive(self, leak_profile_root):
        import io
        import tarfile

        from researcher_profiles.api.upload import build_viewer_archive

        blob = build_viewer_archive(leak_profile_root / "leaky", viewer="public")
        with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
            for member in tf.getmembers():
                if member.isfile():
                    _assert_clean(tf.extractfile(member).read().decode("utf-8"))

    def test_export_bundle(self, leak_profile_root):
        from researcher_profiles.profile.export import build_export_bundle

        prof = ResearcherProfile.from_files(leak_profile_root / "leaky")
        bundle = build_export_bundle(prof)
        _assert_clean(bundle.text)
        _assert_clean(json.dumps(bundle.model_dump(mode="json")))


def test_research_interest_keeps_neutral_and_unknown_out_of_both_lists():
    """0 (declared neutral) and no weight (unknown) are neither interest nor exclusion."""
    from researcher_profiles.schema import InterestConcept, ProfileDocument, ResearchInterest

    def entry(label, weight):
        return ResearchInterest(
            concept=InterestConcept(label=label, unmapped=True),
            weight=weight,
            method="declared",
            generator="user",
            assertedAt="2026-09-25T12:00:00Z",
        )

    doc = ProfileDocument(
        name="Weights",
        rid="local:weights-a1b2c3",
        provenance="self_published",
        research_interests=[
            entry("Example", 0.5),
            entry("Neutral", 0),
            entry("Unknown", None),
            entry("Excluded", -1),
        ],
    )
    assert doc.interests == ["Example"]
    assert doc.not_interests == ["Excluded"]


def test_served_manifest_entries_carry_effective_access_rights():
    """Each entry gets the EU access-right IRI of its EFFECTIVE tier: a
    public-declared digest of a private CV is labeled non-public."""
    from researcher_profiles.privacy import project_document
    from researcher_profiles.schema import ACCESS_RIGHTS_IRI

    prof = _profile(
        hasPart=[
            {"contentUrl": "sources/cv.md", "role": "cv"},
            {
                "contentUrl": "digest.md",
                "role": "web",
                "visibility": "public",
                "derivedFrom": ["cv"],
            },
            {"contentUrl": "sources/trials.jsonld", "role": "trials"},
        ],
        subjectOf=[{"contentUrl": "personality/expertise.md", "role": "expertise"}],
    )
    served = project_document(prof, "public").model_dump(mode="json", by_alias=True)
    rights = {p["contentUrl"]: p["accessRights"] for p in served["hasPart"] + served["subjectOf"]}
    assert rights == {
        "sources/cv.md": ACCESS_RIGHTS_IRI["private"],
        "digest.md": ACCESS_RIGHTS_IRI["private"],
        "sources/trials.jsonld": ACCESS_RIGHTS_IRI["limited"],
        "personality/expertise.md": ACCESS_RIGHTS_IRI["public"],
    }
    # Derived, never stored: the source document is untouched, and a served
    # copy read back in drops the label.
    assert "accessRights" not in prof.model_dump(mode="json", by_alias=True)["hasPart"][0]
    reloaded = ProfileDocument.model_validate(served)
    assert "accessRights" not in reloaded.model_dump(mode="json", by_alias=True)["hasPart"][0]
