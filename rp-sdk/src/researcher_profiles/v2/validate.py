"""Validate a v2 profile directory against the rules in issues #4-#7.

Errors make the profile invalid. Warnings report things a reader should know
but that are allowed, such as a part changed after it was labelled (its hash no
longer matches, so its `method` reads as unknown) or a record left over for a
part that is gone.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import provenance, sections
from .common import (
    CACHE_ROLES,
    CONFIDENCE,
    DATETIME_RE,
    GENERATOR_RE,
    KEYED_LISTS,
    METHODS,
    ORCID_RE,
    PAPER_ROLES,
    PROSE_ROLES,
    PROVENANCE_FIELDS,
    PROVENANCE_ROLE,
    RECORD_FIELDS,
    SINGLE_FILE_ROLES,
    V2_CONTEXT,
    entry_key,
    is_iri,
    sha256_file,
    value_sha256,
)

_UUID_URN = re.compile(r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LOCAL_RID = re.compile(r"^local:[a-z0-9-]+-[0-9a-f]{6}$")
_DATETIME_KEYS = ("dateModified", "generatedAt", "fetchedAt", "verifiedAt", "at")

_PAGE_KEYS = {
    "@context", "@type", "@id", "conformsTo", "dateModified", "license", "level", "visibility",
    "rp:sectionVisibility", "publisher", "synthetic", "note", "proof", "verifiedAt",
    "identityResolution", "excluded", "hasPart", "mainEntity", "synthesisInputsDigest",
}  # fmt: skip

# v1 keys that must not appear in a v2 document (renamed or dropped by #5-#7).
_V1_KEYS = {
    "subjectOf", "provenance", "provenanceNote", "career_stage", "paper_stats", "not_interests",
    "methodological_commitments", "recurring_positions", "intellectual_lineage", "hasCitationGraph",
    "hasEmbeddingIndex", "expertiseCitesPaperIds", "assertedAt",
}  # fmt: skip

TEXT_LISTS = (
    "expertise",
    "critiques",
    "methodologicalCommitments",
    "recurringPositions",
    "intellectualLineage",
)

_REF_KINDS = ("paper", "url", "file")


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning"
    code: str
    where: str
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


class _V:
    def __init__(self, root: Path, served: bool) -> None:
        self.root = root
        self.served = served
        self.findings: list[Finding] = []
        self.paper_ids: set[str] = set()
        self.roles: dict[str, list[dict]] = {}

    def err(self, code: str, where: str, msg: str) -> None:
        self.findings.append(Finding("error", code, where, msg))

    def warn(self, code: str, where: str, msg: str) -> None:
        self.findings.append(Finding("warning", code, where, msg))

    # ---------------------------------------------------------------- provenance

    def generator_form(self, gen: Any, where: str) -> None:
        if (
            gen is not None
            and gen != "user"
            and not (isinstance(gen, str) and GENERATOR_RE.match(gen))
        ):
            self.err(
                "generator",
                where,
                f"generator {gen!r} is not `user` or `<tool>@<version>[/<model>]`",
            )

    def provenance(
        self,
        part: dict,
        where: str,
        *,
        allow_inherits: bool,
        method: Any = None,
        generator: Any = None,
    ) -> None:
        """Check one record. `method` and `generator` are its effective (inherited) values."""
        if "method" in part and part["method"] is not None and part["method"] not in METHODS:
            self.err("method", where, f"unknown method {part['method']!r}")
        self.generator_form(part.get("generator"), where)
        method = part.get("method", method)
        generator = part.get("generator", generator)
        if generator == "user" and method != "declared":
            self.err("user-not-declared", where, "a part with generator `user` must be `declared`")
        if method == "declared" and generator not in ("user", None):
            self.err("declared-not-user", where, "a `declared` part has generator `user` or null")
        refs = part.get("derivedFrom")
        if refs is not None:
            if not isinstance(refs, list):
                self.err("derivedFrom", where, "derivedFrom must be a list")
            else:
                for i, ref in enumerate(refs):
                    self.reference(ref, f"{where}.derivedFrom[{i}]", allow_inherits)

    def reference(self, ref: Any, where: str, allow_inherits: bool) -> None:
        if not isinstance(ref, dict):
            self.err("ref-shape", where, "a reference is an object")
            return
        kinds = [k for k in _REF_KINDS if k in ref]
        if len(kinds) != 1:
            self.err(
                "ref-shape", where, f"a reference names exactly one of {_REF_KINDS}, got {kinds}"
            )
            return
        if ref.get("inheritsVisibility") and not allow_inherits:
            self.err(
                "inherits-placement",
                where,
                "inheritsVisibility is allowed only on records for files and their sections",
            )
        kind = kinds[0]
        if kind == "paper":
            if self.paper_ids and ref["paper"] not in self.paper_ids:
                self.err("ref-paper", where, f"paper {ref['paper']!r} is not in the works file")
        elif kind == "url":
            if not is_iri(ref["url"]):
                self.err("ref-url", where, f"{ref['url']!r} is not a URL")
        elif kind == "file":
            role = ref["file"]
            if role not in SINGLE_FILE_ROLES:
                self.err(
                    "ref-file-role", where, f"`file` names only single-file roles, not {role!r}"
                )
            elif role not in self.roles and not self.served:
                self.err("ref-file", where, f"no manifest entry with role {role!r}")

    def no_inline(self, item: Any, where: str) -> None:
        if isinstance(item, dict):
            for k in (*PROVENANCE_FIELDS, "assertedAt"):
                if k in item:
                    self.err(
                        "inline-provenance", f"{where}.{k}", f"{k!r} belongs in provenance.json"
                    )

    # ---------------------------------------------------------------- walkers

    def datetimes(self, value: Any, where: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if (
                    k in _DATETIME_KEYS
                    and v is not None
                    and not (isinstance(v, str) and DATETIME_RE.match(v))
                ):
                    self.err(
                        "datetime", f"{where}.{k}", f"{v!r} is not UTC to the second ending in Z"
                    )
                self.datetimes(v, f"{where}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                self.datetimes(v, f"{where}[{i}]")

    def v1_keys(self, value: Any, where: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k in _V1_KEYS:
                    self.err("v1-key", f"{where}.{k}", f"v1 key {k!r} is not used in v2")
                self.v1_keys(v, f"{where}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                self.v1_keys(v, f"{where}[{i}]")


def validate_v2(profile_dir: Path | str, *, served: bool = False) -> list[Finding]:
    """Return every finding for the v2 profile at `profile_dir`. Valid means no `error` findings.

    `served=True` checks a copy served to an audience: manifest entries reduced to
    `role`, `visibility` and `paperId` are accepted, and listed files may be absent.
    """
    root = Path(profile_dir)
    v = _V(root, served)
    try:
        page = json.loads((root / "profile.jsonld").read_text())
    except (OSError, json.JSONDecodeError) as e:
        v.err("unreadable", "profile.jsonld", str(e))
        return v.findings

    # ---- page
    if page.get("@context") != V2_CONTEXT:
        v.err("context", "@context", f"expected {V2_CONTEXT}")
    if page.get("conformsTo") != V2_CONTEXT:
        v.err("conformsTo", "conformsTo", f"expected {V2_CONTEXT}")
    if page.get("@type") != "ProfilePage":
        v.err("type", "@type", "the top level is a ProfilePage")
    if not (isinstance(page.get("@id"), str) and _UUID_URN.match(page["@id"])):
        v.err("page-id", "@id", "the page @id is a urn:uuid")
    if not page.get("dateModified"):
        v.err("dateModified", "dateModified", "the page has a dateModified")
    for k in page:
        if k not in _PAGE_KEYS:
            v.warn("unknown-key", k, f"{k!r} is not a page field")
    if "synthetic" in page and not isinstance(page["synthetic"], bool):
        v.err("synthetic", "synthetic", "synthetic is true or false")

    person = page.get("mainEntity")
    if not isinstance(person, dict) or person.get("@type") != "Person":
        v.err("mainEntity", "mainEntity", "mainEntity is a Person")
        person = {}
    rid = person.get("rid")
    if not (isinstance(rid, str) and (ORCID_RE.match(rid) or _LOCAL_RID.match(rid))):
        v.err("rid", "mainEntity.rid", f"rid {rid!r} is an ORCID or local:<slug>-<hex6>")
    pid = person.get("@id")
    if pid is not None and not (isinstance(rid, str) and pid == f"https://orcid.org/{rid}"):
        v.err(
            "person-id", "mainEntity.@id", "the Person @id is the ORCID IRI of its rid, or absent"
        )

    pub = page.get("publisher")
    if pub is not None:
        if not isinstance(pub, dict):
            v.err("publisher", "publisher", "publisher is an object")
        elif "rid" in pub:
            if pub["rid"] != rid:
                v.err("publisher", "publisher.rid", "a publisher rid refers to the subject")
        elif not pub.get("name"):
            v.err("publisher", "publisher", "a third-party publisher has a name")

    # ---- works file, needed for paper references
    entries = page.get("hasPart")
    if not isinstance(entries, list):
        v.err("hasPart", "hasPart", "the manifest is a list")
        entries = []
    for e in entries:
        v.roles.setdefault(e.get("role"), []).append(e)
    works = (v.roles.get("works") or [None])[0]
    if works and works.get("contentUrl") and (root / works["contentUrl"]).exists():
        wdoc = json.loads((root / works["contentUrl"]).read_text())
        seen: set[str] = set()
        for i, r in enumerate(wdoc.get("hasPart", [])):
            p = r.get("paperId")
            if not p:
                v.err("paperId", f"works[{i}]", "a paper record has a paperId")
            elif p in seen:
                v.err(
                    "duplicate-paperId",
                    f"works[{i}]",
                    f"paperId {p!r} appears twice; ids are unique within a profile",
                )
            seen.add(p)
            if "paper_id" in r or "first_author" in r:
                v.err("v1-key", f"works[{i}]", "paper record still has v1 keys")
        v.paper_ids = seen

    # ---- prose sections
    scans: dict[str, sections.SectionScan] = {}
    for role in PROSE_ROLES:
        for e in v.roles.get(role, []):
            path = root / e["contentUrl"] if e.get("contentUrl") else None
            if path and path.exists():
                s = sections.scan(path.read_text(errors="replace"))
                scans[role] = s
                for d in s.duplicates:
                    v.err("section-duplicate", role, f"two level-2 headings read {d!r}")
                if s.empty:
                    v.err("section-empty", role, "a level-2 heading has no text")

    # ---- manifest
    for i, e in enumerate(entries):
        where = f"hasPart[{i}]({e.get('role')})"
        role = e.get("role")
        if role in CACHE_ROLES:
            v.err("cache-part", where, "local caches are not listed as parts")
        for k in (*PROVENANCE_FIELDS, "anchors"):
            if k in e:
                v.err("inline-provenance", f"{where}.{k}", f"{k!r} belongs in provenance.json")
        url = e.get("contentUrl")
        if not url:
            if served and role:
                continue  # a reduced entry in a served copy
            v.err("contentUrl", where, "a manifest entry has a contentUrl")
            continue
        path = root / url
        if not path.exists():
            if not served:
                v.err("missing-file", where, f"{url} does not exist")
            continue
        if role == "paper_summary" and path.read_bytes().startswith(b"---\n"):
            v.err("front-matter", where, "summaries carry no front matter in v2")

    # ---- inline content: no provenance keys, plain-string text entries
    if "summary" in person and not isinstance(person["summary"], str):
        v.err("summary", "mainEntity.summary", "summary is a string")
    for key in TEXT_LISTS:
        for i, item in enumerate(person.get(key) or []):
            if not isinstance(item, str):
                v.err("text-entry", f"mainEntity.{key}[{i}]", "an entry is a string")
    for i, c in enumerate(person.get("collaborators") or []):
        if not (isinstance(c, dict) and c.get("name")):
            v.err(
                "collaborator",
                f"mainEntity.collaborators[{i}]",
                "a collaborator is an object with a name",
            )
    for key in ("collaborators", "rp:researchInterests"):
        for i, item in enumerate(person.get(key) or []):
            v.no_inline(item, f"mainEntity.{key}[{i}]")
    cs = person.get("careerStage")
    if isinstance(cs, dict):
        v.no_inline(cs, "mainEntity.careerStage")
        if cs.get("confidence") is not None and cs["confidence"] not in CONFIDENCE:
            v.err(
                "confidence",
                "mainEntity.careerStage.confidence",
                f"confidence is one of {CONFIDENCE}",
            )

    # ---- the provenance file
    prov_entries = v.roles.get(PROVENANCE_ROLE) or []
    if not prov_entries:
        if not served:
            v.err("no-provenance-file", "hasPart", "the manifest lists provenance.json")
    else:
        url = prov_entries[0].get("contentUrl")
        path = root / url if url else None
        if path and path.exists():
            try:
                prov = json.loads(path.read_text())
            except json.JSONDecodeError as e:
                v.err("unreadable", url, str(e))
            else:
                _check_provenance(v, prov, page, person, scans, served)

    ir = page.get("identityResolution")
    if isinstance(ir, dict):
        v.provenance(ir, "identityResolution", allow_inherits=False)
        if ir.get("confidence") is not None and ir["confidence"] not in CONFIDENCE:
            v.err(
                "confidence", "identityResolution.confidence", f"confidence is one of {CONFIDENCE}"
            )

    for i, x in enumerate(page.get("excluded") or []):
        if (
            not isinstance(x, dict)
            or x.get("type") not in ("author", "paper", "page")
            or not is_iri(x.get("id"))
        ):
            v.err(
                "excluded",
                f"excluded[{i}]",
                "an exclusion has a type (author, paper, page) and an IRI id",
            )

    v.datetimes(page, "")
    v.v1_keys(page, "")
    for where, d in (("", page), ("mainEntity", person)):
        if "anchor" in d:
            v.err(
                "v1-key", f"{where}.anchor", "v1 `anchor` is `identityResolution` on the page in v2"
            )
    return v.findings


def _check_provenance(
    v: _V, prov: Any, page: dict, person: dict, scans: dict, served: bool
) -> None:
    where0 = "provenance.json"
    if not isinstance(prov, dict):
        v.err("record", where0, "the provenance file is one record (an object)")
        return
    if prov.get("about") != page.get("@id"):
        v.err("about", f"{where0}.about", "about is the page @id")
    if prov.get("conformsTo") != V2_CONTEXT:
        v.err("conformsTo", f"{where0}.conformsTo", f"expected {V2_CONTEXT}")
    for k in prov:
        if k not in (*RECORD_FIELDS, "about", "conformsTo"):
            v.err("record-key", f"{where0}.{k}", f"{k!r} is not a record field")
    v.provenance(prov, where0, allow_inherits=False)
    for k in ("derivedFrom", "sha256", "edits"):
        if k in prov:
            v.err("record-key", f"{where0}.{k}", f"the root record has no {k}")

    files = {
        e.get("role"): e for e in page.get("hasPart", []) if e.get("role") in SINGLE_FILE_ROLES
    }
    paper_files = {
        (e.get("paperId"), PAPER_ROLES[e["role"]]): e
        for e in page.get("hasPart", [])
        if e.get("role") in PAPER_ROLES and e.get("paperId")
    }
    root_dir = v.root

    def actual_hash(path: tuple[str, ...]) -> str | None | bool:
        """The current hash of the part at `path`; None if it has none; False if it is gone."""
        branch = path[0]
        if branch == "profile":
            field = path[1]
            if field not in person:
                return False
            if len(path) == 2:
                return value_sha256(person[field])
            value = person[field]
            if not isinstance(value, list):
                return False
            for entry in value:
                if entry_key(field, entry) == path[2]:
                    return value_sha256(entry)
            return False
        if branch == "files":
            e = files.get(path[1])
            if e is None:
                return False
            fpath = root_dir / e["contentUrl"] if e.get("contentUrl") else None
            if len(path) == 2:
                return sha256_file(fpath) if fpath and fpath.exists() else None
            s = scans.get(path[1])
            if s is None:
                return False
            sec = s.by_heading().get(path[2])
            return sec.sha256 if sec else False
        if branch == "papers":
            if path[1] not in v.paper_ids:
                return False
            if len(path) == 2:
                return None
            e = paper_files.get((path[1], path[2]))
            if e is None:
                return False
            fpath = root_dir / e["contentUrl"] if e.get("contentUrl") else None
            return sha256_file(fpath) if fpath and fpath.exists() else None
        return False

    for path, rec in provenance.walk(prov):
        where = where0 + "." + ".".join(path)
        depth_ok = (
            (path[0] == "profile" and len(path) <= 3)
            or (path[0] == "files" and len(path) <= 3)
            or (
                path[0] == "papers"
                and (len(path) <= 2 or (len(path) == 3 and path[2] in ("summary", "fulltext")))
            )
        )
        if path[0] not in ("profile", "files", "papers") or not depth_ok:
            v.err("record-path", where, "no part has this address")
            continue
        for k in rec:
            if k not in RECORD_FIELDS:
                v.err("record-key", f"{where}.{k}", f"{k!r} is not a record field")
        if len(path) == 1:
            continue  # a branch: holds only parts
        method = provenance.effective(prov, path, "method")
        generator = provenance.effective(prov, path, "generator")
        allow = path[0] == "files"
        v.provenance(rec, where, allow_inherits=allow, method=method, generator=generator)
        for j, ed in enumerate(rec.get("edits") or []):
            ew = f"{where}.edits[{j}]"
            if not isinstance(ed, dict) or set(ed) - {"by", "at", "sha256"} or not ed.get("by"):
                v.err("edit", ew, "an edit is {by, at, sha256}")
                continue
            v.generator_form(ed["by"], ew)
        content_keyed = path[0] == "profile" and len(path) == 3 and path[1] not in KEYED_LISTS
        current = actual_hash(path)
        grouping = set(rec) <= {"parts"}  # only groups sub-records: labels nothing itself
        if current is False:
            if not served:
                v.warn("leftover-record", where, "no part has this key any more")
            continue
        if grouping:
            continue
        if (
            method is not None
            and not rec.get("sha256")
            and not content_keyed
            and current is not None
        ):
            v.err("sha256-required", where, "a record with a method has a sha256")
        if rec.get("sha256") and current and rec["sha256"] != current:
            v.warn(
                "edited",
                where,
                "the part changed after it was labelled; its method reads as unknown",
            )
    v.datetimes(prov, where0)


def is_valid(findings: list[Finding]) -> bool:
    return not any(f.level == "error" for f in findings)
