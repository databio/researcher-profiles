"""Constants and small helpers shared by v2 migration and validation."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

V1_CONTEXT = "https://profiles.databio.org/context/v1.jsonld"
V2_CONTEXT = "https://profiles.databio.org/context/v2.jsonld"

METHODS = ("declared", "inferred", "imported")
CONFIDENCE = ("high", "medium", "low")
PROVENANCE_FIELDS = ("method", "generator", "generatedAt", "derivedFrom")
DEFAULTABLE = ("method", "generator", "generatedAt")

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
)

# Prose roles whose sections carry `anchors`.
ANCHOR_ROLES = ("expertise", "soul")

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
