"""Constants and small helpers shared by v2 migration and validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

V1_CONTEXT = "https://profiles.databio.org/context/v1.jsonld"
V2_CONTEXT = "https://profiles.databio.org/context/v2.jsonld"

METHODS = ("declared", "inferred", "imported")
CONFIDENCE = ("high", "medium", "low")
PROVENANCE_FIELDS = ("method", "generator", "generatedAt", "derivedFrom")
# Record fields a sub-record takes from the nearest record above it.
INHERITED = ("method", "generator", "generatedAt")
RECORD_FIELDS = (*PROVENANCE_FIELDS, "sha256", "edits", "parts")

# The provenance file (#5): one per profile, listed in the manifest.
PROVENANCE_ROLE = "provenance"
PROVENANCE_FILE = "provenance.json"

# `user`, or `<tool>@<version>` with an optional `/<model>`. The tool name and
# version contain no `/`, so everything after the first `/` is the model id.
GENERATOR_RE = re.compile(r"^[^/@\s]+@[^/\s]+(/\S+)?$")

# The one datetime form (#7): UTC, to the second, ending in Z.
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")

# Roles that hold one file. `derivedFrom: {file: role}` may name only these.
SINGLE_FILE_ROLES = (
    "works",
    "expertise",
    "soul",
    "citations",
    "embedding_index",
    "grants",
    "topics",
    "cv",
    "interview",
    "clinical_expertise",
    "provenance",
)

# Prose roles whose level-2 sections get records in the provenance file.
PROSE_ROLES = ("expertise", "soul", "clinical_expertise")

# Per-paper roles and the key of their record under `papers.<paperId>`.
PAPER_ROLES = {"paper_summary": "summary", "paper_fulltext": "fulltext"}

# List fields of the person whose entries have a natural key. Other lists are
# keyed by a hash of the entry's content.
KEYED_LISTS = ("rp:researchInterests",)

# Local caches are not parts (#6).
CACHE_ROLES = ("embedding_index_sqlite",)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def normalize_datetime(value: object) -> str | None:
    """Return `value` in the one datetime form, or None when it isn't a datetime.

    v1 has microseconds, `+00:00` offsets and bare `Z`. Dates (no time part)
    keep their precision and are returned unchanged by callers, not here.
    """
    if not isinstance(value, str) or "T" not in value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def camel(key: str) -> str:
    """snake_case to camelCase. Keys with `@` or `:` (JSON-LD, `rp:` extensions) are left alone."""
    if key.startswith("@") or ":" in key or "_" not in key:
        return key
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def is_iri(value: object) -> bool:
    return isinstance(value, str) and re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:\S+$", value) is not None


# --------------------------------------------------------------------------- canonical JSON


def _js_number(x: float | int) -> str:
    """A number as ECMAScript's Number.prototype.toString writes it (RFC 8785, 3.2.2.3)."""
    if isinstance(x, bool):
        raise TypeError("booleans are not numbers")
    if isinstance(x, int):
        return str(x)
    if not math.isfinite(x):
        raise ValueError("NaN and Infinity have no JSON form")
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    # repr gives the shortest digits that round-trip, as ECMAScript does.
    mantissa, _, exp = repr(abs(x)).partition("e")
    int_part, _, frac_part = mantissa.partition(".")
    digits = (int_part + frac_part).lstrip("0") or "0"
    # n: the decimal exponent such that value = 0.digits * 10**n
    n = (
        len(int_part.lstrip("0"))
        if int_part.strip("0")
        else -(len(frac_part) - len(frac_part.lstrip("0")))
    )
    n += int(exp or 0)
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        out = digits + "0" * (n - k)
    elif 0 < n <= 21:
        out = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        out = (
            digits[0]
            + ("." + digits[1:] if k > 1 else "")
            + "e"
            + ("+" if e > 0 else "-")
            + str(abs(e))
        )
    return sign + out


def canonical_json(value: Any) -> bytes:
    """RFC 8785 (JSON Canonicalization Scheme) serialization of `value`, as UTF-8."""

    def enc(v: Any) -> str:
        if v is None:
            return "null"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, (int, float)):
            return _js_number(v)
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, list):
            return "[" + ",".join(enc(x) for x in v) + "]"
        if isinstance(v, dict):
            keys = sorted(v, key=lambda k: k.encode("utf-16-be"))
            return (
                "{"
                + ",".join(json.dumps(k, ensure_ascii=False) + ":" + enc(v[k]) for k in keys)
                + "}"
            )
        raise TypeError(f"{type(v).__name__} has no JSON form")

    return enc(value).encode("utf-8")


def value_sha256(value: Any) -> str:
    """Hash of a JSON value: SHA-256 of its canonical form."""
    return sha256_bytes(canonical_json(value))


def content_key(value: Any) -> str:
    """Key of an unkeyed list entry: the first 16 hex characters of its value hash."""
    return value_sha256(value)[:16]


def entry_key(field: str, entry: Any) -> str:
    """The key of one entry of the person's list `field` in the provenance file."""
    if field == "rp:researchInterests" and isinstance(entry, dict):
        concept = entry.get("concept") or {}
        if isinstance(concept, dict) and concept.get("@id"):
            return str(concept["@id"])
        return content_key(concept)
    return content_key(entry)
