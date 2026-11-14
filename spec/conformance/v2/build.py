"""Regenerate the v2 conformance cases from the v1 fixture `rp-sdk/tests/fixtures/jane-doe`.

    python spec/conformance/v2/build.py

Cases (listed with their expected verdicts in cases.json):

- valid/jane-doe-migrated: `rp migrate` output, with a fixed UUID. Provenance is
  mostly unknown, as after any migration.
- valid/jane-doe-rebuilt: the worked example. A rebuild has labelled every part
  (page defaults for the build, imported records overriding them), stamped one
  anchor record per `expertise.md` section, and a person has since rewritten one
  section (`declared`).
- valid/jane-doe-hand-edited: the rebuilt profile with a sentence added to a
  section by hand. Valid, with an `edited` warning.
- invalid/*: one broken rule each.
"""

import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "rp-sdk" / "src"))

from researcher_profiles.v2 import anchors, migrate_profile, slug  # noqa: E402

V1 = REPO / "rp-sdk" / "tests" / "fixtures" / "jane-doe"
PAGE_ID = "urn:uuid:5f0c2a1e-8d3b-4c7a-9e21-6b4f0d9a7c35"
BUILT = "2026-11-13T09:00:00Z"
EDITED = "2026-11-20T10:02:00Z"
GEN = "profile-pipeline@2.0.0/claude-opus-5-5"


def load(d):
    return json.loads((d / "profile.jsonld").read_text())


def save(d, page):
    (d / "profile.jsonld").write_text(json.dumps(page, indent=2, ensure_ascii=False) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rebuilt(d):
    page = load(d)
    page["synthetic"] = True
    page["publisher"] = {"name": "Example Lab", "url": "https://example.org"}
    # Fields the migration couldn't fill are made explicitly unknown before page
    # defaults exist, so they don't silently take the build's values.
    for it in page["mainEntity"].get("rp:researchInterests", []):
        it.setdefault("generator", None)
    page["partDefaults"] = {"method": "inferred", "generator": GEN, "generatedAt": BUILT}
    for e in page["hasPart"]:
        e.pop("generatedAt", None)
        if e["role"] in ("works", "citations", "paper_fulltext"):
            e["method"] = "imported"
            e["generator"] = "openalex-collector@2.0.0"
    for key in ("summary",):
        page["mainEntity"][key].pop("method", None)
    save(d, page)
    anchors.stamp(d, "expertise", method="inferred", generator=GEN, generated_at=BUILT)
    # A person rewrites one section afterwards.
    page = load(d)
    entry = next(e for e in page["hasPart"] if e["role"] == "expertise")
    path = d / entry["contentUrl"]
    text = path.read_text()
    secs = slug.scan(text).sections
    target = secs[1] if len(secs) > 1 else secs[0]
    data = slug.normalize(text)
    body = data[target.start : target.end].decode()
    new_body = (
        body.rstrip("\n")
        + "\n\nI would put the early methods work first; the later work follows from it.\n\n"
    )
    text = data[: target.start].decode() + new_body + data[target.end :].decode()
    path.write_text(text)
    new = {s.anchor: s.sha256 for s in slug.scan(text).sections}
    for rec in entry["anchors"]:
        if rec["anchor"] == target.anchor:
            rec.update(
                {
                    "method": "declared",
                    "generator": "user",
                    "generatedAt": EDITED,
                    "sha256": new[target.anchor],
                }
            )
            rec.pop("derivedFrom", None)
    entry["sha256"] = sha(path)
    entry["bytes"] = path.stat().st_size
    page["dateModified"] = EDITED
    save(d, page)


def make_invalid(base, name, fn):
    d = HERE / "invalid" / name
    shutil.copytree(base, d)
    page = load(d)
    fn(d, page)
    save(d, page)


def main():
    for sub in ("valid", "invalid"):
        shutil.rmtree(HERE / sub, ignore_errors=True)
        (HERE / sub).mkdir()
    migrated = HERE / "valid" / "jane-doe-migrated"
    migrate_profile(V1, migrated, page_id=PAGE_ID, now=BUILT)
    rb = HERE / "valid" / "jane-doe-rebuilt"
    shutil.copytree(migrated, rb)
    rebuilt(rb)
    he = HERE / "valid" / "jane-doe-hand-edited"
    shutil.copytree(rb, he)
    p = he / "personality" / "expertise.md"
    p.write_text(p.read_text() + "\nA sentence added by hand.\n")

    def no_date(d, page):  # noqa: ARG001
        page.pop("dateModified")

    def method_no_sha(d, page):  # noqa: ARG001
        next(e for e in page["hasPart"] if e["role"] == "soul").pop("sha256")

    def dup_heading(d, page):  # noqa: ARG001
        p = d / "personality" / "expertise.md"
        first = slug.scan(p.read_text()).sections[0].heading
        p.write_text(p.read_text() + f"\n## {first}\nAgain.\n")

    def user_inferred(d, page):  # noqa: ARG001
        page["mainEntity"]["summary"].update({"generator": "user", "method": "inferred"})

    def v1_key(d, page):  # noqa: ARG001
        page["provenance"] = "third_party"

    def offset_datetime(d, page):  # noqa: ARG001
        page["dateModified"] = "2026-11-20T10:02:00+00:00"

    for name, fn in (
        ("no-dateModified", no_date),
        ("method-without-sha256", method_no_sha),
        ("duplicate-heading", dup_heading),
        ("user-not-declared", user_inferred),
        ("v1-key", v1_key),
        ("datetime-offset", offset_datetime),
    ):
        make_invalid(rb, name, fn)


if __name__ == "__main__":
    main()
