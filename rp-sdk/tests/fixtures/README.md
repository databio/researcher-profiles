# Test fixtures

These directories are **inputs, not scratch space**. Nothing in the suite may
write into this tree: a session-scoped guard in `tests/conftest.py` hashes it at
both ends and fails the run if a byte changed. Every test that needs a profile
it can modify gets a copy, from `tests/factories.copy_fixture` or one of the
fixtures below.

## Profile fixtures

| Directory | Shape | Served by |
|---|---|---|
| `jane-doe/` | A complete `full` profile: papers, summaries, persona, citations, full texts. The default subject of most tests. | `jane_doe` / `jane_doe_dir` (writable copy), `jane_doe_readonly` (session-wide copy, assert-only) |
| `john-smith/` | A second complete `full` profile, with schema variation (`career`). Anything needing two profiles. | `fixture_profile("john-smith")`, `fixture_profiles_root("jane-doe", "john-smith")` |
| `incomplete-profile/` | A profile with no synthesized persona: the persona guard's negative case. | `fixture_profile("incomplete-profile")` |
| `.build/<slug>/` | Each profile's build sidecar (`meta/build_state.json`). Outside the content root; copied alongside its profile by default. | `copy_fixture(..., with_build=True)`, the default |
| `.cache/` | The store-wide cache dir: a prebuilt `index.json` rid lookup, for rid-lookup paths. | read directly |

## Corpora (asserted against, never constructed)

| Directory | What it is |
|---|---|
| `published/` | The consumer-skill corpus: broken, hostile, bracket-free and open-vocab published directories (`broken-artifacts`, `hostile`, `not-a-profile`, …). Exercises a *consumer* against documents that lie. Reached as `tests/test_spec_docs.py::FIXTURES_ROOT`. |
| `openalex/` | Raw OpenAlex work records paired with their expected `PaperRecord` output (`*.work.json` / `*.golden.json`). Pins the parser's output contract. Reached as `tests/test_openalex_parser.py::_OPENALEX_FIXTURES`. |

`published/` is a different corpus from `spec/conformance/`
(`tests/test_validate.py::CORPUS`), which is the format's ratified conformance
suite shared with rp-browser's TypeScript validator. Do not merge them.

## Synthetic profiles

Most tests do not need a committed fixture at all; they need a profile of a
known shape. That comes from `tests/factories.build_profile_dir` (or the
`synthetic_profile` / `indexed_profile` / `flat_indexed_profile`
fixtures in `tests/test_embeddings.py`), not
from a new directory here. Add a fixture directory only when the *committed
bytes* are the thing under test.
