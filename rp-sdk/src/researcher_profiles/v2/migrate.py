"""`rp migrate`: rewrite a v1 profile directory as v2 (issues #4-#7), offline.

Migration fills in only what the v1 data says. It never invents a generator, a
`declared` label or a fetch date. Everything v1 doesn't record is left out,
which v2 reads as unknown. The source directory is never modified.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .common import (
    CACHE_ROLES,
    GENERATOR_RE,
    V2_CONTEXT,
    camel,
    normalize_datetime,
    sha256_bytes,
)


class MigrationError(Exception):
    """The profile can't be migrated as is (for example, a listed file is missing)."""


# Top-level v1 keys that describe the document and move to the page.
_PAGE_KEYS = (
    "license",
    "level",
    "visibility",
    "rp:sectionVisibility",
    "proof",
    "verifiedAt",
    "synthesisInputsDigest",
)

# Keys #7 drops outright.
_DROPPED_FLAGS = ("hasCitationGraph", "hasEmbeddingIndex", "expertiseCitesPaperIds")

# Keys found in profiles but not in the v1 schema. Migration drops and reports them.
_OFF_SCHEMA = ("openalex_id", "role", "email")

_TEXT_LISTS = (
    "expertise",
    "critiques",
    "methodological_commitments",
    "recurring_positions",
    "intellectual_lineage",
)

_PERSON_KEYS = (
    "name", "rid", "identifier", "sameAs", "jobTitle", "field", "subfields", "interests", "not_interests",
    "researchOutputs", "career", "training", "paper_stats", "rp:researchInterests",
)  # fmt: skip

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


class _Report:
    def __init__(self) -> None:
        self.dropped: dict[str, int] = {}
        self.notes: list[str] = []
        self.counts: dict[str, int] = {}

    def drop(self, what: str, n: int = 1) -> None:
        self.dropped[what] = self.dropped.get(what, 0) + n

    def count(self, what: str, n: int = 1) -> None:
        self.counts[what] = self.counts.get(what, 0) + n

    def as_dict(self) -> dict:
        return {"dropped": self.dropped, "notes": self.notes, "counts": self.counts}


# --------------------------------------------------------------------------- papers


def _migrate_paper(rec: dict, rep: _Report) -> dict:
    out: dict[str, Any] = {}
    for k, v in rec.items():
        if k == "@id":
            if isinstance(v, str) and v.startswith("#"):
                rep.drop("paper @id fragment")
                continue
            out["@id"] = v
        elif k == "paper_id":
            out["paperId"] = v
        elif k in ("first_author", "last_author"):
            rep.drop(f"paper {k}")
        elif k in ("author_position", "author_index", "total_authors"):
            continue  # folded into `authorship` below
        elif k in ("isPartOf", "venue", "source"):
            continue  # folded into `venue` below
        elif k in ("access", "open_access", "is_oa", "oa_url", "pdf_url", "full_text_link"):
            continue  # folded into `openAccess` / `fullTextLinks` below
        elif k == "topics":
            out["topics"] = [
                t if str(t).startswith("http") else f"https://openalex.org/{t}" for t in v or []
            ]
        elif k == "citation":
            out["citationText"] = v
        else:
            out[camel(k)] = v
    authorship = _drop_none(
        {
            "position": rec.get("author_position"),
            "index": rec.get("author_index"),
            "totalAuthors": rec.get("total_authors"),
        }
    )
    if authorship:
        out["authorship"] = authorship
    part_of = rec.get("isPartOf")
    name = (part_of or {}).get("name") if isinstance(part_of, dict) else None
    name = name or rec.get("venue") or rec.get("source")
    if name:
        venue = {"name": name}
        if isinstance(part_of, dict) and part_of.get("@id"):
            venue["@id"] = part_of["@id"]
        out["venue"] = venue
    flags = [rec.get(k) for k in ("is_oa", "open_access") if isinstance(rec.get(k), bool)]
    access = rec.get("access")
    if flags or access:
        oa: dict[str, Any] = {}
        if flags:
            oa["isOpen"] = any(flags)
        elif access:
            oa["isOpen"] = access == "open"
        if access:
            oa["status"] = access
        out["openAccess"] = oa
    links, seen = [], set()
    for key, kind in (("full_text_link", "fulltext"), ("oa_url", "oa"), ("pdf_url", "pdf")):
        url = rec.get(key)
        if url and url not in seen:
            seen.add(url)
            links.append({"url": url, "kind": kind})
    if links:
        out["fullTextLinks"] = links
    return out


def _migrate_papers_doc(doc: dict, rep: _Report) -> tuple[dict, dict[str, str]]:
    """Return the v2 works document and a map from OpenAlex work id (`W…`) to paperId."""
    out = {k: v for k, v in doc.items() if k != "hasPart"}
    out["@context"] = V2_CONTEXT
    out["conformsTo"] = V2_CONTEXT
    records = [_migrate_paper(r, rep) for r in doc.get("hasPart", [])]
    out["hasPart"] = records
    oa_map = {}
    for r in doc.get("hasPart", []):
        oid = r.get("openalex_id")
        if oid and r.get("paper_id"):
            oa_map[str(oid).rsplit("/", 1)[-1]] = r["paper_id"]
    rep.count("papers", len(records))
    return out, oa_map


# --------------------------------------------------------------------------- person


def _text_part(value: Any) -> dict:
    if isinstance(value, dict):
        return value  # already a part
    return {"text": value}


def _collaborator(c: Any, rep: _Report) -> dict:
    if isinstance(c, str):
        if " - " in c or " — " in c:
            rep.count("collaborator strings with an embedded description")
        return {"name": c}
    out: dict[str, Any] = {}
    notes = []
    for k, v in c.items():
        if k == "role":
            out.setdefault("relationship", v)
        elif k == "description":
            notes.append(v)
        elif k == "note":
            notes.insert(0, v)
        elif k == "@id" and isinstance(v, str) and v.startswith("http") and "orcid.org" not in v:
            out["url"] = v
        else:
            out[camel(k)] = v
    if notes:
        out["note"] = " ".join(n for n in notes if n)
    return out


def _research_output(o: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in o.items():
        if k == "@id" and isinstance(v, str) and v.startswith("http") and "doi.org" not in v:
            out.setdefault("url", v)  # a repository URL used as @id moves to url (#6)
        else:
            out[camel(k)] = v
    return out


def _span(entry: dict) -> dict:
    out = {}
    for k, v in entry.items():
        if k in ("start_year", "year_start"):
            out["startYear"] = v
        elif k in ("end_year", "year_end"):
            out["endYear"] = v
        else:
            out[camel(k)] = v
    return out


def _career_stage(cs: dict) -> dict:
    out: dict[str, Any] = {}
    note_parts = []
    for k, v in cs.items():
        if k == "as_of":
            out["asOf"] = v
        elif k == "source":
            out["method"] = {"self_reported": "declared", "inferred": "inferred"}.get(v, v)
        elif k in ("evidence", "notes"):
            if v:
                note_parts.append(v)
        elif k == "sources_checked":
            out["sourcesChecked"] = v
        else:
            out[camel(k)] = v
    if note_parts:
        out["note"] = "\n\n".join(note_parts)
    return out


def _interest(entry: dict, oa_map: dict[str, str], rep: _Report) -> dict:
    out: dict[str, Any] = {}
    for k, v in entry.items():
        if k == "assertedAt":
            out["generatedAt"] = normalize_datetime(v) or v
        elif k == "generator":
            if v == "user" or (isinstance(v, str) and GENERATOR_RE.match(v)):
                out["generator"] = v
            else:
                rep.count("interest generator not convertible (left unknown)")
        elif k == "evidence":
            refs = []
            for w in (v or {}).get("papers", []) or []:
                pid = oa_map.get(str(w).rsplit("/", 1)[-1])
                if pid:
                    refs.append({"paper": pid})
                else:
                    rep.drop("interest evidence id with no matching paper")
            if refs:
                out["derivedFrom"] = refs
            if (v or {}).get("share") is not None:
                out["paperShare"] = v["share"]
        else:
            out[k] = v
    concept = entry.get("concept") or {}
    if concept.get("system"):
        out["method"] = "imported"
        rep.count("interests relabelled imported")
    if out.get("method") == "self_reported":
        out["method"] = "declared"
    return out


def _identity_resolution(anchor: dict) -> dict:
    out: dict[str, Any] = {}
    when = normalize_datetime(anchor.get("created_at"))
    if when:
        out["generatedAt"] = when
    if anchor.get("confidence") is not None:
        out["confidence"] = anchor["confidence"]
    if anchor.get("disambiguation_evidence"):
        out["note"] = anchor["disambiguation_evidence"]
    refs = []
    for url in anchor.get("sources") or []:
        ref = {"url": url}
        if when:
            ref["fetchedAt"] = when
        refs.append(ref)
    if refs:
        out["derivedFrom"] = refs
    return out


# --------------------------------------------------------------------------- files


def _strip_front_matter(text: str) -> tuple[str, dict[str, str]]:
    m = _FRONT_MATTER.match(text)
    if not m:
        return text, {}
    fields = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip().strip("'\"")
    return text[m.end() :], fields


def _camel_tree(value: Any, renames: dict[str, str] | None = None) -> Any:
    renames = renames or {}
    if isinstance(value, dict):
        return {renames.get(k, camel(k)): _camel_tree(v, renames) for k, v in value.items()}
    if isinstance(value, list):
        return [_camel_tree(v, renames) for v in value]
    return value


def _write_json(path: Path, doc: Any) -> bytes:
    data = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(data)
    return data


# --------------------------------------------------------------------------- profile


def migrate_profile(
    src: Path,
    dst: Path,
    *,
    page_id: str | None = None,
    drop_missing: bool = False,
    now: str | None = None,
) -> dict:
    """Migrate the v1 profile at `src` into a new directory `dst`. Returns a report.

    `page_id` is the page UUID to use (`urn:uuid:…` or a bare UUID). Without one a
    random v4 UUID is minted. Pass the same id for every copy of one profile.
    """
    src, dst = Path(src), Path(dst)
    doc = json.loads((src / "profile.jsonld").read_text())
    rep = _Report()

    manifest = list(doc.get("hasPart", [])) + list(doc.get("subjectOf", []))
    missing = [
        e.get("contentUrl")
        for e in manifest
        if e.get("role") not in CACHE_ROLES
        and e.get("contentUrl")
        and not (src / e["contentUrl"]).exists()
    ]
    if missing and not drop_missing:
        raise MigrationError(f"{len(missing)} listed file(s) missing, e.g. {missing[:3]}")

    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

    # papers first: interest evidence resolves through them
    oa_map: dict[str, str] = {}
    works = next((e for e in manifest if e.get("role") == "works"), None)
    if works and (src / works["contentUrl"]).exists():
        wdoc = json.loads((src / works["contentUrl"]).read_text())
        wdoc2, oa_map = _migrate_papers_doc(wdoc, rep)
        _write_json(dst / works["contentUrl"], wdoc2)

    # --- person
    person: dict[str, Any] = {"@type": "Person"}
    rid = doc.get("rid")
    if isinstance(rid, str) and re.match(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", rid):
        person["@id"] = f"https://orcid.org/{rid}"
    old_id = doc.get("@id")
    if old_id and old_id != person.get("@id"):
        rep.drop("Person @id that is not the ORCID IRI")
    for k in _PERSON_KEYS:
        if k in doc:
            v = doc[k]
            if k in ("career", "training"):
                v = [_span(e) for e in v or []]
            elif k == "rp:researchInterests":
                v = [_interest(e, oa_map, rep) for e in v or []]
                rep.count("interests", len(v))
            elif k == "researchOutputs":
                v = [_research_output(o) if isinstance(o, dict) else o for o in v or []]
            person[camel(k)] = _camel_tree(v) if k == "paper_stats" else v
    if "affiliation" in doc:
        aff = doc["affiliation"]
        person["affiliation"] = {"name": aff} if isinstance(aff, str) else aff
    if "summary" in doc:
        person["summary"] = _text_part(doc["summary"])
    for k in _TEXT_LISTS:
        if k in doc:
            person[camel(k)] = [_text_part(x) for x in doc[k] or []]
    if "collaborators" in doc:
        person["collaborators"] = [_collaborator(c, rep) for c in doc["collaborators"] or []]
    if "career_stage" in doc and doc["career_stage"]:
        person["careerStage"] = _career_stage(doc["career_stage"])

    # --- page
    if page_id:
        pid = page_id if page_id.startswith("urn:uuid:") else f"urn:uuid:{page_id}"
    else:
        pid = f"urn:uuid:{uuid.uuid4()}"
    page: dict[str, Any] = {
        "@context": V2_CONTEXT,
        "@type": "ProfilePage",
        "@id": pid,
        "conformsTo": V2_CONTEXT,
        "dateModified": normalize_datetime(doc.get("dateModified")) or (now or _now()),
    }
    for k in _PAGE_KEYS:
        if k in doc:
            page[k] = doc[k]
    prov = doc.get("provenance")
    if prov == "self_published" and rid:
        page["publisher"] = {"rid": rid}
    elif prov == "synthetic":
        page["synthetic"] = True
    if doc.get("provenanceNote"):
        page["note"] = doc["provenanceNote"]
    if isinstance(page.get("proof"), list):
        for p in page["proof"]:
            if p.get("kind") == "orcid_roundtrip" or p.get("type") == "orcid_roundtrip":
                p["kind" if "kind" in p else "type"] = "orcid_verified"
    if isinstance(doc.get("anchor"), dict):
        page["identityResolution"] = _identity_resolution(doc["anchor"])
    for k in _DROPPED_FLAGS:
        if k in doc:
            rep.drop(k)
    for k in _OFF_SCHEMA:
        if k in doc:
            rep.drop(f"off-schema key {k}")

    # --- manifest
    entries = []
    for e in manifest:
        role = e.get("role")
        url = e.get("contentUrl")
        if role in CACHE_ROLES:
            rep.drop(f"cache part {role}")
            continue
        if url in missing:
            rep.drop("entry whose file is missing")
            continue
        entry = dict(e)
        path = dst / url if url else None
        if role == "paper_summary" and path and path.exists():
            body, fm = _strip_front_matter(path.read_text(errors="replace"))
            path.write_text(body)
            if fm.get("source_kind"):
                entry["sourceType"] = fm["source_kind"]
            if fm.get("source_hash"):
                entry["inputsDigest"] = fm["source_hash"]
            if normalize_datetime(fm.get("written_at")):
                entry["generatedAt"] = normalize_datetime(fm["written_at"])
            rep.count("summaries")
            if fm:
                rep.count("summaries with front matter")
        elif role == "embedding_index" and path and path.exists():
            idx = json.loads(path.read_text())
            _write_json(path, _camel_tree(idx))
            for chunks in path.parent.glob("*.chunks.json"):
                data = json.loads(chunks.read_text())
                _write_json(
                    chunks,
                    _camel_tree(data, {"source_type": "sourceRole", "source_id": "sourceKey"}),
                )
        if path and path.exists():
            data = path.read_bytes()
            entry["bytes"] = len(data)
            entry["sha256"] = sha256_bytes(data)
        entries.append(entry)
    page["hasPart"] = entries
    page["mainEntity"] = person

    _write_json(dst / "profile.jsonld", page)
    rep.count("manifest entries", len(entries))
    return {"page_id": pid, **rep.as_dict()}
