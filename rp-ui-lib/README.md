# rp-ui-lib

Reusable React components for displaying researcher profiles. `src/lib/` takes
already-fetched props and does no fetching and no auth, so other apps can vendor
it directly. The runnable app is `../rp-browser/`.

Full documentation: [docs/rp-ui-lib/](../docs/rp-ui-lib/index.md). It covers what the
components are, the generated typed contract, and how downstream apps vendor it.

## Scripts

| Script | What it does |
|---|---|
| `npm run typecheck` | `tsc --noEmit`. |
| `npm run lint` | `tsc --noEmit`. |
| `npm run gen:types` | Regenerate `src/types.ts` from the pydantic wire models. |

`gen:types` needs a Python that can `import researcher_profiles` (it uses the
repo `.venv` automatically, or set `RP_PYTHON`).

## Two rules that are easy to break

Styling stays CSS-modules only: no Tailwind, no inline `style={{}}`, and no
host design-system classes. Colors come from the overridable `--rp-color-*`
custom properties (`-fg` / `-muted` / `-border` / `-accent` / `-tag-bg` /
`-surface`), which a host app sets on an ancestor container. This lets
the library vendor into any host without carrying a build dependency.

Regenerate types after any wire-contract change. `src/types.ts` is generated
from `../rp-sdk/src/researcher_profiles/api_models.py` and is committed;
`schemas/wire.schema.json` is a gitignored intermediate the generator writes
and reads back. After changing `api_models.py`, run `npm run gen:types` here.
A consumer that vendored these files and has not re-synced will not compile
against the regenerated exports.
