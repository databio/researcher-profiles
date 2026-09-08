# researcher-profiles: component map

This repo is a small monorepo of named components. Work inside the component you
are changing; each has its own README, and rp-sdk has its own AGENTS.md with the
SDK-specific conventions (tests, fixtures, exit codes).

| Path | Component | Language |
|---|---|---|
| `spec/` | The format specification's non-prose assets (the conformance corpus) | data |
| `docs/` | The published documentation, including the spec prose in `docs/rp-spec/` | markdown |
| `docs-dev/` | Maintainer notes not published in the docs nav | markdown |
| `rp-sdk/` | The `researcher-profiles` Python package: models, validation, serving, the `rp` CLI | Python `>=3.12` |
| `scholarcore/` | The `scholarcore` Python package: shared academic vocabulary (Person, Paper, Award, Opportunity) | Python `>=3.12` |
| `rp-browser/` | Browse, search and validate SPA | React + Vite |
| `rp-ui-lib/` | Presentational viewer component library, consumed as a peer dependency | React |

Neither Python package is on PyPI. Install both from a checkout, scholarcore
first, because rp-sdk depends on it:

```bash
pip install -e ./scholarcore
pip install -e "./rp-sdk[dev]"
```

## scholarcore is a separate distribution

scholarcore is not part of rp-sdk and must never become one. It has its own
`pyproject.toml`, its own `requires-python`, and pydantic as its only
dependency. That minimalism is the point: it lets any tool depend on the shared
person and paper vocabulary without pulling in an SDK. It does not import
`researcher_profiles`; `researcher_profiles.schema` does import it. The two
share this repo because the person record is the join point, not because they
are one product.

scholarcore does not rebase RP's models, and RP's models do not rebase onto
scholarcore's. They diverge: RP pins DOI case, has a `funded` grant
status, and stores year-only dates as strings, while scholarcore lowercases
DOIs, has no `funded`, and uses real `date` objects. Merging the two shapes
would corrupt stored profiles. Convergence, if any, is limited to re-exporting
identity helpers.

## Where the spec lives

The format specification's prose is in `docs/rp-spec/`; its non-prose assets
(the conformance corpus) are in `spec/`. The spec is implementation
independent. Do not move it into rp-sdk.

The frozen JSON-LD `@context` is the exception: it ships inside the package, at
`rp-sdk/src/researcher_profiles/context/`. That is the single canonical copy and
it is guarded by a byte lock.

## Cross-component dependencies (what breaks what)

- rp-browser imports rp-ui-lib through the `@rp/ui-lib` alias
  (`rp-browser/vite.config.ts` and `tsconfig.json`, both pointing at
  `../rp-ui-lib/`).
- rp-browser imports rp-sdk's generated JSON Schemas through the `@rp/schemas`
  alias (same two files, pointing at `../rp-sdk/schemas/`). `rp-sdk/schemas/` is
  the only tracked copy: `rp schema export schemas/` writes it and
  `test_schema.py` pins it byte for byte. Never check a second copy into a
  consumer.
- `rp-ui-lib/src/types.ts` is generated from rp-sdk's pydantic wire models
  (`npm run gen:types` in rp-ui-lib). Never hand-edit it.
- `rp-ui-lib/src/lib/` and `src/types.ts` are the vendorable surface. A consumer
  may copy those files wholesale, so moving or renaming them is a breaking
  change for anyone who has.
- `rp-sdk/src/researcher_profiles/skill/**` is the one copy of the consumer
  skill. It ships in the wheel, `rp skill --install` writes it, and
  `rp-browser/scripts/copy-skills.mjs` publishes it into the browser app.
- The conformance corpus (`spec/conformance/`) runs through both validators, the
  Python CLI and rp-browser's `runValidation`, in
  `.github/workflows/conformance.yml`. Their verdicts must match.

## Host hooks the SDK leaves open

rp-sdk serves profiles on its own with a single operator bearer token. A
management host that adds people, sessions, and per-app keys sets these
`app.state` attributes; bare rp-sdk leaves the callables `None` and falls back to
the operator token. rp-sdk stays agnostic about how a host layers its own
edits, grants, and consumers on top: never move those semantics into rp-sdk.
The same list is in the `researcher_profiles.api` package docstring.

- `app.state.store`: the `ProfileStore` every route reads and writes;
  `create_app` sets it, and a host that mounts the routers or re-declares the
  handlers on its own app sets it itself (`api/deps.py::get_store`).
- `app.state.owner_verifier`: decides whether the caller may edit a profile's
  canonical documents, and whether they may read its restricted artifacts.
- `app.state.consumer_verifier`: checks a per-application scoped key.
- `app.state.write_scope_verifier`: decides whether a credential may make one
  specific write (`action`, `detail`); `api/deps.py::check_write_scope` calls it
  before every edit route forwards the write. See
  `docs-dev/rp-sdk/developer/agent-editing.md`.
- `app.state.push_gate`: `(request, slug, rid) -> None`, called on
  `PUT /profiles/{slug}` once the body's rid is known and before anything is
  committed; raises `HTTPException(403)` to refuse a push of that rid
  (`api/routes_push.py`).
- `app.state.viewer_resolver`: `(request, slug | None) -> ViewerTier`, the most
  permissive tier this caller may be shown for that profile; every read is
  projected through it, and the default is `api/deps.py::resolve_viewer_tier`.
- `app.state.profile_tier_floor`: narrows a profile's declared privacy tier for
  this host, and carries the sentence explaining why. See
  `docs-dev/rp-sdk/developer/read-seam.md`.
- `app.state.embedding_healthy` and `app.state.embedding_health_detail`: a host
  that runs a startup query-embedding preflight sets the flag to `False` and
  the detail to its diagnosis, and `/health` answers 503 with it; bare rp-sdk
  leaves them `True` and `None`.

Persistence is also swappable, but through a class rather than a hook. `ResearcherProfile` composes a
`ArtifactStorage` (`storage.py`); the filesystem implementation is one backend
among several, and `store/sql/` is a peer backing store rather than a
projection of a directory. Nothing outside the storage object touches
`self.path` for content I/O, because the public `save_*` surface carries
validation, `dateModified` stamping, and cache bookkeeping that every backend
should inherit. The derived artifacts under `.cache/` sit outside
it. See `docs-dev/rp-sdk/developer/storage-seam.md`.

The dependent-state hook is `store.add_pre_commit_hook(hook)`, taking one
`WriteContext`. It fires inside the write unit, before commit, for every write:
routes, CLI, and out-of-process runs alike. It is not swallowed. A raising hook
aborts the write as a `WriteHookError` (a 500, not a `ProfileWriteError`
400).

## Tests

```bash
./scripts/test-all.sh              # both Python suites
./scripts/test-all.sh rp-sdk       # just the SDK suite
./scripts/test-all.sh scholarcore  # just the scholarcore suite
```

Two packages have a Python suite: `rp-sdk/` and `scholarcore/`. CI
(`.github/workflows/tests.yml`) invokes this same script, passing an explicit
suite name because each CI job installs only its own package, so local and CI
cannot drift. Suite-level rules (what earns a test, where a test goes) are in
`rp-sdk/AGENTS.md`.

Lint is repo wide: the root `ruff.toml` is the single config for every Python
file in the tree, and CI runs `ruff check .` plus `ruff format --check .` from
the root. Do not add a `[tool.ruff]` table to a package's `pyproject.toml`. It
would take precedence for that subtree and silently diverge.

## Packaging

Both built artifacts must stay SDK only. `rp-sdk/tests/test_packaging.py` builds
the real wheel and sdist and rejects foreign paths. The sdist is an explicit
include list in `rp-sdk/pyproject.toml`, so a new top-level folder cannot ship by
accident.
