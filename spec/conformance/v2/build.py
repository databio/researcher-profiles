"""Regenerate the v2 conformance cases from the v1 fixture `rp-sdk/tests/fixtures/jane-doe`.

    python spec/conformance/v2/build.py

Cases (listed with their expected verdicts in cases.json):

- valid/jane-doe-migrated: `rp migrate` output, with a fixed UUID. Its
  `provenance.json` has a null root, as after any migration.
- valid/jane-doe-rebuilt: the worked example. A rebuild has labelled the build
  in the root record, recorded copied files as `imported`, stamped one record
  per `expertise.md` section, and a person has since rewritten one section
  (`declared`, with an `edits` entry).
- valid/jane-doe-hand-edited: the rebuilt profile with a sentence added to a
  section by hand. Valid, with `edited` warnings.
- valid/declared-critique: a `critiques` entry a person declared, keyed by its
  content hash.
- valid/leftover-record: a record for a heading that no longer exists, with a
  `leftover-record` warning.
- valid/excluded: an excluded author and an excluded paper.
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

from researcher_profiles.v2 import migrate_profile, provenance, sections  # noqa: E402
from researcher_profiles.v2.common import PAPER_ROLES, content_key, value_sha256  # noqa: E402
from researcher_profiles.v2.stamp import stamp_sections  # noqa: E402

V1 = REPO / "rp-sdk" / "tests" / "fixtures" / "jane-doe"
PAGE_ID = "urn:uuid:5f0c2a1e-8d3b-4c7a-9e21-6b4f0d9a7c35"
BUILT = "2026-11-13T09:00:00Z"
EDITED = "2026-11-20T10:02:00Z"
GEN = "profile-pipeline@2.0.0/claude-opus-5-5"
COLLECTOR = "openalex-collector@2.0.0"


def load(d):
    return json.loads((d / "profile.jsonld").read_text())


def save(d, page):
    (d / "profile.jsonld").write_text(json.dumps(page, indent=2, ensure_ascii=False) + "\n")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def edit_prov(d, fn):
    prov = provenance.load(d)
    fn(prov)
    provenance.save(d, prov)


def rebuilt(d):
    page = load(d)
    page["synthetic"] = True
    page["publisher"] = {"name": "Example Lab", "url": "https://example.org"}
    save(d, page)

    # The build states itself once in the root record. Copied files are imported;
    # summaries and SOUL.md take the root's labels and need only their hashes.
    prov = provenance.load(d)
    prov.update({"method": "inferred", "generator": GEN, "generatedAt": BUILT})
    for e in page["hasPart"]:
        role, path = e["role"], d / e["contentUrl"]
        if role in PAPER_ROLES:
            rec = provenance.ensure(prov, ("papers", e["paperId"], PAPER_ROLES[role]))
            rec.clear()
            if role == "paper_fulltext":
                rec.update({"method": "imported", "generator": COLLECTOR})
            rec["sha256"] = sha(path)
        elif role in ("works", "citations"):
            provenance.ensure(prov, ("files", role)).update(
                {"method": "imported", "generator": COLLECTOR, "sha256": sha(path)}
            )
        elif role == "soul":
            provenance.ensure(prov, ("files", role))["sha256"] = sha(path)
    provenance.save(d, prov)
    stamp_sections(d, "expertise", method="inferred", generator=GEN, generated_at=BUILT)

    # A person rewrites one section afterwards.
    page = load(d)
    entry = next(e for e in page["hasPart"] if e["role"] == "expertise")
    path = d / entry["contentUrl"]
    text = path.read_text()
    secs = sections.scan(text).sections
    target = secs[1] if len(secs) > 1 else secs[0]
    data = sections.normalize(text)
    body = data[target.start : target.end].decode()
    new_body = (
        body.rstrip("\n")
        + "\n\nI would put the early methods work first; the later work follows from it.\n\n"
    )
    text = data[: target.start].decode() + new_body + data[target.end :].decode()
    path.write_text(text)
    new = sections.scan(text).by_heading()[target.heading].sha256
    entry["sha256"] = sha(path)
    entry["bytes"] = path.stat().st_size
    page["dateModified"] = EDITED
    save(d, page)

    def declare(prov):
        f = provenance.get(prov, ("files", "expertise"))
        f["sha256"] = sha(path)
        f["parts"][target.heading] = {
            "method": "declared",
            "generator": "user",
            "generatedAt": EDITED,
            "sha256": new,
            "edits": [{"by": "user", "at": EDITED, "sha256": new}],
        }

    edit_prov(d, declare)


def variant(base, sub, name, fn):
    d = HERE / sub / name
    shutil.copytree(base, d)
    page = load(d)
    fn(d, page)
    save(d, page)
    if (d / "provenance.json").exists():
        provenance.save(d, provenance.load(d))  # the manifest entry gets the file's final hash


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

    # ---- valid variants

    def declared_critique(d, page):
        crit = page["mainEntity"]["critiques"]
        crit[0] = "Inconsistent metadata, not missing data, is what blocks reuse."

        def fn(prov):
            provenance.ensure(prov, ("profile", "critiques", content_key(crit[0]))).update(
                {
                    "method": "declared",
                    "generator": "user",
                    "generatedAt": EDITED,
                    "edits": [{"by": "user", "at": EDITED, "sha256": value_sha256(crit[0])}],
                }
            )

        save(d, page)
        edit_prov(d, fn)

    def leftover(d, page):  # noqa: ARG001
        edit_prov(
            d,
            lambda prov: provenance.ensure(prov, ("files", "expertise", "Old heading")).update(
                {"sha256": "0" * 64}
            ),
        )

    def excluded(d, page):  # noqa: ARG001
        page["excluded"] = [
            {"type": "author", "id": "https://openalex.org/A5011111111", "reason": "Different person with the same name"},
            {"type": "paper", "id": "https://doi.org/10.1234/example", "reason": "Earlier preprint of a paper already listed"},
        ]  # fmt: skip

    for name, fn in (
        ("declared-critique", declared_critique),
        ("leftover-record", leftover),
        ("excluded", excluded),
    ):
        variant(rb, "valid", name, fn)

    # ---- invalid variants: one broken rule each

    def no_date(d, page):  # noqa: ARG001
        page.pop("dateModified")

    def method_no_sha(d, page):  # noqa: ARG001
        def fn(prov):
            rec = provenance.get(prov, ("files", "soul"))
            rec["method"] = "inferred"
            rec.pop("sha256")

        edit_prov(d, fn)

    def dup_heading(d, page):  # noqa: ARG001
        p = d / "personality" / "expertise.md"
        first = sections.scan(p.read_text()).sections[0].heading
        p.write_text(p.read_text() + f"\n## {first}\nAgain.\n")

    def user_inferred(d, page):  # noqa: ARG001
        edit_prov(
            d,
            lambda prov: provenance.ensure(prov, ("profile", "summary")).update(
                {
                    "generator": "user",
                    "method": "inferred",
                    "sha256": value_sha256(page["mainEntity"]["summary"]),
                }
            ),
        )

    def v1_key(d, page):  # noqa: ARG001
        page["provenance"] = "third_party"

    def offset_datetime(d, page):  # noqa: ARG001
        page["dateModified"] = "2026-11-20T10:02:00+00:00"

    def dup_paper_id(d, page):
        works = d / next(e for e in page["hasPart"] if e["role"] == "works")["contentUrl"]
        doc = json.loads(works.read_text())
        doc["hasPart"][1]["paperId"] = doc["hasPart"][0]["paperId"]
        works.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        entry = next(e for e in page["hasPart"] if e["role"] == "works")
        entry["sha256"] = sha(works)
        entry["bytes"] = works.stat().st_size

    def no_prov_file(d, page):
        page["hasPart"] = [e for e in page["hasPart"] if e["role"] != "provenance"]
        (d / "provenance.json").unlink()

    def bad_generator(d, page):  # noqa: ARG001
        edit_prov(d, lambda prov: prov.update({"generator": "llm"}))

    def inherits_on_field(d, page):
        edit_prov(
            d,
            lambda prov: provenance.ensure(prov, ("profile", "summary")).update(
                {
                    "sha256": value_sha256(page["mainEntity"]["summary"]),
                    "derivedFrom": [{"file": "works", "inheritsVisibility": True}],
                }
            ),
        )

    for name, fn in (
        ("no-dateModified", no_date),
        ("method-without-sha256", method_no_sha),
        ("duplicate-heading", dup_heading),
        ("user-not-declared", user_inferred),
        ("v1-key", v1_key),
        ("datetime-offset", offset_datetime),
        ("duplicate-paperId", dup_paper_id),
        ("no-provenance-file", no_prov_file),
        ("bad-generator", bad_generator),
        ("inherits-on-field", inherits_on_field),
    ):
        variant(rb, "invalid", name, fn)


if __name__ == "__main__":
    main()
