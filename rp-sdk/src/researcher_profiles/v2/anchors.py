"""Stamp anchor records on `expertise.md` / `SOUL.md` manifest entries, without an LLM.

A builder that has just written a prose file calls this to record provenance per
section: one record per level-2 heading, with the section's `sha256` and the
papers its inline citations name (a bracketed id that matches a `paperId`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import slug
from .common import normalize_datetime

_CITE = re.compile(r"\[([^\[\]]+)\]")


def cited_papers(text: str, paper_ids: set[str]) -> list[str]:
    """Paper ids cited inline in `text`, in first-seen order. `[sic]` and `[1]` match no paperId."""
    out: list[str] = []
    for group in _CITE.findall(text):
        for token in (t.strip() for t in group.split(",")):
            if token in paper_ids and token not in out:
                out.append(token)
    return out


def section_records(text: str, paper_ids: set[str]) -> list[dict]:
    data = slug.normalize(text)
    records = []
    for sec in slug.scan(text).sections:
        body = data[sec.start : sec.end].decode("utf-8")
        rec: dict = {"anchor": sec.anchor, "sha256": sec.sha256}
        refs = [{"paper": p} for p in cited_papers(body, paper_ids)]
        if refs:
            rec["derivedFrom"] = refs
        records.append(rec)
    return records


def stamp(
    profile_dir: Path | str, role: str, *, method: str, generator: str, generated_at: str
) -> None:
    """Write anchor records for `role` (expertise or soul) into profile.jsonld.

    Every section gets `method`/`generator`/`generatedAt`, except where the page's
    `partDefaults` already says the same, so records stay short.
    """
    root = Path(profile_dir)
    page = json.loads((root / "profile.jsonld").read_text())
    defaults = page.get("partDefaults") or {}
    works = next(e for e in page["hasPart"] if e.get("role") == "works")
    paper_ids = {
        r["paperId"]
        for r in json.loads((root / works["contentUrl"]).read_text())["hasPart"]
        if r.get("paperId")
    }
    entry = next(e for e in page["hasPart"] if e.get("role") == role)
    text = (root / entry["contentUrl"]).read_text()
    stamped = {
        "method": method,
        "generator": generator,
        "generatedAt": normalize_datetime(generated_at) or generated_at,
    }
    records = []
    for rec in section_records(text, paper_ids):
        for k, val in stamped.items():
            if defaults.get(k) != val:
                rec[k] = val
        records.append(rec)
    entry["anchors"] = records
    for k, val in stamped.items():
        if defaults.get(k) != val:
            entry[k] = val
        else:
            entry.pop(k, None)
    (root / "profile.jsonld").write_text(json.dumps(page, indent=2, ensure_ascii=False) + "\n")
