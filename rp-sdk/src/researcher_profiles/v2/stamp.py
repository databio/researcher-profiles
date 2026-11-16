"""Write section records for a prose file into `provenance.json`, without an LLM.

A builder that has just written `expertise.md`, `SOUL.md` or
`clinical_expertise.md` calls `stamp_sections` to record provenance per level-2
section: the section's `sha256` and the papers its inline citations name (a
bracketed id that matches a `paperId`). The file's record gets the file's hash
and, where they differ from what it would inherit, `method`, `generator` and
`generatedAt`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import provenance, sections
from .common import normalize_datetime, sha256_file

_CITE = re.compile(r"\[([^\[\]]+)\]")


def cited_papers(text: str, paper_ids: set[str]) -> list[str]:
    """Paper ids cited inline in `text`, in first-seen order. `[sic]` and `[1]` match no paperId."""
    out: list[str] = []
    for group in _CITE.findall(text):
        for token in (t.strip() for t in group.split(",")):
            if token in paper_ids and token not in out:
                out.append(token)
    return out


def section_records(text: str, paper_ids: set[str]) -> dict[str, dict]:
    """One record per level-2 section, keyed by heading text: its hash and cited papers."""
    out: dict[str, dict] = {}
    for sec in sections.scan(text).sections:
        rec: dict = {"sha256": sec.sha256}
        refs = [{"paper": p} for p in cited_papers(sections.section_text(text, sec), paper_ids)]
        if refs:
            rec["derivedFrom"] = refs
        out[sec.heading] = rec
    return out


def stamp_sections(
    profile_dir: Path | str, role: str, *, method: str, generator: str, generated_at: str
) -> None:
    """Replace the records for `role`'s file and its sections in `provenance.json`."""
    root = Path(profile_dir)
    page = json.loads((root / "profile.jsonld").read_text())
    prov = provenance.load(root, page)
    if prov is None:
        raise FileNotFoundError("the profile has no provenance.json")
    works = next(e for e in page["hasPart"] if e.get("role") == "works")
    paper_ids = {
        r["paperId"]
        for r in json.loads((root / works["contentUrl"]).read_text())["hasPart"]
        if r.get("paperId")
    }
    entry = next(e for e in page["hasPart"] if e.get("role") == role)
    path = root / entry["contentUrl"]
    stamped = {
        "method": method,
        "generator": generator,
        "generatedAt": normalize_datetime(generated_at) or generated_at,
    }
    rec: dict = {k: v for k, v in stamped.items() if prov.get(k) != v}
    rec["sha256"] = sha256_file(path)
    parts = section_records(path.read_text(), paper_ids)
    if parts:
        rec["parts"] = parts
    files = provenance.ensure(prov, ("files",))
    files.setdefault("parts", {})[role] = rec
    provenance.save(root, prov)
