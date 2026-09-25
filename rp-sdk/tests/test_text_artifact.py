"""The "no garbage characters" rule for paper full text and summaries.

The garbled fixtures in ``fixtures/text_artifacts/`` are real: bioRxiv PDFs
that a download decoded as text and saved as ``sources/papers/*.md`` in a
published profile.
"""

from pathlib import Path

import pytest

from researcher_profiles.text_artifact import text_artifact_problems
from researcher_profiles.validate import validate_profile_dir

from .factories import FIXTURE_DIR

GARBLED_DIR = FIXTURE_DIR / "text_artifacts"
GARBLED = sorted(GARBLED_DIR.glob("*.md"))

#: Legitimate scientific text: every one of these must pass.
CLEAN_SAMPLES = {
    "greek_and_math": "The α-helix and β-sheet; ∑ᵢ xᵢ ≤ 10⁻³ and ∫ f(x) dx ≈ π/2 ± 0.1 μM.",
    "accented_names": "Schrödinger, Müller, Gómez-Pérez, Łukasz, Dvořák, and Ångström.",
    "typography": "“Curly quotes,” ‘single ones,’ an em-dash — and an en-dash 1–2 … done.",
    "cjk_and_units": "Genes: 基因. Temperature 37 °C, ratio 3:1, 5 × 10⁶ cells.",
    "whitespace": "Line one\r\nLine two\n\tIndented with a tab.\n",
    # Real PDF extraction leaves a few stray control characters (math-font
    # brackets, page-break form feeds) in long papers; a trace is not garbage.
    "sparse_extraction_artifacts": ("Real prose about chromatin accessibility. " * 200)
    + "\x0c\x12(a+b)\x13\n",
    # One unmappable glyph in a long paper is not garbage either.
    "one_replacement_char": ("Real prose about gene regulation. " * 200) + "�",
    "mentions_pdf_words_in_prose": (
        "We parsed each file's trailer and every endobj token; the stream was then decoded."
    ),
}


@pytest.mark.parametrize("name", sorted(CLEAN_SAMPLES))
def test_clean_scientific_text_passes(name: str) -> None:
    text = CLEAN_SAMPLES[name]
    assert text_artifact_problems(text) == []
    assert text_artifact_problems(text.encode("utf-8")) == []


@pytest.mark.parametrize("path", GARBLED, ids=[p.name for p in GARBLED])
def test_real_garbled_downloads_fail(path: Path) -> None:
    problems = text_artifact_problems(path.read_bytes())
    assert any("U+FFFD" in p for p in problems), problems


def test_fixtures_are_present() -> None:
    assert len(GARBLED) >= 3


def test_invalid_utf8_fails() -> None:
    problems = text_artifact_problems(b"# Title\n\nFine text then \xff\xfe\xc3 bad bytes.")
    assert any("not valid UTF-8" in p for p in problems)


def test_any_nul_fails() -> None:
    long_text = "Real prose about gene regulation. " * 200
    problems = text_artifact_problems(long_text + "\x00")
    assert any("NUL" in p for p in problems)


def test_dense_control_characters_fail() -> None:
    problems = text_artifact_problems("abc\x01\x02\x03def\x1b\x7f" * 20)
    assert any("control characters" in p for p in problems)


def test_pdf_header_fails() -> None:
    problems = text_artifact_problems("%PDF-1.5\n%âãÏÓ\n1 0 obj\n<<>>\nendobj\n")
    assert any("%PDF-" in p for p in problems)


def test_pdf_structure_without_header_fails() -> None:
    body = "Some text\n12 0 obj\n<< /Length 5 >>\nstream\nxhello\nendstream\nendobj\n"
    problems = text_artifact_problems(body)
    assert any("PDF file structure" in p for p in problems)


def test_min_chars_is_opt_in() -> None:
    assert text_artifact_problems("# Title\n\nShort abstract.") == []
    problems = text_artifact_problems("# Title\n\nShort abstract.", min_chars=2000)
    assert any("too short" in p for p in problems)


# --------------------------------------------------------------------------
# rp validate: the rule fails the profile, naming the file and the reason
# --------------------------------------------------------------------------


def _text_failures(report) -> dict[str, list[str]]:
    return {
        Path(a.path).name: [v.message for v in a.violations]
        for a in report.artifacts
        if any(v.keyword == "text_content" for v in a.violations)
    }


def test_validate_clean_fixture_has_no_text_failures(jane_doe_dir: Path) -> None:
    report = validate_profile_dir(jane_doe_dir)
    assert _text_failures(report) == {}
    assert report.ok


def test_validate_fails_garbled_fulltext(jane_doe_dir: Path) -> None:
    target = jane_doe_dir / "sources" / "papers" / "doe2016example.md"
    target.write_bytes((GARBLED_DIR / "gharavi2021embeddings.md").read_bytes())

    report = validate_profile_dir(jane_doe_dir)

    assert not report.ok
    failures = _text_failures(report)
    assert list(failures) == ["doe2016example.md"]
    assert any("U+FFFD" in m for m in failures["doe2016example.md"])
    bad = next(a for a in report.artifacts if a.path.endswith("doe2016example.md"))
    assert bad.schema_name == "paper_fulltext"


def test_validate_fails_garbled_summary(jane_doe_dir: Path) -> None:
    target = jane_doe_dir / "sources" / "summaries" / "doe2016example.summary.md"
    target.write_bytes(target.read_bytes() + b"\n%PDF-1.5 \xef\xbf\xbd\x08\x1a" * 200)

    report = validate_profile_dir(jane_doe_dir)

    assert not report.ok
    failures = _text_failures(report)
    assert list(failures) == ["doe2016example.summary.md"]
    bad = next(a for a in report.artifacts if a.path.endswith("doe2016example.summary.md"))
    assert bad.schema_name == "paper_summary"


def test_validate_fails_invalid_utf8_summary_without_crashing(jane_doe_dir: Path) -> None:
    target = jane_doe_dir / "sources" / "summaries" / "doe2016example.summary.md"
    target.write_bytes(target.read_bytes() + b"\n\xff\xfe broken")

    report = validate_profile_dir(jane_doe_dir)

    failures = _text_failures(report)
    assert any("not valid UTF-8" in m for m in failures["doe2016example.summary.md"])


def test_validate_accepts_scientific_unicode(jane_doe_dir: Path) -> None:
    target = jane_doe_dir / "sources" / "papers" / "doe2016example.md"
    extra = "\n".join(CLEAN_SAMPLES.values())
    target.write_text(target.read_text(encoding="utf-8") + "\n" + extra, encoding="utf-8")

    report = validate_profile_dir(jane_doe_dir)

    assert _text_failures(report) == {}
    assert report.ok
