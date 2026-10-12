"""The ratified text, checked against what the implementation emits: pure text, no network or LLM.

Three bodies of frozen text: the packaged consumer skill tree, the spec prose in ``docs/rp-spec/``
(one level up, outside this component) whose fenced examples must validate against the generated
schemas, and the published fixture corpus in ``tests/fixtures/published/``.
Siblings: ``test_guardrails.py`` (runtime shape), ``test_packaging.py`` (wheel and sdist contents).
"""

import json
import re
from pathlib import Path

import jsonschema
import pytest
import yaml

from researcher_profiles import ResearcherProfile
from researcher_profiles.validate import validate_artifact, validate_profile_dir

from .factories import CONTEXT_IRI, FIXTURE_DIR, MONOREPO_ROOT, REPO_ROOT

#: The packaged consumer skill: ships in the wheel, `rp skill --install` writes it, rp-browser publishes it.
SKILL_TREE = REPO_ROOT / "src" / "researcher_profiles" / "skill"
SKILL_MD = SKILL_TREE / "SKILL.md"
SKILL_TEXT = SKILL_MD.read_text(encoding="utf-8")

#: Implementation-independent spec prose at the monorepo root, consumed by both validators.
SPEC_DIR = MONOREPO_ROOT / "docs" / "rp-spec"

#: The shared conformance corpus stays under spec/.
CORPUS_DIR = MONOREPO_ROOT / "spec" / "conformance" / "valid"

SCHEMA_DIR = REPO_ROOT / "schemas"

#: Broken, hostile and open-vocab published directories: asserted against, never constructed.
FIXTURES_ROOT = FIXTURE_DIR / "published"

LOSING_CONTEXT_IRI = "rp/v1"

ABANDONED_IRI = "w3id.org/rp/v1"

#: The ratified manifest `role` vocabulary; the skill uses it closed, the format leaves it open.
RATIFIED_ROLES = {
    "agent_entry_point",
    "works",
    "grants",
    "citations",
    "cv",
    "interview",
    "paper_summary",
    "paper_fulltext",
    "web",
    "embedding_index",
    "embedding_index_sqlite",
    "soul",
    "expertise",
    "html",
}

#: Top-level keys a consumer knows (modeled fields plus ratified flags); only detects the open-vocab fixture's unknowns.
KNOWN_TOP_LEVEL_KEYS = {
    "@context",
    "@id",
    "@type",
    "affiliation",
    "affiliation_id",
    "anchor",
    "career",
    "collaborators",
    "conformsTo",
    "critiques",
    "dateModified",
    "email",
    "expertise",
    "field",
    "hasPart",
    "identifier",
    "intellectual_lineage",
    "interests",
    "jobTitle",
    "level",
    "license",
    "methodological_commitments",
    "name",
    "not_interests",
    "paper_stats",
    "provenance",
    "provenanceNote",
    "recurring_positions",
    "researchOutputs",
    "rid",
    "sameAs",
    "subfields",
    "subjectOf",
    "summary",
    "training",
    "url",
    "verifiedAt",
    "expertiseCitesPaperIds",
    "hasCitationGraph",
    "hasEmbeddingIndex",
}

#: Minimal static published trees, each named for the one thing a consumer must cope with.
FIXTURE_DIRS = [
    "current-gen",
    "legacy-nobrackets",
    "partial-build",
    "lite",
    "deep",
    "broken-artifacts",
    "broken-context",
    "open-vocab",
    "hostile",
    "not-a-profile",
    "synthetic",
    "historical",
]

_FENCE = re.compile(r"```(json(?:ld)?)\s*\n(.*?)```", re.DOTALL)


def _frontmatter(text: str) -> str:
    """Return the YAML frontmatter block between the leading ``---`` markers."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        pytest.fail("SKILL.md has no --- delimited frontmatter block")
    return match.group(1)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    """Every file under ``root``, keyed by its root-relative POSIX path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _spec_docs() -> list[Path]:
    """The spec prose documents (the conformance corpus is owned by the corpus tests)."""
    return sorted(
        p for p in SPEC_DIR.rglob("*.md") if "conformance" not in p.relative_to(SPEC_DIR).parts
    )


def _complete_blocks(md_text: str) -> list[str]:
    """Fenced json/jsonld blocks that are complete documents, not sketches.

    A sketch is elided (``...``) or carries ``//`` comment lines. The comment
    test looks for a line that *starts* with ``//``, because a bare ``"//" in
    content`` also matches every ``https://`` inside a legitimate example.
    """
    blocks = []
    for match in _FENCE.finditer(md_text):
        content = match.group(2).strip()
        if not content or not content.startswith(("{", "[")):
            continue
        if "..." in content or "…" in content:
            continue
        if any(line.lstrip().startswith("//") for line in content.splitlines()):
            continue
        blocks.append(content)
    return blocks


def _load_schema(name: str) -> dict | None:
    path = SCHEMA_DIR / name
    return json.loads(path.read_text()) if path.exists() else None


def _collect(predicate) -> list:
    params = []
    for md_file in _spec_docs():
        for content in _complete_blocks(md_file.read_text()):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                continue
            schema_name = predicate(parsed)
            if schema_name:
                params.append(
                    pytest.param(
                        parsed,
                        schema_name,
                        id=f"{md_file.relative_to(SPEC_DIR)}:{schema_name}",
                    )
                )
    return params


def _schema_for(block) -> str | None:
    if not isinstance(block, dict):
        return None
    if "rp:profileList" in block:
        return "profile_list.schema.json"
    # One document shape: a Person record (optionally carrying its hasPart /
    # subjectOf manifest) validates against the single profile schema.
    if block.get("@type") == "Person":
        return "profile_jsonld.schema.json"
    return None


SPEC_EXAMPLES = _collect(_schema_for)


class TestSkillDocs:
    """Structural checks on the consumer skill and spec docs; not prose quality."""

    def test_the_packaged_skill_tree_is_not_empty(self):
        """The shipped skill tree still carries all of its parts (guards a vacuous pass on an emptied tree)."""
        found = set(_tree_bytes(SKILL_TREE))
        required = {"SKILL.md", "reference/read-order.md", "reference/failure-modes.md"}
        assert required <= found, f"packaged skill tree is missing {sorted(required - found)}"
        assert any(rel.startswith("examples/") for rel in found), (
            f"packaged skill tree carries no worked example under examples/; found {sorted(found)}"
        )

    def test_skill_frontmatter(self):
        front = yaml.safe_load(_frontmatter(SKILL_TEXT))
        assert front.get("name") == "researcher-profile", front.get("name")
        assert front["description"].strip(), "frontmatter must carry a non-empty description"

    def test_skill_line_budget(self):
        lines = SKILL_TEXT.splitlines()
        assert len(lines) < 300, f"SKILL.md is {len(lines)} lines; keep it under 300"

    def test_artifact_roles_in_skill(self):
        match = re.search(
            r"The `role` tokens you will use[\s\S]*?:\s*\n+((?:\|[^\n]*\n)+)", SKILL_TEXT
        )
        assert match, (
            "expected a markdown table after 'The `role` tokens you will use' "
            "in SKILL.md enumerating the manifest role vocabulary"
        )
        rows = [ln for ln in match.group(1).splitlines() if ln.startswith("|")]
        tokens: list[str] = []
        for row in rows[2:]:  # skip header + separator
            first_cell = row.split("|")[1]
            tokens += re.findall(r"`([a-z][a-z_]*)`", first_cell)
        assert tokens, "no role tokens found in the skill's role table"
        unknown = [t for t in tokens if t not in RATIFIED_ROLES]
        assert not unknown, f"SKILL.md uses unratified role(s): {unknown}"
        # The roles the read order depends on must all be present.
        consumer_path = {"expertise", "soul", "works", "paper_summary"}
        missing = consumer_path - set(tokens)
        assert not missing, f"SKILL.md role table missing consumer-path role(s): {sorted(missing)}"

    def test_context_iri(self):
        text = SKILL_TEXT
        assert CONTEXT_IRI in text, f"SKILL.md never mentions the context IRI {CONTEXT_IRI}"
        assert LOSING_CONTEXT_IRI not in text, (
            f"SKILL.md must not reference the abandoned context IRI form {LOSING_CONTEXT_IRI!r}"
        )


class TestSpecExamples:
    """Spec prose against the generated artifacts, not hand-written schema copies."""

    def test_all_spec_json_blocks_parse(self):
        """Every complete fenced json/jsonld block in the spec is valid JSON."""
        failures = []
        for md_file in _spec_docs():
            for content in _complete_blocks(md_file.read_text()):
                try:
                    json.loads(content)
                except json.JSONDecodeError as e:
                    failures.append(f"{md_file.relative_to(SPEC_DIR)}: {e}")
        if failures:
            pytest.fail("JSON parse failures:\n" + "\n".join(failures))

    @pytest.mark.parametrize("block,schema_name", SPEC_EXAMPLES)
    def test_spec_example_validates(self, block, schema_name):
        """Spec examples validate against the schemas generated from the models."""
        schema = _load_schema(schema_name)
        if schema is None:
            pytest.fail(f"Generated schema {schema_name} is missing")
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(block))
        if errors:
            msg = "\n".join(e.message for e in errors[:5])
            pytest.fail(f"Validation errors against {schema_name}:\n{msg}")

    def test_spec_uses_the_canonical_context_iri(self):
        """No spec document reintroduces the never-registered w3id identity."""
        offenders = [
            str(md.relative_to(SPEC_DIR))
            for md in _spec_docs()
            if ABANDONED_IRI in md.read_text() and md.name != "CHANGELOG.md"  # records the removal
        ]
        assert not offenders, (
            f"docs referencing the abandoned context IRI {ABANDONED_IRI!r}: {offenders}"
        )

    def test_profile_example_manifest_uses_the_role_vocabulary(self):
        """A spec profile example's manifest uses hasPart/subjectOf + role tokens a real profile emits.

        Also guards against an empty example collection.
        """
        fixtures = sorted(CORPUS_DIR.glob("*/profile.jsonld"))
        examples = [
            block
            for block, schema_name in ((p.values[0], p.values[1]) for p in SPEC_EXAMPLES)
            if schema_name == "profile_jsonld.schema.json"
        ]
        with_manifest = [e for e in examples if e.get("hasPart") or e.get("subjectOf")]
        # Both corpora are committed, so an empty one is a repo bug; skip or return would check nothing.
        assert fixtures, f"no conformance profiles under {CORPUS_DIR}"
        assert with_manifest, (
            "no spec profile example carries a hasPart/subjectOf manifest; "
            "profile-document.md must keep one"
        )
        for example in with_manifest:
            assert "artifacts" not in example, (
                "spec example still uses the retired published `artifacts` array"
            )
            assert example.get("@context") == CONTEXT_IRI
            for entry in [*example.get("hasPart", []), *example.get("subjectOf", [])]:
                assert "contentUrl" in entry and "rel" not in entry and "href" not in entry, (
                    "manifest entries must use contentUrl/role, not the retired rel/href"
                )
        # The union across the whole corpus (ada-lovelace-cited adds `citations`), not filename order.
        real_roles = set()
        for fixture in fixtures:
            real = json.loads(fixture.read_text())
            real_roles |= {
                e.get("role") for e in [*real.get("hasPart", []), *real.get("subjectOf", [])]
            }
        for example in with_manifest:
            ex_roles = {
                e.get("role") for e in [*example.get("hasPart", []), *example.get("subjectOf", [])]
            }
            # Subset only: an `or real_roles <= ex_roles` escape hatch would admit any role.
            assert ex_roles <= real_roles, (
                f"spec example roles {sorted(ex_roles - real_roles)} are not in the "
                f"real profile role vocabulary {sorted(real_roles)}"
            )


class TestPublishedFixtureTree:
    """The `published/` corpus is on disk and still lying in the ways a consumer must cope with.

    A DIFFERENT corpus from `spec/conformance/` (owned by `test_validate.py::TestConformanceCorpus`);
    do not merge the two.
    """

    @pytest.mark.parametrize("fixture_name", [f for f in FIXTURE_DIRS if f != "not-a-profile"])
    def test_fixture_manifests_valid_json(self, fixture_name):
        manifest = FIXTURES_ROOT / fixture_name / "profile.jsonld"
        assert manifest.is_file(), f"missing {manifest}"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"{manifest} did not parse to a JSON object"

    @pytest.mark.parametrize(
        "fixture_name",
        ["current-gen", "legacy-nobrackets", "lite", "deep", "synthetic", "historical", "hostile"],
    )
    def test_well_formed_trees_validate(self, fixture_name):
        """The trees whose lie is not a format defect must not drift from the vocabulary `manifest.py` emits."""
        report = validate_profile_dir(FIXTURES_ROOT / fixture_name)
        assert report.ok, [v.message for a in report.artifacts for v in a.violations] + [
            v.message for v in report.cross_artifact
        ]

    def test_not_a_profile_is_html(self):
        manifest = FIXTURES_ROOT / "not-a-profile" / "profile.jsonld"
        text = manifest.read_text(encoding="utf-8").lstrip()
        assert text[:15].lower().startswith(("<!doctype", "<html")), (
            f"not-a-profile/profile.jsonld should look like an HTML document, got: {text[:40]!r}"
        )
        with pytest.raises(json.JSONDecodeError):
            json.loads(text)

    def test_hostile_contains_injection(self):
        soul = FIXTURES_ROOT / "hostile" / "personality" / "SOUL.md"
        text = soul.read_text(encoding="utf-8")
        assert re.search(r"\[SYSTEM:.*?\]", text, re.IGNORECASE | re.DOTALL), (
            "hostile/personality/SOUL.md should contain an instruction-shaped "
            "injection block, e.g. '[SYSTEM: ...]'"
        )
        assert "evil.example.com" in text, (
            "hostile/personality/SOUL.md should contain an off-origin link "
            "to exercise the no-auto-fetch rule"
        )

    def test_open_vocab_has_unknown_keys(self):
        manifest = FIXTURES_ROOT / "open-vocab" / "profile.jsonld"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        unknown_top_level = set(data.keys()) - KNOWN_TOP_LEVEL_KEYS
        assert unknown_top_level, "open-vocab profile.jsonld has no unknown top-level keys"

        roles_seen = {
            entry.get("role")
            for entry in (*data.get("hasPart", []), *data.get("subjectOf", []))
            if isinstance(entry, dict)
        }
        unknown_roles = roles_seen - RATIFIED_ROLES
        assert unknown_roles, "open-vocab profile.jsonld has no unrecognized artifact role"

    def test_broken_context_is_read_by_key_name(self):
        """A dead `@context` IRI is a warning, not a load failure: read by key name, never dereferenced."""
        tree = FIXTURES_ROOT / "broken-context"
        assert validate_artifact(tree / "profile.jsonld", "profile_jsonld").ok
        prof = ResearcherProfile.from_files(tree)
        assert prof.metadata.context != CONTEXT_IRI
        assert prof.has_persona

    @pytest.mark.parametrize(
        "fixture_name, missing",
        [
            (
                "broken-artifacts",
                {
                    "sources/summaries/voss2019signal.summary.md",
                    "sources/summaries/voss2021screen.summary.md",
                    "personality/expertise.md",
                },
            ),
            ("partial-build", {"personality/SOUL.md", "personality/expertise.md"}),
        ],
        ids=["listed-summaries-missing", "full-without-persona-files"],
    )
    def test_dangling_manifest_entries_are_reported(self, fixture_name, missing):
        """Every manifest entry with no file is named; both trees declare `full` without persona files."""
        report = validate_profile_dir(FIXTURES_ROOT / fixture_name)
        found = {v.found for v in report.cross_artifact if v.keyword == "content_url_missing"}
        assert found == missing
        prof = ResearcherProfile.from_files(FIXTURES_ROOT / fixture_name)
        assert prof.level == "full"
        assert not prof.has_persona

    @pytest.mark.parametrize(
        "fixture_name, cites",
        [("current-gen", True), ("legacy-nobrackets", False)],
        ids=["current-gen-cites", "legacy-nobrackets-does-not"],
    )
    def test_expertise_cites_paper_ids_tells_the_truth(self, fixture_name, cites):
        """`expertiseCitesPaperIds` matches what `expertise.md` contains (the skill follows `[paper_id]` only when true)."""
        prof = ResearcherProfile.from_files(FIXTURES_ROOT / fixture_name)
        cited = set(re.findall(r"\[([A-Za-z0-9]+)\]", prof.expertise))
        assert prof.metadata.expertise_cites_paper_ids is cites
        assert bool(cited) is cites
        assert cited <= set(prof.summaries)

    def test_deep_tree_ships_the_deep_only_parts(self):
        """`deep` adds grants, a CV and web pages to `full`, and every listed part is on disk."""
        tree = FIXTURES_ROOT / "deep"
        prof = ResearcherProfile.from_files(tree)
        assert prof.level == "deep"
        parts = {p.role: p.content_url for p in prof.metadata.has_part}
        assert {"grants", "cv", "web"} <= set(parts)
        assert len(prof.grants) == 2
        report = validate_profile_dir(tree)
        assert not [v for v in report.cross_artifact if v.keyword == "content_url_missing"]

    def test_lite_tree_has_no_persona(self):
        """`lite` is identity plus works: no persona documents, no `subjectOf` entries."""
        prof = ResearcherProfile.from_files(FIXTURES_ROOT / "lite")
        assert prof.level == "lite"
        assert not prof.has_persona
        assert not prof.metadata.subject_of

    @pytest.mark.parametrize("fixture_name", ["synthetic", "historical"])
    def test_persona_provenance_trees_carry_a_local_rid(self, fixture_name):
        """A `synthetic` or `historical` profile is a persona with a minted rid, not a living researcher."""
        prof = ResearcherProfile.from_files(FIXTURES_ROOT / fixture_name)
        assert prof.provenance == fixture_name
        assert prof.rid.startswith("local:")
        assert prof.has_persona
