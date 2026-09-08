# RP UI Library

RP UI Library is a set of reusable React components for displaying researcher
profiles: profile headers, expertise sections, paper lists, and so on. It is for
React developers using these components against a Researcher Profile API.

This is a library, not a standalone app. For a ready-to-use profile browser,
see [RP Browser](../rp-browser/).

## Layout

| Path | Role |
|---|---|
| `rp-ui-lib/src/lib/` | Presentational components with no data layer. They take already-fetched props and do no fetching, auth, or host coupling. This is what other apps vendor. |
| `rp-ui-lib/src/types.ts` | Generated TypeScript mirror of the HTTP wire contract. |

The library components cover the wire types:

- `ResearcherProfileViewer`: the shell. It composes every section from
  `{ detail, papers, loadSummary }`.
- `ProfileHeader`, `MetadataPanel`: `ProfileMetadataPayload`.
- `MarkdownSection`: one titled markdown body; the shell renders it twice, for
  the `expertise` and `soul` fields, via the `Markdown` component (`marked` +
  `dompurify`).
- `PapersList` / `PaperRow`: `PaperEntry[]`, with lazy `PaperSummary` expansion.
- `Markdown`: the sanitized markdown renderer the sections share.
- `worksGraphToPapers(works)`: maps a `sources/papers.jsonld` document (a
  `Collection` with `hasPart`, a `@graph` document, or a bare array) to
  `PaperEntry[]`.

The package also exports the types `ResearcherProfileViewerProps`,
`LoadSummary`, `ProfileDetail`, `ProfileMetadataPayload`, `ProfileSummary`,
`PaperEntry`, and `PaperSummary`.

## Typed contract and regeneration

`src/types.ts` is generated from the pydantic wire models in
`researcher_profiles.api_models`. It is the single typed contract the
components speak. The pipeline (`npm run gen:types`):

1. `rp schema export-wire <tmp>/wire.schema.json` dumps the combined wire JSON
   Schema from `api_models.py` to a gitignored intermediate file.
2. `json-schema-to-typescript` compiles that schema to `src/types.ts`.

Only `src/types.ts` is committed. Regenerate whenever `api_models.py` or the
wire types change, and keep the committed output in sync. A CI check that
fails when `gen:types` would produce a diff is the way to enforce that; the
repository does not have one yet.

## Where the UI is served

The FastAPI app in `rp-sdk` serves the JSON API only; it mounts no static
files and has no built-in viewer. The browser is a separate SPA, built from
this library plus `rp-browser`, served by whoever deploys it. For local
review, run `npm run dev` in `rp-browser/`.

## Downstream vendoring

`src/lib/` and `src/types.ts` are the vendorable surface: a consumer may copy
those files wholesale and wrap them with its own data layer and design system.
See `rp-ui-lib/README.md` for the sync workflow.
