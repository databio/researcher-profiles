"""Validate a v2 profile directory against the rules in issues #4-#7.

Errors make the profile invalid. Warnings report things a reader should know
but that are allowed, such as a file edited after it was labelled (its hash no
longer matches, so its `method` reads as unknown).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import slug
from .common import (
    ANCHOR_ROLES,
    CACHE_ROLES,
    CONFIDENCE,
    DATETIME_RE,
    DEFAULTABLE,
    GENERATOR_RE,
    METHODS,
    ORCID_RE,
    SINGLE_FILE_ROLES,
    V2_CONTEXT,
    is_iri,
    sha256_file,
)

_UUID_URN = re.compile(r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LOCAL_RID = re.compile(r"^local:[a-z0-9-]+-[0-9a-f]{6}$")
_DATETIME_KEYS = ("dateModified", "generatedAt", "fetchedAt", "verifiedAt")

_PAGE_KEYS = {
    "@context", "@type", "@id", "conformsTo", "dateModified", "license", "level", "visibility",
    "rp:sectionVisibility", "publisher", "synthetic", "note", "proof", "verifiedAt", "partDefaults",
    "identityResolution", "excluded", "hasPart", "mainEntity", "synthesisInputsDigest",
}  # fmt: skip

# v1 keys that must not appear in a v2 document (renamed or dropped by #5-#7).
_V1_KEYS = {
    "subjectOf", "provenance", "provenanceNote", "career_stage", "paper_stats", "not_interests",
    "methodological_commitments", "recurring_positions", "intellectual_lineage", "hasCitationGraph",
    "hasEmbeddingIndex", "expertiseCitesPaperIds", "assertedAt",
}  # fmt: skip

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
        self.defaults: dict[str, Any] = {}
        self.paper_ids: set[str] = set()
        self.roles: dict[str, list[dict]] = {}
        self.anchors_by_role: dict[str, set[str]] = {}

    def err(self, code: str, where: str, msg: str) -> None:
        self.findings.append(Finding("error", code, where, msg))

    def warn(self, code: str, where: str, msg: str) -> None:
        self.findings.append(Finding("warning", code, where, msg))

    # ---------------------------------------------------------------- provenance

    def effective(self, part: dict, key: str) -> Any:
        return part[key] if key in part else self.defaults.get(key)

    def provenance(self, part: dict, where: str, *, allow_inherits: bool) -> None:
        if "method" in part and part["method"] is not None and part["method"] not in METHODS:
            self.err("method", where, f"unknown method {part['method']!r}")
        gen = part.get("generator")
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
        method, generator = self.effective(part, "method"), self.effective(part, "generator")
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
                "inheritsVisibility is allowed only on manifest entries and anchor records",
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
            if (
                "anchor" in ref
                and role in self.anchors_by_role
                and ref["anchor"] not in self.anchors_by_role[role]
            ):
                self.err("ref-anchor", where, f"no heading {ref['anchor']!r} in {role}")

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

    defaults = page.get("partDefaults") or {}
    if not isinstance(defaults, dict) or any(k not in DEFAULTABLE for k in defaults):
        v.err("partDefaults", "partDefaults", f"partDefaults may hold only {DEFAULTABLE}")
        defaults = {}
    v.defaults = defaults
    v.provenance(defaults, "partDefaults", allow_inherits=False)

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
                v.warn("duplicate-paperId", f"works[{i}]", f"paperId {p!r} appears twice")
            seen.add(p)
            if "paper_id" in r or "first_author" in r:
                v.err("v1-key", f"works[{i}]", "paper record still has v1 keys")
        v.paper_ids = seen

    # ---- anchors per prose file (needed before references resolve)
    scans: dict[str, slug.SectionScan] = {}
    for role in ANCHOR_ROLES:
        for e in v.roles.get(role, []):
            path = root / e["contentUrl"] if e.get("contentUrl") else None
            if path and path.exists():
                s = slug.scan(path.read_text(errors="replace"))
                scans[role] = s
                v.anchors_by_role[role] = {sec.anchor for sec in s.sections}

    # ---- manifest
    for i, e in enumerate(entries):
        where = f"hasPart[{i}]({e.get('role')})"
        role = e.get("role")
        if role in CACHE_ROLES:
            v.err("cache-part", where, "local caches are not listed as parts")
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
        v.provenance(e, where, allow_inherits=True)
        if v.effective(e, "method") is not None and not e.get("sha256"):
            v.err("sha256-required", where, "an entry with a method has a sha256")
        if e.get("sha256") and sha256_file(path) != e["sha256"]:
            v.warn(
                "edited",
                where,
                "the file changed after it was labelled; its method reads as unknown",
            )
        if role == "paper_summary" and path.read_bytes().startswith(b"---\n"):
            v.err("front-matter", where, "summaries carry no front matter in v2")
        if "anchors" in e:
            if role not in ANCHOR_ROLES:
                v.err("anchors-role", where, f"only {ANCHOR_ROLES} carry anchors")
                continue
            s = scans.get(role)
            if s is None:
                continue
            for d in s.duplicates:
                v.err("anchor-duplicate", where, f"two headings share the anchor {d!r}")
            for h in s.empty:
                v.err("anchor-empty", where, f"heading {h!r} has an empty anchor")
            by_anchor = {sec.anchor: sec for sec in s.sections}
            recorded = set()
            for j, rec in enumerate(e["anchors"]):
                aw = f"{where}.anchors[{j}]"
                a = rec.get("anchor")
                recorded.add(a)
                if a not in by_anchor:
                    v.err("anchor-unmatched", aw, f"no heading with anchor {a!r}")
                    continue
                v.provenance(rec, aw, allow_inherits=True)
                if v.effective(rec, "method") is not None and not rec.get("sha256"):
                    v.err("sha256-required", aw, "an anchor record with a method has a sha256")
                if rec.get("sha256") and rec["sha256"] != by_anchor[a].sha256:
                    v.warn(
                        "edited",
                        aw,
                        "the section changed after it was labelled; its method reads as unknown",
                    )
            for a in by_anchor:
                if a not in recorded:
                    v.err("anchor-unrecorded", where, f"heading {a!r} has no anchor record")

    # ---- inline parts
    if isinstance(person.get("summary"), dict):
        v.provenance(person["summary"], "mainEntity.summary", allow_inherits=False)
    elif "summary" in person:
        v.err("summary", "mainEntity.summary", "summary is {text, <provenance>}")
    for key in (
        "expertise",
        "critiques",
        "methodologicalCommitments",
        "recurringPositions",
        "intellectualLineage",
    ):
        for i, item in enumerate(person.get(key) or []):
            if not (isinstance(item, dict) and "text" in item):
                v.err("text-part", f"mainEntity.{key}[{i}]", "an entry is {text, <provenance>}")
            else:
                v.provenance(item, f"mainEntity.{key}[{i}]", allow_inherits=False)
    for i, c in enumerate(person.get("collaborators") or []):
        if not (isinstance(c, dict) and c.get("name")):
            v.err(
                "collaborator",
                f"mainEntity.collaborators[{i}]",
                "a collaborator is an object with a name",
            )
        else:
            v.provenance(c, f"mainEntity.collaborators[{i}]", allow_inherits=False)
    for i, it in enumerate(person.get("rp:researchInterests") or []):
        v.provenance(it, f"mainEntity.rp:researchInterests[{i}]", allow_inherits=False)
    cs = person.get("careerStage")
    if isinstance(cs, dict):
        v.provenance(cs, "mainEntity.careerStage", allow_inherits=False)
        if cs.get("confidence") is not None and cs["confidence"] not in CONFIDENCE:
            v.err(
                "confidence",
                "mainEntity.careerStage.confidence",
                f"confidence is one of {CONFIDENCE}",
            )
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


def is_valid(findings: list[Finding]) -> bool:
    return not any(f.level == "error" for f in findings)
