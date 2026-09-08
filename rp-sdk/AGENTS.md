# rp: researcher profiles (SDK)

The `researcher-profiles` package (rp-sdk) is the portable, validated
representation of an on-disk researcher profile, plus the read, match and serve
surface: loading, validating, querying, rendering, publishing, and persona
consumption (`.ask` / `.review` / `.innovate` / `.riff`) of an already finished
profile.

Creating a profile's content is out of scope for this package. The SDK's job
starts once a profile directory exists.

## Quick start

```bash
rp list                    # every profile in the local cache
rp where voss-elena           # path to one profile (accepts a slug OR an ORCID)
rp validate <profile>      # does the profile conform to the format?
rp render <profile>        # render index.html + refresh the manifest, in place
rp index <profile>         # build/update the embedding index  ([vectors,st])
rp search <profile> "..."  # query that index                  ([vectors,st])
rp export <profile>        # one text blob + metadata, for a knowledge base
```

The HTTP server has its own entry point, `python -m researcher_profiles.api`.
See `docs/rp-sdk/reference/api.md`.

## Install

scholarcore is not on PyPI, and neither is this package. Install from the
checkout, scholarcore first:

```bash
pip install -e ../scholarcore
pip install -e ".[dev]"
```

## Documentation

`docs/rp-sdk/reference/python-api.md` is generated, not hand written. Docstrings
in `src/researcher_profiles/` are the source of truth for it, including the
`#: comment` style used on most dataclass, SQLModel and pydantic fields, which
`scripts/render_python_api.py` reads directly from source (griffe does not see
plain comments). To change what that page says, edit a docstring (or a `#:`
comment, or a `Field(description=...)`), then regenerate:

```bash
pip install -e ".[llm,client,sql,vectors,st,docs]"   # or the `dev` extra plus `docs`
python scripts/render_python_api.py
```

Do not hand-edit `docs/rp-sdk/reference/python-api.md`; the next regeneration
overwrites it. To change which classes and functions appear, or their order,
edit the `::: module.Class` directive list in `scripts/python-api-directives.md`.
Narrative content (module layout, the storage interface, capability managers, the
export idempotency contract) is hand written and lives in
`docs-dev/rp-sdk/explanation/sdk-architecture.md`.

## Where the cache is

`rp` reads a local profile cache: one directory per researcher, plus rosters and
an index. Resolution order, highest first (every command uses the same one):

1. `--root`
2. `$RP_PROFILES_ROOT`
3. `~/researcher-profiles`

## Identity vs. name

Identity is the rid: an ORCID, or a minted `local:` id for someone without it.
The directory name is only a display handle. `voss-elena` and
`0000-0002-1825-0097` are both valid directory names and both occur.
`<root>/.cache/index.json` maps between them. Never infer meaning from a directory
name; resolve it:

```bash
rp where 0000-0002-1825-0097     # -> /path/to/voss-elena
rp seek voss-elena                  # -> /path/to/voss-elena   (for $(...) pipelines)
```

## Tests

```bash
CUDA_VISIBLE_DEVICES="" python -m pytest -q
```

The supported install is `pip install -e ".[dev]"`. Its `dev` extra pulls in the
`api`, `vectors`, `client`, and `sql` tiers, so the suite exercises the real
FastAPI app, the sqlite-vec / numpy analytics, the httpx remote client, and the
SQL profile store (on SQLite).

Storage and stores are two interfaces. `researcher_profiles.storage` holds
`ArtifactStorage`, an ABC for one profile's backing, and `DirectoryArtifactStorage`. Both are
core. A profile composes a storage; a backend implements `ArtifactStorage` and
never subclasses `ResearcherProfile`. One level up, `researcher_profiles.store`
holds the `ProfileStore` protocol (a set of profiles) and the filesystem store;
both are also core. `store/sql/` holds `SqlArtifactStorage` and `SqlProfileStore` behind
the `sql` tier: a peer backing store a whole profile can live in, not a
projection of a directory. `client/` holds the two read-only HTTP backends.
`create_app` takes a store, never a path.

Capabilities are composed too. `prof.persona`, `.index`, `.cite`, `.coverage`,
`.topics` and `.edit` are manager objects, each built lazily on first access.
Nothing monkey-patches methods onto `ResearcherProfile`, and a guardrail greps
`src/` to keep it that way. `ResearcherProfile` itself is a package,
`profile/__init__.py`, mirroring `analytics/`: one file per manager sits beside it
(`profile/persona.py`, `profile/index.py`, `profile/cite.py`,
`profile/coverage.py`, `profile/topics.py`, `profile/edit.py`), each holding only
the thin manager class. The heavy logic each one calls into stays in
`generative/llm.py`, `generative/core.py`, `generative/chat.py`, `embeddings/`,
`citations.py`, `coverage.py` and
`topics.py`.

Two guardrails in `test_guardrails.py` protect that shape. `db.py` and `store/sql/` are the only modules allowed to import
sqlmodel/sqlalchemy, and `import researcher_profiles` must not pull SQLAlchemy
even on a machine that has the extra. The SQL names are therefore resolved by
PEP 562 `__getattr__` on first access, never by a module-scope
`try/except ImportError`, which defers nothing.

`tests/test_store.py` owns the protocol and the filesystem backend, and
parametrizes its contract over both stores. `tests/test_db.py` owns the `rp_*`
schema and the SQL backend's specifics.

Mask CUDA: on a host whose GPU is incompatible with the installed torch,
`tests/test_embeddings.py` fails for that reason alone.

One setup layer, in two files. Builders used by more than one test module
(document writers, the `build_profile_dir` / `ProfilesTree` factories,
`FakeBackend`, the LLM stubs, `copy_fixture`) live in `tests/factories.py` as
plain functions, importable from any subdirectory. Fixtures live in
`tests/conftest.py` as thin wrappers over them. Four rules follow:

- A helper with exactly one consumer does not live in the setup layer. It
  belongs in the module that uses it, as an underscore-prefixed local. Promote
  it on the second consumer, not in advance.
- No test writes into `tests/fixtures/`. Take a copy (`fixture_profile`,
  `jane_doe`, `copy_fixture`). A session-scoped digest guard fails the run if
  the tree changed. See `tests/fixtures/README.md` for what each one is.
- No test module imports another test module, and nothing imports a conftest. If
  two files need the same helper, it belongs in `factories.py`.
- `conftest.py` imports no optional dependency at module scope. Optional imports
  go inside fixture bodies, so it stays importable on any install.

Malformed and hostile payloads stay written out inline in the test that
is about them. A helper that can produce them is a helper that can hide them.

### Where a test goes

One domain file per source domain, named for the domain. `tests/test_*.py`
mirrors `src/researcher_profiles/`: `test_api.py` owns the HTTP surface and its
clients, `test_profile.py` owns a profile directory and its sidecars,
`test_llm.py` owns everything that calls a model, and so on. Within a file, each
area is a class (`TestPush`, `TestManifest`, and so on), which is also what stops
two same-named tests from silently shadowing each other. A new test goes in the
file named for the module it exercises, as a method on the class for that area,
not in a new flat file.

Subdirectories exist only for suites with a run condition of their own, declared
in that directory's `conftest.py`. `tests/integration/` auto-marks its items
`integration`, which `addopts` deselects by default; `../scripts/test-integration.sh`
passes `-m integration` to run them. A directory with no conftest and no run
condition does not earn one: its files belong at the top level, named for their
domain.

Adding a test file means "this is a new source domain", and it needs a
matching module under `src/`. If no existing file fits, the domain
is wrong, not the file list.

The one family of exceptions is the package-level guardrails, which are about
the distribution rather than about any module under `src/`:
`test_guardrails.py` (runtime shape: import cost, the `rp` command, the
`@context` and model parity, the slug rule), `test_packaging.py` (what the built
wheel and sdist contain), and `test_spec_docs.py` (SKILL.md, the repo-root
`spec/` examples, the published fixture corpus). These three are the whole
family. A new guardrail goes on a class inside one of them; it does not earn a
fourth file.

### What to test

Public entry points, not internal helpers. The entry points are the CLI
(`main([...])`), the HTTP API (`TestClient` over `create_app`), and the
documented Python API (`ResearcherProfile`, `validate_*`, `render_profile`,
`build_site`, `ProfileStore` and its analytics accessors, `render_export_text`
/ `build_export_bundle`).
A private helper is covered through the entry point that uses it; if it needs
its own test to be trustworthy, it wants to be public.

Do not test that a function returns the type it is annotated to return.

### What earns a test

A test earns its place by pinning something we must be able to control:
something an outside consumer, another repo, or a future us would break by
accident. Everything else is noise that makes a real failure harder to find.

Keep a test that pins:

- an on-disk format: `profile.jsonld` and its manifest, `papers.jsonld`,
  `grants.jsonld`, the flat `embeddings/` form, `.publishignore`, the
  `build_site` collection files, or the byte-level canonical JSON-LD
  serialization;
- a privacy or refusal guarantee: what never leaves the machine, what never
  reaches the published tree, when the model is not called;
- a CLI exit code or machine-readable output shape (see Exit codes);
- a cross-tool contract: the consumer skill, the static client, the HTTP API,
  the OpenAlex parser output contract, the shared conformance corpus;
- a guardrail: import cost, packaging (the wheel ships the skill tree and the
  JSON-LD context), the frozen `v1` context lock.

Delete a test that is:

- a redundant angle on a contract already pinned elsewhere;
- an internal helper's own test when a public entry point already covers it;
- an assertion about error-message wording where the error type or the exit code
  is the contract (naming which rule fired via `match=` is fine);
- mock in, mock out: it asserts back the value the mock was handed;
- an exhaustive flag or mode sweep where one representative pins the parsing;
- an assertion that cannot fail, or that restates its own arithmetic or its own
  constant;
- a `hasattr` / `isinstance` / `repr` / "returns `None`" check.

Coverage is not the goal. An uncovered private helper whose public entry point
is tested is correctly uncovered.

### How to add a case

Prefer `pytest.mark.parametrize` over a copy-pasted variant. If a new test would
be an existing test with one value changed, it is a new row in that test's table,
not a new function. Always pass `ids=`: the id is what a failure reads as, so it
carries the intent a function name would otherwise carry.

Add a new function only when the assertion shape differs: a different exception,
a different code path, a claim the existing test does not make.

## Exit codes

`0` ok, `1` general error, `2` invalid usage, `4` validation/contract failure
