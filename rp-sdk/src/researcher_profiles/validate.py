"""Build-time and CLI schema validation for researcher-profile artifacts.

This module is the single validator for the researcher-profiles format,
providing both Pydantic model conformance checks and undeclared-term detection.

Design rules:

- Schemas are imported in-process from ``researcher_profiles.schema_export``.
  No vendored copies, no network fetch. One source of truth.
- This module must import nothing outside the SDK's core tier. Validity is
  a property of the *format*; gating is a property of the *build*.
- Schema conformance failure is deterministic and structural. Undeclared-term
  drift is a separate check (``undeclared_terms``) that the build may route
  through questions.
- ``additionalProperties`` stays ``true`` in the Pydantic models.
  Strictness lives in ``undeclared_terms``, not in the JSON Schema.
- Content quality (expertise.md length, SOUL.md sections, missing bracketed
  paper_id citations) is out of this module's remit. Nothing in
  the SDK enforces it; that judgment belongs to the agent writing the profile.

**Two-validator split.**  The Pydantic models are the authoritative conformance
check: they accept the on-disk serialized form via ``mode="before"`` validators
(e.g. ``datePublished`` is a string on disk, coerced to ``int`` by the model).
The exported JSON Schema (``build_schemas()``) describes the Python input shape,
which differs from the on-disk shape for fields with custom serializers. The
validator therefore uses Pydantic ``model_validate`` for conformance and the
JSON Schema only for undeclared-term detection via ``known_terms()``.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError

from .schema import (
    GrantsDocument,
    PapersDocument,
    ProfileDocument,
    SummaryFile,
)
from .schema.jsonld import CONTEXT_URL, read_jsonld
from .schema_export import build_schemas
from .utils.paths import CACHE_DIRNAME

if TYPE_CHECKING:
    from .schema import ArtifactRef

# ---------------------------------------------------------------------------
# Schema name -> Pydantic model mapping
# ---------------------------------------------------------------------------
#
# One document shape: profile.jsonld is a ProfileDocument and papers.jsonld a
# PapersDocument, authored and published alike. There is no separate
# published-manifest schema.

_SCHEMA_MODELS: dict[str, type] = {
    "profile_jsonld": ProfileDocument,
    "papers_jsonld": PapersDocument,
    "grants_jsonld": GrantsDocument,
    "summary_file": SummaryFile,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One schema conformance violation."""

    json_pointer: str
    keyword: str
    message: str
    found: str
    expected: str
    fix: str


@dataclass(frozen=True)
class TermUse:
    """An undeclared term found in a document."""

    term: str
    json_pointer: str
    sample_value: str


@dataclass
class ArtifactResult:
    """Validation result for a single artifact."""

    path: str
    schema_name: str
    exists: bool
    parsed: bool
    ok: bool
    violations: list[Violation] = field(default_factory=list)
    truncated: int = 0
    undeclared: list[TermUse] = field(default_factory=list)


@dataclass
class ProfileValidationReport:
    """Full validation report for a profile directory."""

    profile_dir: str
    validated_at: str
    schema_fingerprint: str
    package_version: str
    artifacts: list[ArtifactResult] = field(default_factory=list)
    cross_artifact: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(a.ok for a in self.artifacts) and not self.cross_artifact


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_VIOLATIONS = 25
_MAX_FOUND_CHARS = 120


def _cap(text: str, limit: int = _MAX_FOUND_CHARS) -> str:
    """Cap a string at ``limit`` chars with an ellipsis."""
    s = str(text)
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# Pydantic error -> Violation conversion
# ---------------------------------------------------------------------------


def _pydantic_error_to_violation(err: dict[str, Any]) -> Violation:
    """Convert one Pydantic V2 error dict to a Violation."""
    loc = err.get("loc", ())
    pointer = "/" + "/".join(str(p) for p in loc) if loc else "/"
    err_type = err.get("type", "unknown")
    msg = err.get("msg", "")
    ctx = err.get("ctx", {})

    # Compute 'found' from the input value (Pydantic may include it in ctx)
    found = _cap(str(err.get("input", "<unknown>")))

    # Compute 'expected' based on error type
    if err_type == "missing":
        expected = "field is required"
    elif err_type == "string_type":
        expected = "type: string"
    elif err_type == "int_type":
        expected = "type: integer"
    elif err_type == "list_type":
        expected = "type: list"
    elif err_type == "dict_type":
        expected = "type: object"
    elif err_type == "literal_error":
        expected_val = ctx.get("expected", "")
        expected = f"one of: {expected_val}"
    elif err_type == "value_error":
        expected = msg
    elif "min_length" in err_type:
        expected = f"min_length: {ctx.get('min_length', '?')}"
    else:
        expected = msg

    # Compute 'fix'
    if err_type == "missing":
        fix = f"add the required field at {pointer}"
    elif "value_error" in err_type and "conformsTo" in pointer:
        fix = f"set conformsTo to the @context IRI ({CONTEXT_URL})"
    elif "value_error" in err_type:
        fix = msg
    else:
        fix = f"fix the {err_type} error at {pointer}"

    return Violation(
        json_pointer=pointer,
        keyword=err_type,
        message=_cap(msg, 300),
        found=found,
        expected=expected,
        fix=fix,
    )


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------


def validate_artifact(
    path: str | Path,
    schema_name: str,
) -> ArtifactResult:
    """Validate a single artifact file against its Pydantic model.

    Returns an :class:`ArtifactResult` whether or not the file passes.
    """
    p = Path(path)
    result = ArtifactResult(
        path=str(p),
        schema_name=schema_name,
        exists=False,
        parsed=False,
        ok=False,
    )

    if not p.is_file():
        result.violations = [
            Violation(
                json_pointer="/",
                keyword="file",
                message=f"file not found: {p}",
                found="<missing>",
                expected="file exists",
                fix=f"create {p.name}",
            )
        ]
        return result

    result.exists = True

    # Parse
    try:
        if p.suffix == ".json":
            doc = json.loads(p.read_text(encoding="utf-8"))
        else:
            doc = read_jsonld(p)
    except (json.JSONDecodeError, OSError) as exc:
        result.violations = [
            Violation(
                json_pointer="/",
                keyword="parse",
                message=f"parse error: {exc}",
                found="<unparseable>",
                expected="valid JSON",
                fix="fix the JSON syntax",
            )
        ]
        return result

    result.parsed = True

    # Validate against the Pydantic model
    model_cls = _SCHEMA_MODELS.get(schema_name)
    if model_cls is None:
        raise KeyError(f"Unknown schema name {schema_name!r}; available: {sorted(_SCHEMA_MODELS)}")

    try:
        model_cls.model_validate(doc)
    except (PydanticValidationError, ValueError) as exc:
        if isinstance(exc, PydanticValidationError):
            raw_errors = exc.errors()
        else:
            # A ValueError from a model_validator (e.g. bare list for papers)
            raw_errors = [{"loc": (), "type": "value_error", "msg": str(exc)}]

        violations = sorted(
            [_pydantic_error_to_violation(e) for e in raw_errors],
            key=lambda v: (v.json_pointer, v.keyword, v.message),
        )
        truncated = max(0, len(violations) - _MAX_VIOLATIONS)
        violations = violations[:_MAX_VIOLATIONS]
        result.violations = violations
        result.truncated = truncated
        result.ok = False
        return result

    # Undeclared terms and retired-key detection (only for JSON-LD artifacts,
    # not summary)
    if schema_name in ("profile_jsonld", "papers_jsonld", "grants_jsonld"):
        result.undeclared = undeclared_terms(doc, schema_name)
        result.violations = [_retired_term_violation(t) for t in retired_terms(doc)]

    result.ok = not result.violations
    return result


# ---------------------------------------------------------------------------
# Known terms and undeclared-term detection
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def known_terms(schema_name: str) -> frozenset[str]:
    """Return the known terms for one on-disk document schema.

    known = { every field alias in ``schema_name``'s own JSON Schema, and
             the ``$defs`` it reaches }
           U { every term defined in context/v1.jsonld }
           U { "@context", "@id", "@type", "@value", "@list", "@set", "@graph" }

    Scoped to one schema on purpose. A property name is only "known" because
    ``schema_name`` itself declares it, not because some unrelated schema
    happens to reuse the word: ``ProfileCollection`` (schema name
    ``collection``) has its own ``artifacts`` field, but that must not
    license the word ``artifacts`` inside a ``profile.jsonld``, which is a
    different document with a different vocabulary. Unioning across every
    published schema is exactly the bug this scoping avoids: a rename on one
    document type would otherwise go undetected on every other.
    """
    terms: set[str] = set()

    # JSON-LD keywords
    terms.update(
        {"@context", "@id", "@type", "@value", "@list", "@set", "@graph", "@vocab", "@version"}
    )

    # This schema's own field aliases (and whatever it reaches via $defs).
    _collect_property_names(build_schemas()[schema_name], terms)

    # Terms from context/v1.jsonld (bundled copy in a wheel, repo-root in a
    # source checkout, see jsonld.context_document_text). The context is
    # shared vocabulary across every document type, not per-schema.
    from .schema.jsonld import context_document_text

    context_text = context_document_text()
    if context_text is not None:
        try:
            ctx_doc = json.loads(context_text)
            ctx = ctx_doc.get("@context", {})
            for key in ctx:
                if not key.startswith("@"):
                    terms.add(key)
        except json.JSONDecodeError:
            pass

    return frozenset(terms)


def _collect_property_names(schema: dict, terms: set[str]) -> None:
    """Recursively collect all property names from a JSON Schema."""
    for key in schema.get("properties", {}):
        terms.add(key)
    for defn in schema.get("$defs", {}).values():
        for key in defn.get("properties", {}):
            terms.add(key)


def _walk_keys(doc: Any, prefix: str = "") -> list[tuple[str, str, Any]]:
    """Walk all object keys in a document, yielding (term, json_pointer, sample_value)."""
    results: list[tuple[str, str, Any]] = []
    if isinstance(doc, dict):
        for key, value in doc.items():
            pointer = f"{prefix}/{key}"
            results.append((key, pointer, value))
            results.extend(_walk_keys(value, pointer))
    elif isinstance(doc, list):
        for i, item in enumerate(doc):
            results.extend(_walk_keys(item, f"{prefix}/{i}"))
    return results


def undeclared_terms(doc: Any, schema_name: str) -> list[TermUse]:
    """Find terms in ``doc`` that are not known to ``schema_name``.

    The vocabulary (:func:`known_terms`) is scoped to the one document schema
    being checked, not every published schema, so a term only counts as known
    when ``schema_name`` itself declares it. Deciding *whether* a document
    gets the check at all is the caller's job (see :func:`validate_artifact`).

    Returns a list of :class:`TermUse` entries, sorted by term then pointer.
    """
    known = known_terms(schema_name)
    seen: dict[str, TermUse] = {}

    for term, pointer, value in _walk_keys(doc):
        if term not in known:
            if term not in seen:
                sample = _cap(repr(value), 80)
                seen[term] = TermUse(term=term, json_pointer=pointer, sample_value=sample)

    return sorted(seen.values(), key=lambda t: (t.term, t.json_pointer))


# ---------------------------------------------------------------------------
# Retired-key detection
# ---------------------------------------------------------------------------

#: Key -> replacement key, for on-disk field names retired since first
#: publication. Checked unconditionally, independent of :func:`known_terms`:
#: a retired key can still be "known" (some other schema may reuse the same
#: word for something else), so this cannot rely on vocabulary membership.
#: A document that reused a retired name would otherwise pass silently, which
#: is exactly how ``artifacts`` (renamed to ``researchOutputs``) went
#: undetected on six live profiles.
RETIRED_TERMS: dict[str, str] = {
    "artifacts": "researchOutputs",
}


def retired_terms(doc: Any) -> list[TermUse]:
    """Find retired keys in ``doc``, checked against :data:`RETIRED_TERMS`.

    Independent of :func:`known_terms`: a retired key is flagged whether or
    not some other schema happens to still declare a field of that name.

    Returns a list of :class:`TermUse` entries, sorted by term then pointer.
    """
    seen: dict[str, TermUse] = {}

    for term, pointer, value in _walk_keys(doc):
        if term in RETIRED_TERMS and term not in seen:
            sample = _cap(repr(value), 80)
            seen[term] = TermUse(term=term, json_pointer=pointer, sample_value=sample)

    return sorted(seen.values(), key=lambda t: (t.term, t.json_pointer))


def _retired_term_violation(hit: TermUse) -> Violation:
    """Build the actionable :class:`Violation` for one retired-key hit."""
    replacement = RETIRED_TERMS[hit.term]
    return Violation(
        json_pointer=hit.json_pointer,
        keyword="retired_term",
        message=f"{hit.term!r} was renamed to {replacement!r}",
        found=hit.term,
        expected=replacement,
        fix=f"rename {hit.term!r} to {replacement!r} at {hit.json_pointer}",
    )


# ---------------------------------------------------------------------------
# Profile-directory validation
# ---------------------------------------------------------------------------


def validate_profile_dir(profile_dir: str | Path) -> ProfileValidationReport:
    """Validate all artifacts in a profile directory.

    Walks the artifact table below, runs per-artifact schema validation, then
    checks cross-artifact invariants. A profile is a single document shape.
    ``profile.jsonld`` is always a ``ProfileDocument`` (there is no separate
    published-manifest form to detect).
    """
    root = Path(profile_dir)
    now = datetime.now(timezone.utc).isoformat()

    report = ProfileValidationReport(
        profile_dir=str(root),
        validated_at=now,
        schema_fingerprint=schema_fingerprint(),
        package_version=_package_version(),
    )

    # Per-artifact validation
    artifact_map = [
        (root / "profile.jsonld", "profile_jsonld"),
        (root / "sources" / "papers.jsonld", "papers_jsonld"),
        (root / "sources" / "grants.jsonld", "grants_jsonld"),
    ]
    for path, schema_name in artifact_map:
        # grants.jsonld is presence-conditional
        if schema_name == "grants_jsonld" and not path.is_file():
            continue
        art_result = validate_artifact(path, schema_name)
        report.artifacts.append(art_result)

    # Per-file validation for summaries
    summaries_dir = root / "sources" / "summaries"
    if summaries_dir.is_dir():
        for f in sorted(summaries_dir.iterdir()):
            if f.is_file() and f.name.endswith(".summary.md"):
                text = f.read_text(encoding="utf-8")
                fm = _extract_frontmatter(text)
                if fm is not None:
                    art_result = _validate_data(fm, "summary_file", str(f))
                    report.artifacts.append(art_result)

    # Cross-artifact invariants
    _check_cross_artifact(root, report)

    return report


def _extract_frontmatter(text: str) -> dict | None:
    """Extract YAML frontmatter from markdown text."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    import yaml as _yaml

    try:
        data = _yaml.safe_load(parts[1])
    except _yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _validate_data(data: dict, schema_name: str, label: str) -> ArtifactResult:
    """Validate an in-memory dict against a Pydantic model."""
    result = ArtifactResult(
        path=label,
        schema_name=schema_name,
        exists=True,
        parsed=True,
        ok=False,
    )
    model_cls = _SCHEMA_MODELS.get(schema_name)
    if model_cls is None:
        raise KeyError(f"Unknown schema name {schema_name!r}")

    try:
        model_cls.model_validate(data)
    except (PydanticValidationError, ValueError) as exc:
        if isinstance(exc, PydanticValidationError):
            raw_errors = exc.errors()
        else:
            raw_errors = [{"loc": (), "type": "value_error", "msg": str(exc)}]
        violations = sorted(
            [_pydantic_error_to_violation(e) for e in raw_errors],
            key=lambda v: (v.json_pointer, v.keyword, v.message),
        )
        truncated = max(0, len(violations) - _MAX_VIOLATIONS)
        violations = violations[:_MAX_VIOLATIONS]
        result.violations = violations
        result.truncated = truncated
        return result

    result.ok = True
    return result


def _load_json_doc(path: Path) -> dict | None:
    """Read a JSON-LD document, or ``None`` when it is absent or unreadable.

    A document that is valid JSON but not an object is ``None`` here too: the
    per-artifact pass already reports its shape, and the cross-artifact checks
    have nothing to read from it.
    """
    try:
        doc = read_jsonld(path)
    except (json.JSONDecodeError, OSError):
        return None
    return doc if isinstance(doc, dict) else None


def _paper_ids(papers_doc: dict) -> set[str]:
    """The paper_id of every record in a papers document."""
    return {
        p["paper_id"]
        for p in papers_doc.get("hasPart", [])
        if isinstance(p, dict) and p.get("paper_id")
    }


def _manifest_refs(profile_doc: dict, report: ProfileValidationReport) -> "list[ArtifactRef]":
    """Parse the manifest entries, reporting any that are not a valid ArtifactRef.

    A malformed entry is a finding, not something to skip: dropping it silently
    would let the drift check call an unclean profile clean.
    """
    from .schema import ArtifactRef

    refs: list[ArtifactRef] = []
    for section_name in ("hasPart", "subjectOf"):
        for entry in profile_doc.get(section_name, []):
            if not isinstance(entry, dict):
                continue
            try:
                refs.append(ArtifactRef.model_validate(entry))
            except PydanticValidationError as exc:
                report.cross_artifact.append(
                    Violation(
                        json_pointer=f"/{section_name}",
                        keyword="manifest_entry",
                        message=f"manifest entry is not a valid ArtifactRef: {_cap(str(exc), 300)}",
                        found=_cap(repr(entry)),
                        expected="a valid ArtifactRef object",
                        fix="run `rp manifest --write` to regenerate",
                    )
                )
    return refs


#: Directory name used for the profile-adjacent derived cache before its
#: rename to :data:`~researcher_profiles.utils.paths.CACHE_DIRNAME`
#: (``.cache/``). A manifest entry still pointing under this name is not
#: really dangling: the file is on disk, just under the old directory.
_LEGACY_CACHE_DIRNAME = "cache"


def _split_legacy_cache_stale(
    root: Path, stale: list[str], report: ProfileValidationReport
) -> list[str]:
    """Pull pre-rename-cache-dir entries out of ``stale``, reporting them.

    ``build_manifest`` only ever looks for the derived sqlite cache under
    :data:`CACHE_DIRNAME` (``.cache/``), so a manifest recorded before that
    rename names the file under the old, un-dotted ``cache/`` directory. The
    fresh manifest has nothing at that path, and generic drift then reads it
    as "no file" even though the file is right there under the old name.

    Returns the remaining ``stale`` entries, for the caller to report as
    ordinary drift.
    """
    legacy_prefix = f"{_LEGACY_CACHE_DIRNAME}/"
    legacy = [
        entry for entry in stale if entry.startswith(legacy_prefix) and (root / entry).is_file()
    ]
    if legacy:
        report.cross_artifact.append(
            Violation(
                json_pointer="/hasPart",
                keyword="legacy_cache_dirname",
                message=(
                    f"{len(legacy)} manifest entry(ies) under the retired cache "
                    f"directory name {_LEGACY_CACHE_DIRNAME!r}: {legacy[:5]}"
                ),
                found=f"{_LEGACY_CACHE_DIRNAME}/",
                expected=f"{CACHE_DIRNAME}/",
                fix=(
                    f"move the file(s) from {_LEGACY_CACHE_DIRNAME}/ to {CACHE_DIRNAME}/ "
                    "and run `rp manifest --write` to regenerate"
                ),
            )
        )
    return [entry for entry in stale if entry not in legacy]


def _check_manifest_drift(root: Path, profile_doc: dict, report: ProfileValidationReport) -> None:
    """Manifest entries match what is on disk."""
    from .schema.manifest import manifest_drift

    drift = manifest_drift(root, _manifest_refs(profile_doc, report))
    if drift["missing"]:
        report.cross_artifact.append(
            Violation(
                json_pointer="/hasPart",
                keyword="manifest_drift",
                message=(
                    f"{len(drift['missing'])} file(s) on disk not in manifest: "
                    f"{drift['missing'][:5]}"
                ),
                found=f"{len(drift['missing'])} unlisted files",
                expected="manifest lists all artifacts on disk",
                fix="run `rp manifest --write` to regenerate",
            )
        )
    stale = _split_legacy_cache_stale(root, drift["stale"], report)
    if stale:
        report.cross_artifact.append(
            Violation(
                json_pointer="/hasPart",
                keyword="manifest_drift",
                message=f"{len(stale)} manifest entry(ies) with no file: {stale[:5]}",
                found=f"{len(stale)} dangling entries",
                expected="every manifest entry has a file on disk",
                fix="run `rp manifest --write` to regenerate",
            )
        )


def _check_orphan_summaries(
    root: Path, paper_ids: set[str], report: ProfileValidationReport
) -> None:
    """Summary files match real papers."""
    summaries_dir = root / "sources" / "summaries"
    if not summaries_dir.is_dir():
        return
    summary_stems = {
        f.name[: -len(".summary.md")]
        for f in summaries_dir.iterdir()
        if f.is_file() and f.name.endswith(".summary.md")
    }
    orphan_summaries = sorted(summary_stems - paper_ids)
    if orphan_summaries:
        report.cross_artifact.append(
            Violation(
                json_pointer="/sources/summaries",
                keyword="cross_artifact",
                message=(
                    f"{len(orphan_summaries)} summary file(s) with no "
                    f"matching paper_id: {orphan_summaries[:5]}"
                ),
                found=f"{len(orphan_summaries)} orphan summaries",
                expected="every summary matches a paper in papers.jsonld",
                fix="remove orphan summaries or add papers to papers.jsonld",
            )
        )


def _check_content_urls(root: Path, profile_doc: dict, report: ProfileValidationReport) -> None:
    """Every manifest contentUrl is relative and resolves on disk."""
    for section_name in ("hasPart", "subjectOf"):
        for entry in profile_doc.get(section_name, []):
            if not isinstance(entry, dict):
                continue
            url = entry.get("contentUrl", "")
            if not url:
                continue
            if url.startswith(("http://", "https://", "/")):
                report.cross_artifact.append(
                    Violation(
                        json_pointer=f"/{section_name}/contentUrl",
                        keyword="relative_url",
                        message=f"contentUrl is absolute: {url!r}",
                        found=url,
                        expected="relative path",
                        fix="change to a relative path within the profile directory",
                    )
                )
            elif not (root / url).exists():
                report.cross_artifact.append(
                    Violation(
                        json_pointer=f"/{section_name}/contentUrl",
                        keyword="content_url_missing",
                        message=f"contentUrl resolves to a missing file: {url}",
                        found=url,
                        expected="file exists on disk",
                        fix=f"create {url} or remove the manifest entry",
                    )
                )


def _check_cross_artifact(root: Path, report: ProfileValidationReport) -> None:
    """Check cross-artifact invariants and append violations to the report.

    Each check depends only on the documents it names, so an artifact that
    cannot be read skips its own checks and no others. Aborting the whole pass
    on one unreadable file would report a corrupt profile as clean.
    """
    profile_doc = _load_json_doc(root / "profile.jsonld")
    papers_doc = _load_json_doc(root / "sources" / "papers.jsonld")

    if profile_doc is not None:
        _check_manifest_drift(root, profile_doc, report)
        _check_content_urls(root, profile_doc, report)
    if papers_doc is not None:
        _check_orphan_summaries(root, _paper_ids(papers_doc), report)


# ---------------------------------------------------------------------------
# Fingerprint and version
# ---------------------------------------------------------------------------


def schema_fingerprint() -> str:
    """SHA-256 over the canonical JSON Schema bundle."""
    raw = json.dumps(build_schemas(), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _package_version() -> str:
    """Return the installed researcher_profiles version."""
    import researcher_profiles

    return researcher_profiles.__version__


# ---------------------------------------------------------------------------
# Report serialization
# ---------------------------------------------------------------------------


def report_to_dict(report: ProfileValidationReport) -> dict[str, Any]:
    """Serialize a report to a JSON-safe dict."""
    return {
        "profile_dir": report.profile_dir,
        "validated_at": report.validated_at,
        "schema_fingerprint": report.schema_fingerprint,
        "package_version": report.package_version,
        "ok": report.ok,
        "artifacts": [
            {
                "path": a.path,
                "schema_name": a.schema_name,
                "exists": a.exists,
                "parsed": a.parsed,
                "ok": a.ok,
                "violations": [
                    {
                        "json_pointer": v.json_pointer,
                        "keyword": v.keyword,
                        "message": v.message,
                        "found": v.found,
                        "expected": v.expected,
                        "fix": v.fix,
                    }
                    for v in a.violations
                ],
                "truncated": a.truncated,
                "undeclared": [
                    {
                        "term": t.term,
                        "json_pointer": t.json_pointer,
                        "sample_value": t.sample_value,
                    }
                    for t in a.undeclared
                ],
            }
            for a in report.artifacts
        ],
        "cross_artifact": [
            {
                "json_pointer": v.json_pointer,
                "keyword": v.keyword,
                "message": v.message,
                "found": v.found,
                "expected": v.expected,
                "fix": v.fix,
            }
            for v in report.cross_artifact
        ],
    }


def format_text_report(report: ProfileValidationReport) -> str:
    """Format a report as human-readable text grouped by artifact."""
    lines: list[str] = []
    status = "PASS" if report.ok else "FAIL"
    lines.append(f"Validation: {status}")
    lines.append(f"Profile: {report.profile_dir}")
    lines.append(f"Schema fingerprint: {report.schema_fingerprint[:16]}...")
    lines.append(f"Package version: {report.package_version}")
    lines.append("")

    for artifact in report.artifacts:
        mark = "OK" if artifact.ok else "FAIL"
        lines.append(f"  [{mark}] {artifact.path} ({artifact.schema_name})")
        if not artifact.exists:
            lines.append("       file not found")
            continue
        if not artifact.parsed:
            lines.append("       parse error")
            continue
        for v in artifact.violations:
            lines.append(f"       {v.json_pointer}: {v.keyword} - {v.message}")
            lines.append(f"         found: {v.found}")
            lines.append(f"         expected: {v.expected}")
            lines.append(f"         fix: {v.fix}")
        if artifact.truncated:
            lines.append(f"       ... {artifact.truncated} more violation(s) truncated")
        if artifact.undeclared:
            lines.append(f"       undeclared terms ({len(artifact.undeclared)}):")
            for t in artifact.undeclared[:10]:
                lines.append(f"         {t.term} at {t.json_pointer}")

    if report.cross_artifact:
        lines.append("")
        lines.append("  Cross-artifact invariants:")
        for v in report.cross_artifact:
            lines.append(f"    {v.json_pointer}: {v.keyword} - {v.message}")

    return "\n".join(lines)


__all__ = [
    "RETIRED_TERMS",
    "ArtifactResult",
    "ProfileValidationReport",
    "TermUse",
    "Violation",
    "format_text_report",
    "known_terms",
    "report_to_dict",
    "retired_terms",
    "schema_fingerprint",
    "undeclared_terms",
    "validate_artifact",
    "validate_profile_dir",
]
