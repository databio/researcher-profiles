"""The validator and the shared conformance corpus it must agree with.

``validate_profile_dir`` is one of two implementations of the same verdict;
the other is rp-browser's ``runValidation``. The corpus at the bottom of
this file is what keeps them from drifting apart.
"""

import json
from pathlib import Path

import pytest
import yaml

from researcher_profiles.schema import SummaryFile
from researcher_profiles.validate import (
    RETIRED_TERMS,
    known_terms,
    report_to_dict,
    retired_terms,
    schema_fingerprint,
    undeclared_terms,
    validate_artifact,
    validate_profile_dir,
)

from .factories import CONTEXT_IRI, FIXTURE_DIR, MONOREPO_ROOT, write_raw_profile

# --------------------------------------------------------------------------
# The validator
# --------------------------------------------------------------------------


#: Read-only: this file asserts that the committed fixture bytes still validate.
JANE_DOE = FIXTURE_DIR / "jane-doe"

#: The boilerplate of a conforming profile document, as raw JSON. Deviate in
#: exactly one visible way: ``{**RAW_MINIMAL, "conformsTo": "...wrong"}``.
RAW_MINIMAL = {
    "@context": CONTEXT_IRI,
    "@type": "Person",
    "conformsTo": CONTEXT_IRI,
    "name": "Test Person",
    "rid": "0000-0002-1825-0097",
    "provenance": "third_party",
}


def test_validate_conforming_profile():
    report = validate_profile_dir(JANE_DOE)
    assert report.ok is True


@pytest.mark.parametrize(
    "relpath, kind",
    [
        ("profile.jsonld", "profile_jsonld"),
        ("sources/papers.jsonld", "papers_jsonld"),
    ],
    ids=["profile_jsonld", "papers_jsonld"],
)
def test_validate_artifact_accepts_the_conforming_fixture(relpath: str, kind: str):
    result = validate_artifact(JANE_DOE / relpath, kind)
    assert result.ok is True
    assert result.exists is True
    assert result.parsed is True
    assert result.violations == []


def test_validate_artifact_missing_file():
    result = validate_artifact("/nonexistent/path/profile.jsonld", "profile_jsonld")
    assert result.exists is False
    assert result.ok is False
    assert len(result.violations) >= 1


def test_validate_artifact_parse_error(tmp_path: Path):
    bad = tmp_path / "profile.jsonld"
    bad.write_text("{not valid json!!!", encoding="utf-8")
    result = validate_artifact(bad, "profile_jsonld")
    assert result.exists is True
    assert result.parsed is False
    assert result.ok is False
    assert any(v.keyword == "parse" for v in result.violations)


def _validate_doc(tmp_path: Path, doc: dict):
    """Write ``doc`` as a raw ``profile.jsonld`` and validate it.

    The deviation under test stays visible in the caller; only the
    write-then-validate boilerplate lives here.
    """
    return validate_artifact(write_raw_profile(tmp_path, doc), "profile_jsonld")


def test_validate_artifact_missing_required_field(tmp_path: Path):
    doc = {k: v for k, v in RAW_MINIMAL.items() if k != "name"}  # no ``name``
    result = _validate_doc(tmp_path, doc)
    assert result.ok is False
    assert any(v.keyword == "missing" and "name" in v.json_pointer for v in result.violations)


def test_validate_artifact_wrong_conformsTo(tmp_path: Path):
    doc = {**RAW_MINIMAL, "conformsTo": "https://example.com/wrong"}
    result = _validate_doc(tmp_path, doc)
    assert result.ok is False
    assert any(
        "conformsTo" in v.json_pointer or "conformsTo" in v.message for v in result.violations
    )


# --------------------------------------------------------------------------
# Retired-key detection
# --------------------------------------------------------------------------


def test_retired_terms_flags_the_old_key():
    doc = {**RAW_MINIMAL, "artifacts": [{"type": "Software", "name": "some-tool"}]}
    hits = retired_terms(doc)
    assert {h.term for h in hits} == {"artifacts"}
    assert hits[0].json_pointer == "/artifacts"


def test_retired_terms_ignores_the_replacement_key():
    doc = {**RAW_MINIMAL, "researchOutputs": [{"type": "Software", "name": "some-tool"}]}
    assert retired_terms(doc) == []


def test_validate_artifact_fails_on_a_retired_key(tmp_path: Path):
    """The retired key is a distinct, actionable failure, not a silent pass.

    ``artifacts`` is still a known term overall (``ProfileCollection`` has its
    own field of that name), so without this check a document using it would
    validate clean.
    """
    doc = {**RAW_MINIMAL, "artifacts": [{"type": "Software", "name": "some-tool"}]}
    result = _validate_doc(tmp_path, doc)
    assert result.ok is False
    retired = [v for v in result.violations if v.keyword == "retired_term"]
    assert len(retired) == 1
    assert retired[0].found == "artifacts"
    assert retired[0].expected == RETIRED_TERMS["artifacts"]
    assert "researchOutputs" in retired[0].message


def test_validate_artifact_passes_with_the_replacement_key(tmp_path: Path):
    doc = {**RAW_MINIMAL, "researchOutputs": [{"type": "Software", "name": "some-tool"}]}
    result = _validate_doc(tmp_path, doc)
    assert result.ok is True
    assert not any(v.keyword == "retired_term" for v in result.violations)


@pytest.mark.parametrize(
    "term, origin",
    [
        ("name", "model field"),
        ("rid", "model field"),
        ("conformsTo", "model field"),
        ("hasPart", "model field"),
        ("sameAs", "context term"),
        ("datePublished", "context term"),
    ],
    ids=[
        "name-model-field",
        "rid-model-field",
        "conformsTo-model-field",
        "hasPart-model-field",
        "sameAs-context-term",
        "datePublished-context-term",
    ],
)
def test_known_terms_includes(term: str, origin: str):
    terms = known_terms("profile_jsonld")
    assert term in terms, f"{term!r} should be a known {origin}"


def test_known_terms_is_scoped_per_schema():
    """A field on one schema does not license the same word on another.

    ``ProfileCollection`` (schema name ``collection``) has its own
    ``artifacts`` field. That must not make ``artifacts`` a known term for
    ``profile_jsonld``, which is a different document with no such field
    (its equivalent field is ``researchOutputs``). This is the mechanism
    behind the ``artifacts`` -> ``researchOutputs`` rename going undetected.
    """
    assert "artifacts" in known_terms("collection")
    assert "artifacts" not in known_terms("profile_jsonld")
    assert "researchOutputs" in known_terms("profile_jsonld")


@pytest.mark.parametrize(
    "extra_fields, expected",
    [
        ({"bogus_field": "surprise"}, {"bogus_field"}),
        ({"rid": "0000-0002-1825-0097"}, set()),
    ],
    ids=["unknown-field-reported", "known-field-ignored"],
)
def test_undeclared_terms_reports_exactly_the_unknown_fields(extra_fields: dict, expected: set):
    doc = {
        "@context": "https://profiles.databio.org/context/v1.jsonld",
        "name": "Test",
        **extra_fields,
    }
    result = undeclared_terms(doc, "profile_jsonld")
    assert {t.term for t in result} == expected


def test_schema_fingerprint_stable():
    fp1 = schema_fingerprint()
    fp2 = schema_fingerprint()
    assert fp1 == fp2
    assert isinstance(fp1, str)
    assert len(fp1) == 64  # SHA-256 hex


def test_violations_are_deterministic(tmp_path: Path):
    doc = {
        "@context": CONTEXT_IRI,
        "@type": "Person",
        "conformsTo": "https://example.com/wrong",
        # missing name, rid, provenance
    }

    results = [_validate_doc(tmp_path, doc) for _ in range(20)]
    # Serialize violations to JSON for byte-identical comparison
    serialized = [
        json.dumps(
            [
                (v.json_pointer, v.keyword, v.message, v.found, v.expected, v.fix)
                for v in r.violations
            ],
            sort_keys=True,
        )
        for r in results
    ]
    assert all(s == serialized[0] for s in serialized)


def test_found_truncation_cap(tmp_path: Path):
    doc = {
        **RAW_MINIMAL,
        "name": 12345,  # wrong type: expects str, gets int, so found will be "12345"
        "level": "A" * 200,  # wrong literal value, triggers an error with a long found
    }
    result = _validate_doc(tmp_path, doc)
    assert result.ok is False
    for v in result.violations:
        assert len(v.found) <= 120


def test_report_to_dict_roundtrips():
    report = validate_profile_dir(JANE_DOE)
    d = report_to_dict(report)
    assert isinstance(d, dict)
    assert d["ok"] is True
    assert "artifacts" in d
    assert "cross_artifact" in d
    # Should be JSON-serializable
    json.dumps(d)


def test_validate_profile_dir_cross_artifact_manifest_drift(tmp_path: Path):
    """A manifest entry pointing to a nonexistent file triggers a cross_artifact violation."""
    profile_dir = tmp_path / "profile"
    (profile_dir / "sources").mkdir(parents=True)

    # A hasPart entry pointing at a file that is not there: the drift itself.
    write_raw_profile(
        profile_dir,
        {
            **RAW_MINIMAL,
            "hasPart": [
                {
                    "@type": "DigitalDocument",
                    "name": "Ghost file",
                    "role": "works",
                    "encodingFormat": "application/ld+json",
                    "contentUrl": "sources/ghost.jsonld",
                }
            ],
        },
    )

    # A minimal papers.jsonld so that artifact itself validates.
    papers_doc = {
        "@context": CONTEXT_IRI,
        "@type": "Collection",
        "conformsTo": CONTEXT_IRI,
        "about": {"@id": "https://orcid.org/0000-0002-1825-0097"},
        "hasPart": [],
    }
    (profile_dir / "sources" / "papers.jsonld").write_text(json.dumps(papers_doc), encoding="utf-8")

    report = validate_profile_dir(profile_dir)
    # The ghost contentUrl should produce a cross_artifact violation
    stale_violations = [
        v for v in report.cross_artifact if v.keyword in ("manifest_drift", "content_url_missing")
    ]
    assert len(stale_violations) >= 1, (
        f"Expected a cross-artifact violation for the missing file, got: {report.cross_artifact}"
    )


def test_validate_profile_dir_flags_legacy_cache_dirname(tmp_path: Path):
    """A manifest entry under the pre-rename ``cache/`` dir is not "no file".

    ``build_manifest`` only looks under ``.cache/`` (``CACHE_DIRNAME``). A
    manifest recorded before that rename still names the file under the old,
    un-dotted ``cache/`` directory. The file is right there on disk, so this
    must surface as its own named condition, not the generic "no file"
    ``manifest_drift`` message.
    """
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir(parents=True)

    write_raw_profile(
        profile_dir,
        {
            **RAW_MINIMAL,
            "hasPart": [
                {
                    "@type": "DataDownload",
                    "name": "Embedding index (local sqlite)",
                    "role": "embedding_index_sqlite",
                    "encodingFormat": "application/vnd.sqlite3",
                    "contentUrl": "cache/embeddings.sqlite",
                }
            ],
        },
    )

    # The file really is on disk, just under the retired directory name.
    legacy_cache = profile_dir / "cache"
    legacy_cache.mkdir(parents=True)
    (legacy_cache / "embeddings.sqlite").write_bytes(b"not a real sqlite file")

    report = validate_profile_dir(profile_dir)

    legacy = [v for v in report.cross_artifact if v.keyword == "legacy_cache_dirname"]
    assert len(legacy) == 1, (
        f"Expected one legacy_cache_dirname violation, got: {report.cross_artifact}"
    )
    assert "cache/embeddings.sqlite" in legacy[0].message

    # Not double-reported under the generic drift message.
    generic_stale = [
        v for v in report.cross_artifact if v.keyword == "manifest_drift" and "no file" in v.message
    ]
    assert generic_stale == [], f"Should not also appear as generic drift: {generic_stale}"


@pytest.mark.parametrize(
    "raw_written_at, parses_as_str, expected",
    [
        ("2026-08-04T20:54:57Z", False, "2026-08-04T20:54:57+00:00"),
        ('"2026-08-04T20:54:57Z"', True, "2026-08-04T20:54:57Z"),
    ],
    ids=["unquoted_yaml_datetime_is_coerced", "quoted_string_passes_through"],
)
def test_summary_file_coerces_datetime_written_at(raw_written_at, parses_as_str, expected):
    """An unquoted ISO-8601 timestamp in YAML frontmatter parses as a
    datetime object; SummaryFile must coerce it back to a string. A quoted
    one is already a string and passes through untouched."""
    fm = yaml.safe_load(
        "paper_id: paper2020a\n"
        "source_kind: fulltext\n"
        "source_hash: abc123\n"
        f"written_at: {raw_written_at}\n"
    )
    assert isinstance(fm["written_at"], str) is parses_as_str
    sf = SummaryFile.model_validate(fm)
    assert sf.written_at == expected


# --------------------------------------------------------------------------
# The shared conformance corpus
# --------------------------------------------------------------------------


CORPUS = MONOREPO_ROOT / "spec" / "conformance"


CASES = json.loads((CORPUS / "cases.json").read_text(encoding="utf-8"))["cases"]


def _case_id(case: dict) -> str:
    return case["dir"]


class TestConformanceCorpus:
    """Run the CLI validator over the shared conformance corpus.

    The corpus (``spec/conformance/``) is the single set of fixtures that both
    validators, this Python CLI (:func:`validate_profile_dir`) and the explorer's
    ``runValidation`` (see ``rp-browser/tests/conformance.test.ts``), run over.
    Each case's expected verdict lives in ``cases.json``. This test asserts the CLI
    half; the vitest harness asserts the explorer half against the same fixtures.

    Together they make validator divergence impossible to ship unnoticed: a change
    that makes one validator accept something the corpus says is invalid (or reject
    something it says is valid) fails CI.
    """

    @pytest.mark.parametrize("case", CASES, ids=[_case_id(c) for c in CASES])
    def test_cli_verdict_matches_corpus(self, case: dict) -> None:
        report = validate_profile_dir(CORPUS / case["dir"])
        expected_valid = case["cli"]["valid"]

        assert report.ok is expected_valid, (
            f"{case['dir']}: CLI verdict {report.ok} != expected {expected_valid}. "
            f"Violations: {[(a.schema_name, v.keyword, v.message) for a in report.artifacts for v in a.violations]}"
        )

        # When the case names the check(s) that must fail, confirm at least one
        # shows up. Keywords are matched against Pydantic error types and schema
        # names so the hint does not depend on message wording.
        expect_fail = case["cli"].get("expect_fail", [])
        if expect_fail:
            seen = {
                token
                for a in report.artifacts
                for v in a.violations
                for token in (v.keyword, a.schema_name, v.json_pointer)
            }
            assert any(any(hint in token for token in seen) for hint in expect_fail), (
                f"{case['dir']}: none of {expect_fail} found in {sorted(seen)}"
            )

    def test_corpus_has_valid_and_invalid_cases(self) -> None:
        """Guard against an empty or one-sided corpus silently passing."""
        assert any(c["cli"]["valid"] for c in CASES), "corpus has no valid CLI case"
        assert any(not c["cli"]["valid"] for c in CASES), "corpus has no invalid CLI case"
