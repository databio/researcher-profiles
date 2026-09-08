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
    CitationRef,
    PersonaResponse,
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

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _patch_profile_json(profile_dir: Path, **fields) -> Path:
    """Read/patch/write ``profile.jsonld`` raw, without re-validating it."""
    path = profile_dir / "profile.jsonld"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Tiers, .publishignore, and chunk source tiers
# --------------------------------------------------------------------------


def _profile(**parts) -> ProfileDocument:
    base = {
        "name": "X",
        "rid": "local:x-a1b2c3",
        "provenance": "self_published",
    }
    base.update(parts)
    return ProfileDocument.model_validate(base)


# ---------------------------------------------------------------------------
# The derivation rule
# ---------------------------------------------------------------------------


def test_most_restrictive_order():
    assert most_restrictive("public", "internal") == "internal"
    assert most_restrictive("public", "restricted") == "restricted"
    assert most_restrictive("internal", "restricted") == "restricted"
    assert most_restrictive() == "public"
    assert most_restrictive(None, "public") == "public"


@pytest.mark.parametrize(
    "content_url, role, declared, expected",
    [
        ("sources/cv.md", "cv", None, "restricted"),
        ("w.md", "web", None, "restricted"),
        ("sources/papers.jsonld", "works", None, "public"),
        ("sources/papers/p1.md", "paper_fulltext", "public", "restricted"),
        ("w.md", "web", "internal", "internal"),
    ],
    ids=[
        "cv-role-default",
        "web-role-default",
        "works-role-default",
        "fulltext-floor-cannot-be-lowered",
        "explicit-tier-overrides-role-default",
    ],
)
def test_part_ref_visibility(content_url, role, declared, expected):
    """Role defaults apply at model level; an explicit tier overrides them,
    except `paper_fulltext`, which is `restricted` even if declared public.
    """
    data = {"contentUrl": content_url, "role": role}
    if declared is not None:
        data["visibility"] = declared
    assert ArtifactRef.model_validate(data).visibility == expected


def test_derivation_takes_most_restrictive_of_sources():
    prof = _profile(
        hasPart=[
            {"contentUrl": "sources/cv.md", "role": "cv"},  # restricted
            {
                "contentUrl": "derived.json",
                "role": "custom",
                "derivedFrom": ["cv"],
            },  # inherits restricted from cv
        ]
    )
    eff = privacy.effective_tiers(prof)
    assert eff["sources/cv.md"] == "restricted"
    assert eff["derived.json"] == "restricted"


def test_profile_default_holds_a_whole_profile_back():
    prof = _profile(
        visibility="internal",
        hasPart=[{"contentUrl": "sources/papers.jsonld", "role": "works"}],
    )
    # A public artifact is floored by the profile default.
    assert privacy.effective_tiers(prof)["sources/papers.jsonld"] == "internal"


# ---------------------------------------------------------------------------
# .publishignore
# ---------------------------------------------------------------------------


def test_publishignore_and_effective_tiers_cannot_drift():
    """Every excluded path is non-public; every non-public artifact is excluded.

    This is the invariant that keeps the declared data and the layout in sync.
    """
    prof = _profile(
        visibility="public",
        hasPart=[
            {"contentUrl": "sources/papers/p1.md", "role": "paper_fulltext"},
            {"contentUrl": "sources/cv.md", "role": "cv"},
            {"contentUrl": "custom.md", "role": "web", "visibility": "internal"},
            {"contentUrl": "sources/papers.jsonld", "role": "works"},
            {"contentUrl": "embeddings/index.json", "role": "embedding_index"},
        ],
    )
    eff = privacy.effective_tiers(prof)
    ignore = set(privacy.publishignore_lines(prof))
    # every manifest artifact above public appears in .publishignore
    for url, tier in eff.items():
        if tier != "public":
            assert url in ignore, f"{url} ({tier}) missing from .publishignore"
    # every listed *artifact* path (not the .cache/.keys prefixes) is non-public
    artifact_urls = set(eff)
    for line in ignore:
        if line in artifact_urls:
            assert eff[line] != "public"


# ---------------------------------------------------------------------------
# The two surfaces must agree: a static rsync deploy and the live API
# ---------------------------------------------------------------------------


def test_static_deploy_and_anonymous_http_publish_the_same_set(tmp_path, make_api_client):
    """What ``.publishignore`` excludes is exactly what anonymous HTTP withholds.

    Two mechanisms decide what reaches the open web: the exclude list a dumb
    rsync honours, and the projection the live API applies. They were written at
    different times for different callers, and nothing forced them to agree.
    That is how the API came to serve, byte for byte, a profile the static
    deploy would have held back. This is the test that keeps them from drifting
    apart again.
    """
    from .factories import ADA, build_profile_dir

    root = tmp_path / "profiles"
    root.mkdir()
    pdir = build_profile_dir(
        root / "drift-check", rid=ADA, cv=True, web=True, index="both", manifest=True
    )
    (pdir / "sources" / "papers").mkdir(parents=True, exist_ok=True)
    (pdir / "sources" / "papers" / "p1.md").write_text("full text\n", encoding="utf-8")
    ResearcherProfile.from_files(pdir).build_manifest(write=True)

    prof = ResearcherProfile.from_files(pdir)
    excluded = set(privacy.publishignore_lines(prof.metadata))
    all_artifacts = set(privacy.effective_tiers(prof.metadata))

    client = make_api_client(root)
    detail = client.get("/api/v1/profiles/drift-check")
    assert detail.status_code == 200
    withheld = set(detail.json()["withheld"])

    # rsync's view of "publishable", minus the prefixes it excludes by hand.
    rsync_publishes = {
        url
        for url in all_artifacts
        if url not in excluded
        and not any(url.startswith(p) for p in privacy.ALWAYS_RESTRICTED_PREFIXES)
    }
    http_publishes = all_artifacts - withheld
    assert http_publishes == rsync_publishes


# ---------------------------------------------------------------------------
# explain_tiers: the decision and its explanation are one computation
# ---------------------------------------------------------------------------


def test_explain_tiers_is_the_only_implementation_of_effective_tiers():
    prof = _profile(
        visibility="internal",
        hasPart=[
            {"contentUrl": "sources/cv.md", "role": "cv"},
            {"contentUrl": "derived.json", "role": "custom", "derivedFrom": ["cv"]},
            {"contentUrl": "sources/papers.jsonld", "role": "works"},
        ],
    )
    explained = privacy.explain_tiers(prof)
    assert {k: v.effective for k, v in explained.items()} == privacy.effective_tiers(prof)


def test_raised_by_names_the_specific_cause_not_the_rule():
    prof = _profile(
        visibility="public",
        hasPart=[
            {"contentUrl": "sources/cv.md", "role": "cv"},
            {"contentUrl": "derived.json", "role": "custom", "derivedFrom": ["cv"]},
        ],
    )
    note = privacy.explain_tiers(prof)["derived.json"]
    assert note.declared == "public"
    assert note.effective == "restricted"
    assert any("sources/cv.md" in phrase for phrase in note.raised_by)


def test_the_profile_default_names_itself():
    prof = _profile(
        visibility="internal",
        hasPart=[{"contentUrl": "sources/papers.jsonld", "role": "works"}],
    )
    works = privacy.explain_tiers(prof)["sources/papers.jsonld"]
    assert works.effective == "internal"
    assert works.raised_by == ["the profile default (internal)"]


def test_a_public_artifact_has_nothing_to_explain():
    prof = _profile(hasPart=[{"contentUrl": "sources/papers.jsonld", "role": "works"}])
    assert privacy.explain_tiers(prof)["sources/papers.jsonld"].raised_by == []


def test_fulltext_is_locked_with_a_sentence_a_person_can_read():
    prof = _profile(hasPart=[{"contentUrl": "sources/papers/p1.md", "role": "paper_fulltext"}])
    entry = privacy.explain_tiers(prof)["sources/papers/p1.md"]
    assert entry.locked is True
    assert entry.lock_reason == privacy.FULLTEXT_LOCK_REASON
    assert "legal floor" in entry.lock_reason


# ---------------------------------------------------------------------------
# Viewer tiers read the scale in the OTHER direction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "viewer, artifact, allowed",
    [
        ("public", "public", True),
        ("public", "internal", False),
        ("public", "restricted", False),
        ("internal", "public", True),
        ("internal", "internal", True),
        ("internal", "restricted", False),
        ("restricted", "restricted", True),
    ],
)
def test_tier_allows(viewer, artifact, allowed):
    assert privacy.tier_allows(viewer, artifact) is allowed


def test_narrow_viewer_is_a_cap_not_the_artifact_rule():
    """The two rules move opposite ways; using the wrong one un-caps a preview."""
    assert privacy.narrow_viewer("restricted", "public") == "public"
    assert privacy.narrow_viewer("public", "restricted") == "public"
    assert privacy.narrow_viewer("restricted", "internal") == "internal"
    # ...whereas most_restrictive, the ARTIFACT rule, would have said otherwise.
    assert most_restrictive("restricted", "public") == "restricted"


def test_profile_visible_folds_in_a_host_floor():
    prof = _profile(visibility="public")
    assert privacy.profile_visible(prof, "public") is True
    assert privacy.profile_visible(prof, "public", floor="internal") is False
    assert privacy.profile_visible(prof, "internal", floor="internal") is True


# ---------------------------------------------------------------------------
# derivedFrom: the synthesis carve-out, asserted rather than left implicit
# ---------------------------------------------------------------------------


def test_no_built_manifest_role_declares_derivedFrom(tmp_path):
    """Authored syntheses do not inherit their sources' tier. See spec section 3.

    A paper summary, ``expertise.md``, and ``SOUL.md`` are new works, not
    reproductions, so ``manifest.build_manifest`` populates ``derivedFrom`` on
    nothing. That absence is load-bearing: were a summary to inherit from the
    full text it summarizes, the public surface of every profile would collapse
    to nothing. Asserted here so the carve-out is a decision on the record
    rather than an omission nobody noticed. If a future role genuinely
    REPRODUCES a restricted source, it gets ``derivedFrom`` and this test
    changes with it.
    """
    from .factories import ADA, build_profile_dir

    pdir = build_profile_dir(
        tmp_path / "derivation", rid=ADA, cv=True, web=True, index="both", manifest=True
    )
    (pdir / "sources" / "papers").mkdir(parents=True, exist_ok=True)
    (pdir / "sources" / "papers" / "p1.md").write_text("full text\n", encoding="utf-8")
    ResearcherProfile.from_files(pdir).build_manifest(write=True)

    prof = ResearcherProfile.from_files(pdir)
    parts = list(prof.metadata.has_part) + list(prof.metadata.subject_of)
    assert parts, "sanity: the fixture has a manifest"
    assert all(not p.derived_from for p in parts)


def test_render_publishignore_is_stable_text():
    prof = _profile(hasPart=[{"contentUrl": "sources/cv.md", "role": "cv"}])
    body = privacy.render_publishignore(prof)
    assert body.endswith("\n")
    assert body == privacy.render_publishignore(prof)  # deterministic


# ---------------------------------------------------------------------------
# Chunk source tiers (the embedding-export filter) + the grant default
# ---------------------------------------------------------------------------


def test_grant_role_defaults_to_restricted():
    """Grant-derived embedding text is restricted, though the collection is public."""
    assert ROLE_DEFAULT_VISIBILITY["grant"] == "restricted"
    assert role_default_visibility("grant") == "restricted"
    # The plural collection role stays public (bibliographic record).
    assert role_default_visibility("grants") == "public"


def test_chunk_source_tiers_tolerates_missing_profile():
    # None profile: default treated as public, role defaults still filter.
    tiers = privacy.chunk_source_tiers(None, [("cv", "cv"), ("soul", "soul")])
    assert tiers[("cv", "cv")] == "restricted"
    assert tiers[("soul", "soul")] == "public"


# --------------------------------------------------------------------------
# The persona-availability guard
# --------------------------------------------------------------------------


class TestPersonaGuard:
    """Library-level tests for the persona-availability guard.

    The persona methods (``.ask``/``.review``/``.innovate``/``.riff``) must raise
    ``PersonaUnavailableError``, never role-play an empty persona and
    never call the LLM, when a profile lacks a synthesized persona (empty
    ``expertise.md`` + ``SOUL.md``).
    """

    @pytest.fixture
    def incomplete(self, fixture_profile):
        """A profile whose persona documents were never synthesized."""
        return ResearcherProfile.from_files(fixture_profile("incomplete-profile"))

    @pytest.fixture
    def lite_jane(self, jane_doe_dir):
        """The complete fixture with its level flipped to ``lite`` on disk."""
        _patch_profile_json(jane_doe_dir, level="lite")
        return ResearcherProfile.from_files(jane_doe_dir)

    # ----------------------------------------------------------------------
    # has_persona predicate
    # ----------------------------------------------------------------------

    def test_has_persona_true_for_complete(self, jane_doe_readonly):
        assert jane_doe_readonly.has_persona is True

    def test_has_persona_false_for_incomplete(self, incomplete):
        assert incomplete.has_persona is False

    def test_persona_unavailable_is_profile_error(self):
        assert issubclass(PersonaUnavailableError, ProfileError)

    # ----------------------------------------------------------------------
    # Level gate: a lite profile is never persona-ready, even with stray content
    # ----------------------------------------------------------------------

    def test_has_persona_false_for_lite_even_with_content(self, lite_jane):
        # SOUL/expertise are present on disk, but level=lite gates persona off.
        assert lite_jane.level == "lite"
        assert lite_jane.expertise.strip()
        assert lite_jane.soul.strip()
        assert lite_jane.has_persona is False

    def test_ask_raises_persona_unavailable_for_lite(self, lite_jane):
        fake = stub_llm(lite_jane)
        with pytest.raises(PersonaUnavailableError):
            lite_jane.persona.ask("q")
        fake.complete.assert_not_called()

    def test_ask_raises_persona_unavailable(self, incomplete):
        fake = stub_llm(incomplete)
        with pytest.raises(PersonaUnavailableError):
            incomplete.persona.ask("q")
        fake.complete.assert_not_called()


# --------------------------------------------------------------------------
# The refusal contract on .ask / .review
# --------------------------------------------------------------------------


class _Hit:
    def __init__(self, sid, score, text="t", source_type="paper_summary"):
        self.source_id = sid
        self.source_type = source_type
        self.text = text
        self.score = score
        self.meta = {}


class TestRefusalContract:
    """Tests for refusal contract on .ask / .review."""

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
        """A below-threshold corpus refuses without ever calling the LLM.

        The refusal payload is inert: no citations, no model, no token spend.
        A refusal can never be mistaken for a cheap answer.
        """
        fake = stub_llm(jane_doe, "ok")
        jane_doe.index.search = MagicMock(return_value=[_Hit("a", score)])  # type: ignore[method-assign]

        r = getattr(jane_doe.persona, method)(payload, **kwargs)
        assert isinstance(r, PersonaResponse)
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

    def test_citations_are_citation_refs(self, jane_doe):
        stub_llm(jane_doe, "ans")
        jane_doe.index.search = MagicMock(  # type: ignore[method-assign]
            return_value=[_Hit("doe2016example", 0.8), _Hit("doe2019methods", 0.6)]
        )
        r = jane_doe.persona.ask("q?")
        assert all(isinstance(c, CitationRef) for c in r.citations)
        relevances = {c.paper_id: c.relevance for c in r.citations}
        assert relevances["doe2016example"] == 0.8

    def test_ask_emits_citations_for_paper_summary_chunks(self, jane_doe):
        """Regression: paper_summary chunks must produce non-empty citations.

        The embeddings store writes ``source_type="paper_summary"``; a check
        for ``"summary"`` in ``_render_evidence`` would leave ``.ask()`` citations
        always empty. This asserts the store's real value is honored.
        """
        stub_llm(jane_doe, "ans")
        jane_doe.index.search = MagicMock(  # type: ignore[method-assign]
            return_value=[_Hit("doe2016example", 0.8, source_type="paper_summary")]
        )
        r = jane_doe.persona.ask("q?")
        assert r.citations, "paper_summary chunks must yield citations"
        assert r.citations[0].paper_id == "doe2016example"

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
        # First two are the historical turns, last is the current question.
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "earlier question"
        assert msgs[1]["role"] == "assistant"
        assert "new q?" in msgs[-1]["content"]
