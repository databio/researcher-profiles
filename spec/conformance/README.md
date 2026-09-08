# Conformance corpus

A shared set of source-layout profile fixtures that both researcher-profiles
validators run over, so they can never silently disagree.

## Why this exists

Two validators check profiles, and they encode overlapping rules in different
languages:

- CLI: `researcher_profiles.validate.validate_profile_dir` (Python, pydantic)
  validates a directory on disk.
- Explorer: `rp-browser/src/validate/runner.ts` (TypeScript) validates a
  profile served over HTTP in the browser.

Because the rules were written independently, they drift and develop gaps. This
already caused real bugs (validating one document against the wrong schema; the
explorer mishandling a relative `@id`; the explorer JSON-parsing markdown/HTML
artifacts and failing every valid full profile). A single valid-profile fixture
run through both validators catches all of these before release.

This corpus is that fixture set. Both validators run over the same
directories in CI and each verdict is asserted against `cases.json`:

- `tests/test_validate.py::TestConformanceCorpus` drives the CLI validator.
- `rp-browser/tests/conformance.test.ts` drives the explorer validator (with
  a stub `fetch` that serves these files from disk).
- `.github/workflows/conformance.yml` runs both on every relevant change.

## The format

A profile is a single document. `profile.jsonld` is one `schema:Person`
node (a `ProfileDocument`) that is simultaneously the record, the crawler
payload, and the file manifest: its `hasPart` and `subjectOf` arrays list every
other file in the directory as a typed `ArtifactRef` (`role`, a `contentUrl` that is
always relative, `encodingFormat`, `name`, and optionally `paperId`). A
profile directory is a source layout, not a flattened published tree:

```
valid/ada-lovelace/
  profile.jsonld              # the Person document + manifest
  sources/papers.jsonld       # a Collection of ScholarlyArticle nodes
  sources/summaries/*.summary.md
  sources/citations.json      # only in the -cited case
  personality/SOUL.md
  personality/expertise.md
```

`conformsTo` is a string (the context IRI) and is the format gate.

## Layout

```
spec/conformance/
  cases.json          # every case + expected verdict per validator
  valid/<slug>/       # profile dirs that MUST pass
  invalid/<slug>/     # profile dirs that MUST fail (with a reason)
```

Each `cases.json` entry names the directory and the expected verdict for each
validator:

```json
{
  "dir": "invalid/profile-missing-name",
  "description": "profile.jsonld omits the required `name`.",
  "cli":      { "valid": false, "expect_fail": ["missing"] },
  "explorer": { "valid": false, "expect_fail": ["profile-schema"] }
}
```

`expect_fail` is an optional hint: for the CLI it matches against Pydantic error
types / schema names / JSON pointers; for the explorer it matches a check `id`.

## Two kinds of rule (why some cases pass one validator, fail the other)

Shape rules (the structure of the JSON documents) have a single source of
truth: the pydantic models in `researcher_profiles.schema` (`ProfileDocument`, `PapersDocument`).
`schema_export.py` generates the JSON Schemas from them and the explorer's
TypeScript types derive from that same schema. Shape violations are caught by
both validators, so those cases are `valid:false` for both.

Environment-specific checks legitimately live in only one validator and have
no cross-language equivalent:

- Explorer only: reachability, CORS, Content-Type, byte size, real-404,
  embedding-dim, relative-`@id`-resolves-to-base.
- CLI only: on-disk cross-artifact invariants and per-node papers-graph shape.
  (`invalid/papers-bad-node` exercises this: a bad `ScholarlyArticle` node in
  `sources/papers.jsonld` fails the CLI's `PapersDocument` schema, while the
  explorer only reachability-checks that file.)

`invalid/dangling-artifact` fails both, for different reasons: the CLI's
cross-artifact checks flag a `hasPart` entry whose `contentUrl` file is missing,
and the explorer's reachability check flags the unresolvable URL.

Cases valid for one validator and invalid for the other are documented in
`cases.json`, not bugs. The corpus keeps these honest: if a validator's
coverage regresses, its half of the case flips and CI fails.

## Regenerating

The `valid/` fixtures are hand-authored source-layout profiles whose
`profile.jsonld` manifest is generated from disk with
`researcher_profiles.manifest.build_manifest`, so `hasPart`/`subjectOf` always
match the files present. The `invalid/` cases are derived by copying a valid
case and mutating one thing (dropping `name`, breaking `conformsTo`, removing a
paper node's `name`, or adding a manifest entry with no file).

When adding a case, put the directory under `valid/` or `invalid/` and add an
entry to `cases.json` with the expected verdict for each validator. Verify the
CLI verdict with:

```python
from researcher_profiles.validate import validate_profile_dir

print(validate_profile_dir("spec/conformance/valid/ada-lovelace").ok)
```
