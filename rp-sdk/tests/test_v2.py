"""v2 draft format: sections, the provenance file, migration from v1, and the v2 validator (#4-#7)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from researcher_profiles.v2 import V2_CONTEXT, migrate_profile, provenance, sections, validate_v2
from researcher_profiles.v2 import paperid as v2_paperid
from researcher_profiles.v2.common import (
    canonical_json,
    content_key,
    entry_key,
    normalize_datetime,
    value_sha256,
)
from researcher_profiles.v2.migrate import MigrationError
from researcher_profiles.v2.stamp import cited_papers, stamp_sections
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


@pytest.fixture
def migrated_rich(tmp_path: Path) -> Path:
    """The fixture with v1 interests (one mapped, one unmapped) and critiques added."""
    src = tmp_path / "src"
    shutil.copytree(V1_FIXTURE, src)
    doc = json.loads((src / "profile.jsonld").read_text())
    doc["rp:researchInterests"] = [
        {
            "@type": "ResearchInterest",
            "concept": {
                "@id": "https://openalex.org/T10001",
                "system": "openalex",
                "display": "Computing",
            },
            "method": "inferred",
            "generator": "openalex-topics@2026-10",
            "assertedAt": "2026-10-08T16:24:56.123+00:00",
            "evidence": {"papers": [], "share": 0.4},
            "weight": 0.4,
        },
        {
            "@type": "ResearchInterest",
            "concept": {"label": "Analytical engines", "unmapped": True},
            "method": "inferred",
            "generator": "llm",
            "assertedAt": "2026-10-08T16:24:56Z",
            "weight": 0.5,
        },
    ]
    doc["critiques"] = ["Machines originate nothing.", "Tables need checking."]
    (src / "profile.jsonld").write_text(json.dumps(doc, indent=2))
    out = tmp_path / "rich"
    migrate_profile(src, out, page_id=FIXED_ID)
    return out


def edit_page(root: Path, fn) -> None:
    page = json.loads((root / "profile.jsonld").read_text())
    fn(page)
    (root / "profile.jsonld").write_text(json.dumps(page, indent=2))


def edit_prov(root: Path, fn) -> None:
    prov = provenance.load(root)
    fn(prov)
    provenance.save(root, prov)


def page_of(root: Path) -> dict:
    return json.loads((root / "profile.jsonld").read_text())


class TestSections:
    def test_heading_text_is_the_key(self):
        text = "## Intellectual lineage\nx\n## Methods *championed* ##\ny\n"
        assert [s.heading for s in sections.scan(text).sections] == [
            "Intellectual lineage",
            "Methods *championed*",
        ]

    def test_headings_in_code_fences_and_setext_do_not_count(self):
        text = "Intro\n\n## Methods\n\n```\n## Not a heading\n```\n\nSetext\n------\n\n## Career\nend\n"
        assert [s.heading for s in sections.scan(text).sections] == ["Methods", "Career"]

    def test_duplicates_and_empty_are_reported(self):
        s = sections.scan("## Methods\na\n## Methods\nb\n##  \nc\n")
        assert s.duplicates == ["Methods"]
        assert s.empty == 1

    def test_section_hash_ignores_crlf_and_covers_heading_to_next_heading(self):
        a = sections.scan("pre\n## A\nx\n## B\ny\n").sections
        b = sections.scan("pre\r\n## A\r\nx\r\n## B\r\ny\r\n").sections
        assert [s.sha256 for s in a] == [s.sha256 for s in b]
        changed = sections.scan("pre\n## A\nx!\n## B\ny\n").sections
        assert a[0].sha256 != changed[0].sha256
        assert a[1].sha256 == changed[1].sha256


class TestCitations:
    def test_only_tokens_matching_a_paper_id_cite(self):
        ids = {"smith2020a", "jones2019b"}
        text = "Claim [smith2020a]. Two [jones2019b, smith2020a]. Not [sic] or [1] or [a link](http://x)."
        assert cited_papers(text, ids) == ["smith2020a", "jones2019b"]


class TestCanonicalJson:
    @pytest.mark.parametrize(
        ("value", "out"),
        [
            ({"b": 1, "a": [True, None, "é"]}, '{"a":[true,null,"é"],"b":1}'),
            (1.0, "1"),
            (0.5, "0.5"),
            (1e21, "1e+21"),
            (1e20, "100000000000000000000"),
            (1e-7, "1e-7"),
            (0.000001, "0.000001"),
            (-0.25, "-0.25"),
            ({"\u20ac": 1, "\r": 2}, '{"\\r":2,"\u20ac":1}'),
        ],
    )
    def test_rfc8785(self, value, out):
        assert canonical_json(value).decode() == out

    def test_keys(self):
        assert len(content_key("x")) == 16
        assert content_key("x") == value_sha256("x")[:16]
        mapped = {"concept": {"@id": "https://openalex.org/T1"}, "weight": 0.5}
        unmapped = {"concept": {"label": "Cracked pots", "unmapped": True}, "weight": 0.5}
        assert entry_key("rp:researchInterests", mapped) == "https://openalex.org/T1"
        assert entry_key("rp:researchInterests", unmapped) == content_key(unmapped["concept"])
        assert entry_key("critiques", "a claim") == content_key("a claim")


class TestProvenanceTree:
    def test_inheritance_and_explicit_null(self):
        root = provenance.new_root(
            FIXED_ID, method="inferred", generator="b@1", generatedAt="2026-01-01T00:00:00Z"
        )
        provenance.ensure(root, ("files", "expertise")).update({"generator": None})
        provenance.ensure(root, ("files", "expertise", "Methods")).update({"method": "declared"})
        path = ("files", "expertise", "Methods")
        assert provenance.effective(root, path, "method") == "declared"
        assert provenance.effective(root, path, "generator") is None
        assert provenance.effective(root, path, "generatedAt") == "2026-01-01T00:00:00Z"
        assert provenance.effective(root, ("papers", "x", "summary"), "method") == "inferred"

    def test_prune_keeps_the_file_sparse(self):
        root = provenance.new_root(FIXED_ID)
        provenance.ensure(root, ("profile", "critiques", "abc"))
        provenance.prune(root)
        assert root["parts"] == {}


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
        page = page_of(migrated)
        assert page["@type"] == "ProfilePage"
        assert page["@id"] == FIXED_ID
        assert page["@context"] == V2_CONTEXT
        assert page["mainEntity"]["@type"] == "Person"
        assert "subjectOf" not in page and "provenance" not in page and "partDefaults" not in page
        assert any(e["role"] == "provenance" for e in page["hasPart"])

    def test_provenance_root_is_null_and_names_the_page(self, migrated: Path):
        prov = provenance.load(migrated)
        assert prov["about"] == FIXED_ID
        assert (prov["method"], prov["generator"], prov["generatedAt"]) == (None, None, None)

    def test_interest_provenance_moves_to_records(self, migrated_rich: Path):
        assert is_valid(validate_v2(migrated_rich))
        interests = page_of(migrated_rich)["mainEntity"]["rp:researchInterests"]
        prov = provenance.load(migrated_rich)
        recs = []
        for it in interests:
            assert not {
                "method",
                "generator",
                "generatedAt",
                "assertedAt",
                "derivedFrom",
                "evidence",
            } & set(it)
            key = entry_key("rp:researchInterests", it)
            recs.append(provenance.get(prov, ("profile", "rp:researchInterests", key)))
        mapped, unmapped = recs
        assert interests[0]["paperShare"] == 0.4
        assert mapped["method"] == "imported" and mapped["generator"] == "openalex-topics@2026-10"
        assert mapped["generatedAt"] == "2026-10-08T16:24:56Z"
        assert mapped["sha256"] == value_sha256(interests[0])
        assert unmapped["method"] == "inferred" and unmapped["generator"] is None  # bare `llm`

    def test_text_fields_are_plain_strings(self, migrated: Path):
        person = page_of(migrated)["mainEntity"]
        assert isinstance(person.get("summary", ""), str)
        for key in ("expertise", "critiques"):
            assert all(isinstance(x, str) for x in person.get(key) or [])

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
        prov = provenance.load(migrated)
        text = json.dumps(prov)
        assert '"generator": "llm"' not in text
        for _, rec in provenance.walk(prov):
            if rec.get("method") == "declared":
                pytest.fail("migration wrote a declared record")

    def test_v1_profile_fails_v2_validation(self):
        assert not is_valid(validate_v2(V1_FIXTURE))


class TestValidate:
    def test_page_needs_uuid_and_date(self, migrated: Path):
        edit_page(
            migrated, lambda p: (p.update({"@id": "https://example.org/me"}), p.pop("dateModified"))
        )
        assert {"page-id", "dateModified"} <= codes(validate_v2(migrated))

    def test_inline_provenance_is_rejected(self, migrated: Path):
        def f(p):
            next(x for x in p["hasPart"] if x["role"] == "expertise")["method"] = "inferred"
            p["mainEntity"]["summary"] = {"text": "x", "method": "inferred"}

        edit_page(migrated, f)
        assert {"inline-provenance", "summary"} <= codes(validate_v2(migrated))

    def test_record_method_needs_sha256_even_when_inherited(self, migrated: Path):
        edit_prov(migrated, lambda pr: pr.update({"method": "inferred"}))
        edit_prov(
            migrated,
            lambda pr: provenance.ensure(pr, ("files", "soul")).update({"generator": "b@1"}),
        )
        assert "sha256-required" in codes(validate_v2(migrated))

    def test_content_keyed_entries_and_grouping_records_need_no_hash(self, migrated_rich: Path):
        crit = page_of(migrated_rich)["mainEntity"]["critiques"][0]
        edit_prov(migrated_rich, lambda pr: pr.update({"method": "inferred"}))
        edit_prov(
            migrated_rich,
            lambda pr: provenance.ensure(pr, ("profile", "critiques", content_key(crit))).update(
                {"method": "declared", "generator": "user"}
            ),
        )
        assert is_valid(validate_v2(migrated_rich))

    def test_user_must_be_declared_and_generator_form(self, migrated: Path):
        summary = page_of(migrated)["mainEntity"]["summary"]
        edit_prov(
            migrated,
            lambda pr: (
                provenance.ensure(pr, ("profile", "summary")).update(
                    {"generator": "user", "method": "inferred", "sha256": value_sha256(summary)}
                ),
                pr.update({"generator": "llm"}),
            ),
        )
        assert {"user-not-declared", "generator"} <= codes(validate_v2(migrated))

    def test_edits_shape(self, migrated: Path):
        edit_prov(
            migrated,
            lambda pr: provenance.ensure(pr, ("files", "soul")).update(
                {"edits": [{"by": "llm", "at": "2026-01-01T00:00:00Z"}, {"who": "x"}]}
            ),
        )
        assert {"generator", "edit"} <= codes(validate_v2(migrated))

    def test_datetime_form(self, migrated: Path):
        edit_page(migrated, lambda p: p.update({"dateModified": "2026-10-17T00:33:11+00:00"}))
        edit_prov(migrated, lambda pr: pr.update({"generatedAt": "2026-10-17"}))
        found = [f for f in validate_v2(migrated) if f.code == "datetime"]
        assert {f.where.split(".")[0] for f in found} >= {"", "provenance"}

    def test_person_id_must_be_orcid_of_rid(self, migrated: Path):
        edit_page(migrated, lambda p: p["mainEntity"].update({"@id": "https://example.org/ada"}))
        assert "person-id" in codes(validate_v2(migrated))

    def test_cache_part_and_v1_keys_rejected(self, migrated: Path):
        def f(p):
            p["hasPart"].append({"role": "embedding_index_sqlite", "contentUrl": "profile.jsonld"})
            p["provenance"] = "third_party"

        edit_page(migrated, f)
        assert {"cache-part", "v1-key"} <= codes(validate_v2(migrated))

    def test_excluded_shape(self, migrated: Path):
        edit_page(migrated, lambda p: p.update({"excluded": [{"type": "person", "id": "x"}]}))
        assert "excluded" in codes(validate_v2(migrated))

    def test_inherits_visibility_only_on_file_records(self, migrated: Path):
        summary = page_of(migrated)["mainEntity"]["summary"]
        edit_prov(
            migrated,
            lambda pr: provenance.ensure(pr, ("profile", "summary")).update(
                {
                    "sha256": value_sha256(summary),
                    "derivedFrom": [{"file": "works", "inheritsVisibility": True}],
                }
            ),
        )
        assert "inherits-placement" in codes(validate_v2(migrated))

    def test_unresolved_paper_reference(self, migrated: Path):
        edit_prov(
            migrated,
            lambda pr: provenance.ensure(pr, ("files", "soul")).update(
                {"derivedFrom": [{"paper": "nobody1999none"}]}
            ),
        )
        assert "ref-paper" in codes(validate_v2(migrated))

    def test_record_about_and_unknown_keys(self, migrated: Path):
        edit_prov(
            migrated,
            lambda pr: pr.update(
                {"about": "urn:uuid:00000000-0000-0000-0000-000000000000", "note": "x"}
            ),
        )
        assert {"about", "record-key"} <= codes(validate_v2(migrated))

    def test_missing_provenance_file(self, migrated: Path):
        edit_page(
            migrated,
            lambda p: p.update({"hasPart": [e for e in p["hasPart"] if e["role"] != "provenance"]}),
        )
        assert "no-provenance-file" in codes(validate_v2(migrated))


class TestStamp:
    def test_stamped_sections_validate_and_an_edit_is_a_warning_not_an_error(self, migrated: Path):
        stamp_sections(
            migrated,
            "expertise",
            method="inferred",
            generator="builder@2.1/claude-opus-5-5",
            generated_at="2026-11-13T09:00:00Z",
        )
        assert is_valid(validate_v2(migrated))
        rec = provenance.get(provenance.load(migrated), ("files", "expertise"))
        assert rec["parts"] and all(r["sha256"] for r in rec["parts"].values())
        entry = next(e for e in page_of(migrated)["hasPart"] if e["role"] == "expertise")
        path = migrated / entry["contentUrl"]
        path.write_text(path.read_text() + "\nA hand-added sentence.\n")
        findings = validate_v2(migrated)
        assert is_valid(findings)
        assert "edited" in codes(findings, "warning")

    def test_renamed_heading_leaves_a_record_and_duplicates_are_errors(self, migrated: Path):
        stamp_sections(
            migrated,
            "expertise",
            method="inferred",
            generator="builder@2.1",
            generated_at="2026-11-13T09:00:00Z",
        )
        entry = next(e for e in page_of(migrated)["hasPart"] if e["role"] == "expertise")
        path = migrated / entry["contentUrl"]
        text = path.read_text()
        first = sections.scan(text).sections[0].heading
        path.write_text(text.replace(f"## {first}", "## Renamed", 1) + "\n## Renamed\ny\n")
        findings = validate_v2(migrated)
        assert "section-duplicate" in codes(findings)
        assert "leftover-record" in codes(findings, "warning")

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


class TestPaperId:
    def test_usual_id_skips_stopwords_and_abstract_numbers(self):
        assert v2_paperid.candidates("Díez", 2021, "The Colon Transcriptome Explorer")[0] == (
            "diez2021colon"
        )
        assert v2_paperid.candidates("Taha", 2025, "Abstract A065: Community engagement")[0] == (
            "taha2025community"
        )

    def test_collision_adds_title_words_and_keeps_the_short_id(self):
        taken = {"doe2020whole"}
        pid = v2_paperid.mint_paper_id("Doe", 2020, "Whole proteome GPS predictions", taken)
        assert pid == "doe2020whole-proteome"
        taken.add(pid)
        assert (
            v2_paperid.mint_paper_id("Doe", 2020, "Whole proteome NetworKIN predictions", taken)
            == "doe2020whole-proteome-networkin"
        )

    def test_same_title_is_one_work(self):
        with pytest.raises(v2_paperid.SameWork):
            v2_paperid.mint_paper_id(
                "Doe", 2020, "New pipeline", {"doe2020new", "doe2020new-pipeline"}
            )

    def test_assignment_keeps_existing_ids_and_does_not_depend_on_input_order(self):
        def recs():
            return [
                {"paperId": "doe2020atlas", "name": "Atlas of example cells", "_surname": "Doe"},
                {"name": "Atlas of proteome sites", "datePublished": "2020", "_surname": "Doe"},
                {"name": "Atlas of chromatin states", "datePublished": "2020", "_surname": "Doe"},
                {
                    "name": "Atlas of example cells",
                    "datePublished": "2020",
                    "_surname": "Doe",
                    "@id": "https://doi.org/10.1101/x",
                },
            ]

        a = recs()
        same_a = v2_paperid.assign_paper_ids(a)
        b = list(reversed(recs()))
        v2_paperid.assign_paper_ids(b)
        assert a[0]["paperId"] == "doe2020atlas"
        assert {r.get("paperId") for r in a} == {r.get("paperId") for r in b}
        assert {a[1]["paperId"], a[2]["paperId"]} == {
            "doe2020atlas-proteome",
            "doe2020atlas-chromatin",
        }
        assert same_a == [(3, 0)] and "paperId" not in a[3]
