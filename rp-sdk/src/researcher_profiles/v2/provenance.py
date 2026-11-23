"""Read and write `provenance.json`, the one provenance file of a profile (#5).

The file is one record with nested sub-records of the same shape. A part's
address is its key path below the root, through each record's `parts`:

- `("profile", field)` and `("profile", field, entry key)` for the person's fields
- `("files", role)` and `("files", role, heading text)` for single-file roles
- `("papers", paperId, "summary" | "fulltext")` for per-paper files

A part takes `method`, `generator` and `generatedAt` from the nearest record on
its path that has the field, up to the root. A field set to null stays null.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .common import INHERITED, PROVENANCE_FILE, PROVENANCE_ROLE, V2_CONTEXT, sha256_bytes

Path_ = tuple[str, ...]


def new_root(page_id: str, **fields: Any) -> dict:
    root: dict[str, Any] = {"about": page_id, "conformsTo": V2_CONTEXT}
    root.update(fields)
    root["parts"] = {}
    return root


def get(root: dict, path: Path_) -> dict | None:
    rec: Any = root
    for key in path:
        rec = (rec.get("parts") or {}).get(key) if isinstance(rec, dict) else None
        if rec is None:
            return None
    return rec if isinstance(rec, dict) else None


def ensure(root: dict, path: Path_) -> dict:
    """The record at `path`, created empty (with empty parents) when missing."""
    rec = root
    for key in path:
        rec = rec.setdefault("parts", {}).setdefault(key, {})
    return rec


def effective(root: dict, path: Path_, field: str) -> Any:
    """`field` for the part at `path`: its own value or the nearest one above."""
    assert field in INHERITED
    value = root.get(field)
    rec: Any = root
    for key in path:
        rec = (rec.get("parts") or {}).get(key) if isinstance(rec, dict) else None
        if not isinstance(rec, dict):
            break
        if field in rec:
            value = rec[field]
    return value


def walk(rec: dict, path: Path_ = ()) -> Iterator[tuple[Path_, dict]]:
    """Every record below `rec` (not `rec` itself), depth first, with its path."""
    for key, sub in (rec.get("parts") or {}).items():
        if isinstance(sub, dict):
            yield (*path, key), sub
            yield from walk(sub, (*path, key))


def prune(rec: dict) -> None:
    """Drop empty `parts` and records left empty, so the file stays sparse."""
    parts = rec.get("parts")
    if not isinstance(parts, dict):
        return
    for key in list(parts):
        if isinstance(parts[key], dict):
            prune(parts[key])
            if not parts[key]:
                del parts[key]
    if not parts and "about" not in rec:
        rec.pop("parts", None)


def entry_of(page: dict) -> dict | None:
    return next((e for e in page.get("hasPart", []) if e.get("role") == PROVENANCE_ROLE), None)


def load(profile_dir: Path | str, page: dict | None = None) -> dict | None:
    root = Path(profile_dir)
    page = page or json.loads((root / "profile.jsonld").read_text())
    entry = entry_of(page)
    path = root / (
        entry.get("contentUrl") if entry and entry.get("contentUrl") else PROVENANCE_FILE
    )
    return json.loads(path.read_text()) if path.exists() else None


def save(profile_dir: Path | str, prov: dict) -> None:
    """Write the provenance file and list it in the manifest with its hash and size."""
    root = Path(profile_dir)
    prune(prov)
    data = (json.dumps(prov, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    page = json.loads((root / "profile.jsonld").read_text())
    entry = entry_of(page)
    if entry is None:
        entry = {
            "@type": "DigitalDocument",
            "name": "Provenance",
            "role": PROVENANCE_ROLE,
            "encodingFormat": "application/json",
            "contentUrl": PROVENANCE_FILE,
            "visibility": "public",
        }
        page.setdefault("hasPart", []).append(entry)
    (root / entry["contentUrl"]).write_bytes(data)
    entry["bytes"] = len(data)
    entry["sha256"] = sha256_bytes(data)
    (root / "profile.jsonld").write_text(json.dumps(page, indent=2, ensure_ascii=False) + "\n")
