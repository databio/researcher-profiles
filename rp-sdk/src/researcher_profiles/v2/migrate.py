"""`rp migrate`: rewrite a v1 profile directory as v2 (issues #4-#7), offline.

Migration fills in only what the v1 data says. It never invents a generator, a
`declared` label or a fetch date. It writes `provenance.json` with a root record
whose `method`, `generator` and `generatedAt` are null (v1 records no build), and
moves the provenance v1 does record into records there. A record it writes sets
all three fields, null where v1 says nothing, so a later build's root values never
reach it. The source directory is never modified.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import provenance
from .common import (
    CACHE_ROLES,
    GENERATOR_RE,
    PAPER_ROLES,
    SINGLE_FILE_ROLES,
    V2_CONTEXT,
    camel,
    entry_key,
    normalize_datetime,
    sha256_bytes,
    value_sha256,
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


def _record(
    method: Any = None, generator: Any = None, generated_at: Any = None, **rest: Any
) -> dict:
    """A record that states all three inherited fields, null where v1 says nothing."""
    rec = {"method": method, "generator": generator, "generatedAt": generated_at}
    rec.update({k: v for k, v in rest.items() if v is not None})
    return rec


def _generator(value: Any, rep: _Report, what: str) -> str | None:
    if value == "user" or (isinstance(value, str) and GENERATOR_RE.match(value)):
        return value
    if value is not None:
        rep.count(f"{what} generator not convertible (left unknown)")
    return None


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


def _career_stage(cs: dict) -> tuple[dict, dict | None]:
    """The v2 career stage and its record (from v1 `source`), or None when v1 has no source."""
    out: dict[str, Any] = {}
    note_parts = []
    rec = None
    for k, v in cs.items():
        if k == "as_of":
            out["asOf"] = v
        elif k == "source":
            rec = _record({"self_reported": "declared", "inferred": "inferred"}.get(v))
            if rec["method"] == "declared":
                rec["generator"] = "user"
        elif k in ("evidence", "notes"):
            if v:
                note_parts.append(v)
        elif k == "sources_checked":
            out["sourcesChecked"] = v
        else:
            out[camel(k)] = v
    if note_parts:
        out["note"] = "\n\n".join(note_parts)
    return out, rec


def _interest(entry: dict, oa_map: dict[str, str], rep: _Report) -> tuple[dict, dict]:
    """The v2 interest (content only) and its record."""
    out: dict[str, Any] = {}
    method = generator = generated_at = None
    refs: list[dict] = []
    for k, v in entry.items():
        if k == "assertedAt":
            generated_at = normalize_datetime(v) or v
        elif k == "generator":
            generator = _generator(v, rep, "interest")
        elif k == "method":
            method = "declared" if v == "self_reported" else v
        elif k == "evidence":
            for w in (v or {}).get("papers", []) or []:
                pid = oa_map.get(str(w).rsplit("/", 1)[-1])
                if pid:
                    refs.append({"paper": pid})
                else:
                    rep.drop("interest evidence id with no matching paper")
            if (v or {}).get("share") is not None:
                out["paperShare"] = v["share"]
        else:
            out[k] = v
    concept = entry.get("concept") or {}
    if concept.get("system"):
        method = "imported"
        rep.count("interests relabelled imported")
    rec = _record(method, generator, generated_at, derivedFrom=refs or None)
    if method is not None:
        rec["sha256"] = value_sha256(out)
    return out, rec


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
    paper_ids: set[str] = set()
    works = next((e for e in manifest if e.get("role") == "works"), None)
    if works and (src / works["contentUrl"]).exists():
        wdoc = json.loads((src / works["contentUrl"]).read_text())
        wdoc2, oa_map = _migrate_papers_doc(wdoc, rep)
        paper_ids = {r["paperId"] for r in wdoc2["hasPart"] if r.get("paperId")}
        _write_json(dst / works["contentUrl"], wdoc2)

    # --- person
    person: dict[str, Any] = {"@type": "Person"}
    rid = doc.get("rid")
    if isinstance(rid, str) and re.match(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", rid):
        person["@id"] = f"https://orcid.org/{rid}"
    old_id = doc.get("@id")
    if old_id and old_id != person.get("@id"):
        rep.drop("Person @id that is not the ORCID IRI")
    pfile = provenance.new_root("", method=None, generator=None, generatedAt=None)
    for k in _PERSON_KEYS:
        if k in doc:
            v = doc[k]
            if k in ("career", "training"):
                v = [_span(e) for e in v or []]
            elif k == "rp:researchInterests":
                pairs = [_interest(e, oa_map, rep) for e in v or []]
                v = [p[0] for p in pairs]
                for entry, rec in pairs:
                    provenance.ensure(pfile, ("profile", k, entry_key(k, entry))).update(rec)
                rep.count("interests", len(v))
            elif k == "researchOutputs":
                v = [_research_output(o) if isinstance(o, dict) else o for o in v or []]
            person[camel(k)] = _camel_tree(v) if k == "paper_stats" else v
    if "affiliation" in doc:
        aff = doc["affiliation"]
        person["affiliation"] = {"name": aff} if isinstance(aff, str) else aff
    if "summary" in doc:
        s = doc["summary"]
        person["summary"] = s.get("text") if isinstance(s, dict) else s
    for k in _TEXT_LISTS:
        if k in doc:
            person[camel(k)] = [x.get("text") if isinstance(x, dict) else x for x in doc[k] or []]
    if "collaborators" in doc:
        person["collaborators"] = [_collaborator(c, rep) for c in doc["collaborators"] or []]
    if "career_stage" in doc and doc["career_stage"]:
        cs, rec = _career_stage(doc["career_stage"])
        person["careerStage"] = cs
        if rec is not None:
            if rec["method"] is not None:
                rec["sha256"] = value_sha256(cs)
            provenance.ensure(pfile, ("profile", "careerStage")).update(rec)

    # --- page
    if page_id:
        pid = page_id if page_id.startswith("urn:uuid:") else f"urn:uuid:{page_id}"
    else:
        pid = f"urn:uuid:{uuid.uuid4()}"
    pfile["about"] = pid
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
        rec = _entry_record(entry, paper_ids, rep)
        if role == "paper_summary" and path and path.exists():
            body, fm = _strip_front_matter(path.read_text(errors="replace"))
            path.write_text(body)
            if fm.get("source_kind"):
                entry["sourceType"] = fm["source_kind"]
            if fm.get("source_hash"):
                entry["inputsDigest"] = fm["source_hash"]
            if normalize_datetime(fm.get("written_at")):
                rec = rec or _record()
                rec["generatedAt"] = normalize_datetime(fm["written_at"])
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
            if rec is not None and rec.get("method") is not None:
                rec["sha256"] = entry["sha256"]
        if rec is not None:
            where = _file_path(entry)
            if where:
                provenance.ensure(pfile, where).update(rec)
            else:
                rep.drop("provenance on a file with no record address")
        entries.append(entry)
    page["hasPart"] = entries
    page["mainEntity"] = person

    _write_json(dst / "profile.jsonld", page)
    provenance.save(dst, pfile)
    rep.count("manifest entries", len(entries) + 1)
    return {"page_id": pid, **rep.as_dict()}


def _file_path(entry: dict) -> tuple[str, ...] | None:
    """Where a manifest entry's record lives in the provenance file."""
    role = entry.get("role")
    if role in PAPER_ROLES and entry.get("paperId"):
        return ("papers", entry["paperId"], PAPER_ROLES[role])
    if role in SINGLE_FILE_ROLES:
        return ("files", role)
    return None


def _entry_record(entry: dict, paper_ids: set[str], rep: _Report) -> dict | None:
    """Take any provenance off a v1 manifest entry and return it as a record.

    v1 `derivedFrom` values are roles or paper ids, and every one inherits
    visibility, so each becomes a typed reference with `inheritsVisibility`.
    """
    fields = {
        k: entry.pop(k) for k in ("method", "generator", "generatedAt", "derivedFrom") if k in entry
    }
    if not fields:
        return None
    refs = []
    for v in fields.get("derivedFrom") or []:
        if isinstance(v, str) and v in paper_ids:
            refs.append({"paper": v, "inheritsVisibility": True})
        elif isinstance(v, str) and v in SINGLE_FILE_ROLES:
            refs.append({"file": v, "inheritsVisibility": True})
        else:
            rep.drop("v1 derivedFrom value that names no paper or single-file role")
    return _record(
        fields.get("method"),
        _generator(fields.get("generator"), rep, "file"),
        normalize_datetime(fields.get("generatedAt")),
        derivedFrom=refs or None,
    )
