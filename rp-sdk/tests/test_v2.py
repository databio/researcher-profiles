"""v2 draft format: anchors, migration from v1, and the v2 validator (issues #4-#7)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from researcher_profiles.v2 import V2_CONTEXT, migrate_profile, slug, validate_v2
from researcher_profiles.v2 import anchors as v2_anchors
from researcher_profiles.v2.common import normalize_datetime
from researcher_profiles.v2.migrate import MigrationError
from researcher_profiles.v2.validate import is_valid

REPO = Path(__file__).resolve().parents[2]
V1_FIXTURE = REPO / "spec" / "conformance" / "valid" / "ada-lovelace"
FIXED_ID = "urn:uuid:5f0c2a1e-8d3b-4c7a-9e21-6b4f0d9a7c35"


def codes(findings, level="error"):
    return {f.code for f in findings if f.level == level}


@pytest.fixture
def migrated(tmp_path: Path) -> Path:
    out = tmp_path / "ada"
    migrate_profile(V1_FIXTURE, out, page_id=FIXED_ID)
    return out


def edit_page(root: Path, fn) -> None:
    page = json.loads((root / "profile.jsonld").read_text())
    fn(page)
    (root / "profile.jsonld").write_text(json.dumps(page, indent=2))


class TestSlug:
    @pytest.mark.parametrize(
        ("heading", "anchor"),
        [
            ("Intellectual lineage", "intellectual-lineage"),
            ("Methods *championed*", "methods-championed"),
            ("[Linked](https://x.org) heading", "linked-heading"),
            ("Café & co.", "café--co"),
            ("`code` words", "code-words"),
        ],
    )
    def test_vectors(self, heading, anchor):
        assert slug.slugify(heading) == anchor

    def test_closing_hashes_are_not_part_of_the_heading(self):
        assert [s.anchor for s in slug.scan("## Recurring critiques ##\nx\n").sections] == [
            "recurring-critiques"
        ]

    def test_headings_in_code_fences_and_setext_do_not_count(self):
        text = "Intro\n\n## Methods\n\n```\n## Not a heading\n```\n\nSetext\n------\n\n## Career\nend\n"
        assert [s.anchor for s in slug.scan(text).sections] == ["methods", "career"]

    def test_duplicates_and_empty_are_reported_not_suffixed(self):
        s = slug.scan("## Methods\na\n## Methods\nb\n## !!!\nc\n")
        assert s.duplicates == ["methods"]
        assert s.empty == ["!!!"]
        assert [x.anchor for x in s.sections] == ["methods", "methods", ""]

    def test_section_hash_ignores_crlf_and_covers_heading_to_next_heading(self):
        a = slug.scan("pre\n## A\nx\n## B\ny\n").sections
        b = slug.scan("pre\r\n## A\r\nx\r\n## B\r\ny\r\n").sections
        assert [s.sha256 for s in a] == [s.sha256 for s in b]
        assert a[0].sha256 != slug.scan("pre\n## A\nx!\n## B\ny\n").sections[0].sha256
        assert a[1].sha256 == slug.scan("pre\n## A\nx!\n## B\ny\n").sections[1].sha256


class TestCitations:
    def test_only_tokens_matching_a_paper_id_cite(self):
        ids = {"smith2020a", "jones2019b"}
        text = "Claim [smith2020a]. Two [jones2019b, smith2020a]. Not [sic] or [1] or [a link](http://x)."
        assert v2_anchors.cited_papers(text, ids) == ["smith2020a", "jones2019b"]


class TestDatetime:
    @pytest.mark.parametrize(
        ("raw", "norm"),
        [
            ("2026-10-17T00:33:11+00:00", "2026-10-17T00:33:11Z"),
            ("2026-10-09T13:30:07.304956+00:00", "2026-10-09T13:30:07Z"),
            ("2026-10-09T13:30:49Z", "2026-10-09T13:30:49Z"),
            ("2026-10-09T09:30:49-04:00", "2026-10-09T13:30:49Z"),
            ("2026-10-09", None),
        ],
    )
    def test_normalize(self, raw, norm):
        assert normalize_datetime(raw) == norm


class TestMigrate:
    def test_v1_fixture_migrates_to_a_valid_v2_profile(self, migrated: Path):
        findings = validate_v2(migrated)
        assert is_valid(findings), [f for f in findings if f.level == "error"]
        page = json.loads((migrated / "profile.jsonld").read_text())
        assert page["@type"] == "ProfilePage"
        assert page["@id"] == FIXED_ID
        assert page["@context"] == V2_CONTEXT
        assert page["mainEntity"]["@type"] == "Person"
        assert "subjectOf" not in page and "provenance" not in page

    def test_source_directory_is_not_modified(self, tmp_path: Path):
        before = {p: p.read_bytes() for p in V1_FIXTURE.rglob("*") if p.is_file()}
        migrate_profile(V1_FIXTURE, tmp_path / "out")
        assert before == {p: p.read_bytes() for p in V1_FIXTURE.rglob("*") if p.is_file()}

    def test_random_uuid_when_none_given(self, tmp_path: Path):
        a = migrate_profile(V1_FIXTURE, tmp_path / "a")["page_id"]
        b = migrate_profile(V1_FIXTURE, tmp_path / "b")["page_id"]
        assert a != b and a.startswith("urn:uuid:")

    def test_missing_listed_file_stops_migration(self, tmp_path: Path):
        src = tmp_path / "src"
        shutil.copytree(V1_FIXTURE, src)
        doc = json.loads((src / "profile.jsonld").read_text())
        victim = next(
            e for e in doc["hasPart"] + doc.get("subjectOf", []) if e.get("role") != "works"
        )
        (src / victim["contentUrl"]).unlink()
        with pytest.raises(MigrationError):
            migrate_profile(src, tmp_path / "out")
        rep = migrate_profile(src, tmp_path / "out", drop_missing=True)
        assert rep["dropped"].get("entry whose file is missing") == 1

    def test_migration_never_invents_declared_or_generator(self, migrated: Path):
        page = json.loads((migrated / "profile.jsonld").read_text())
        text = json.dumps(page)
        assert '"generator": "llm"' not in text
        for e in page["hasPart"]:
            assert e.get("method") != "declared"

    def test_v1_profile_fails_v2_validation(self):
        assert not is_valid(validate_v2(V1_FIXTURE))


class TestValidate:
    def test_page_needs_uuid_and_date(self, migrated: Path):
        edit_page(
            migrated, lambda p: (p.update({"@id": "https://example.org/me"}), p.pop("dateModified"))
        )
        assert {"page-id", "dateModified"} <= codes(validate_v2(migrated))

    def test_method_needs_sha256(self, migrated: Path):
        def f(p):
            e = next(x for x in p["hasPart"] if x["role"] == "expertise")
            e["method"] = "inferred"
            e.pop("sha256", None)

        edit_page(migrated, f)
        assert "sha256-required" in codes(validate_v2(migrated))

    def test_page_default_method_also_needs_sha256(self, migrated: Path):
        def f(p):
            p["partDefaults"] = {"method": "inferred"}
            next(x for x in p["hasPart"] if x["role"] == "expertise").pop("sha256", None)

        edit_page(migrated, f)
        assert "sha256-required" in codes(validate_v2(migrated))

    def test_user_must_be_declared_and_generator_form(self, migrated: Path):
        def f(p):
            p["mainEntity"]["summary"].update({"generator": "user", "method": "inferred"})
            p["mainEntity"]["expertise"][0]["generator"] = "llm"

        edit_page(migrated, f)
        assert {"user-not-declared", "generator"} <= codes(validate_v2(migrated))

    def test_datetime_form(self, migrated: Path):
        edit_page(migrated, lambda p: p.update({"dateModified": "2026-10-17T00:33:11+00:00"}))
        assert "datetime" in codes(validate_v2(migrated))

    def test_person_id_must_be_orcid_of_rid(self, migrated: Path):
        edit_page(migrated, lambda p: p["mainEntity"].update({"@id": "https://example.org/ada"}))
        assert "person-id" in codes(validate_v2(migrated))

    def test_cache_part_and_v1_keys_rejected(self, migrated: Path):
        def f(p):
            p["hasPart"].append({"role": "embedding_index_sqlite", "contentUrl": "profile.jsonld"})
            p["provenance"] = "third_party"

        edit_page(migrated, f)
        assert {"cache-part", "v1-key"} <= codes(validate_v2(migrated))

    def test_excluded_and_partdefaults_shape(self, migrated: Path):
        edit_page(
            migrated,
            lambda p: p.update(
                {"excluded": [{"type": "person", "id": "x"}], "partDefaults": {"derivedFrom": []}}
            ),
        )
        assert {"excluded", "partDefaults"} <= codes(validate_v2(migrated))

    def test_inherits_visibility_only_on_files(self, migrated: Path):
        edit_page(
            migrated,
            lambda p: p["mainEntity"]["summary"].update(
                {"derivedFrom": [{"file": "works", "inheritsVisibility": True}]}
            ),
        )
        assert "inherits-placement" in codes(validate_v2(migrated))

    def test_unresolved_paper_reference(self, migrated: Path):
        edit_page(
            migrated,
            lambda p: p["mainEntity"]["summary"].update(
                {"derivedFrom": [{"paper": "nobody1999none"}]}
            ),
        )
        assert "ref-paper" in codes(validate_v2(migrated))


class TestAnchors:
    def test_stamped_anchors_validate_and_an_edit_is_a_warning_not_an_error(self, migrated: Path):
        v2_anchors.stamp(
            migrated,
            "expertise",
            method="inferred",
            generator="builder@2.1/claude-opus-5-5",
            generated_at="2026-11-13T09:00:00Z",
        )
        assert is_valid(validate_v2(migrated))
        page = json.loads((migrated / "profile.jsonld").read_text())
        entry = next(e for e in page["hasPart"] if e["role"] == "expertise")
        assert entry["anchors"] and all(r["sha256"] for r in entry["anchors"])
        path = migrated / entry["contentUrl"]
        path.write_text(path.read_text() + "\nA hand-added sentence.\n")
        findings = validate_v2(migrated)
        assert is_valid(findings)
        assert "edited" in codes(findings, "warning")

    def test_unrecorded_and_duplicate_headings_are_errors(self, migrated: Path):
        v2_anchors.stamp(
            migrated,
            "expertise",
            method="inferred",
            generator="builder@2.1",
            generated_at="2026-11-13T09:00:00Z",
        )
        page = json.loads((migrated / "profile.jsonld").read_text())
        path = migrated / next(e for e in page["hasPart"] if e["role"] == "expertise")["contentUrl"]
        first = slug.scan(path.read_text()).sections[0].heading
        path.write_text(path.read_text() + f"\n## New section\nx\n\n## {first}\ny\n")
        assert {"anchor-unrecorded", "anchor-duplicate"} <= codes(validate_v2(migrated))

    def test_served_copy_accepts_reduced_entries(self, migrated: Path):
        def f(p):
            e = next(x for x in p["hasPart"] if x["role"] == "soul")
            for k in list(e):
                if k not in ("role", "visibility"):
                    e.pop(k)

        edit_page(migrated, f)
        assert "contentUrl" in codes(validate_v2(migrated))
        assert is_valid(validate_v2(migrated, served=True))


V2_CASES = REPO / "spec" / "conformance" / "v2"


@pytest.mark.parametrize(
    "case", json.loads((V2_CASES / "cases.json").read_text())["cases"], ids=lambda c: c["dir"]
)
def test_v2_conformance_case(case):
    findings = validate_v2(V2_CASES / case["dir"])
    assert is_valid(findings) == case["valid"], [f for f in findings if f.level == "error"]
    assert set(case.get("errors", [])) <= codes(findings)
    assert set(case.get("warnings", [])) <= codes(findings, "warning")
