# Changelog

All notable changes to the packages in this repository are documented here.
Each part is versioned on its own; see `docs-dev/releasing.md`. Changes to the
specification are tracked in `docs/rp-spec/CHANGELOG.md`.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the parts follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### scholarcore

scholarcore 0.1.0 -> 0.2.0.

- `normalize_pmcid` and `normalize_arxiv_id`, exported from `scholarcore`
- `normalize_doi` strips a bare `doi.org/` or `dx.doi.org/` prefix, a `doi:`
  label after a URL prefix, and a leading slash
- `Paper` normalizes `pmcid` and `arxiv_id` on load; a malformed PMCID loads
  as `None`

## [0.2.0] - 2026-10-12

Releases `rp-sdk` 0.2.0, `rp-browser` 0.2.0, and `rp-ui-lib` 0.2.0, matching
spec 0.2.0. `scholarcore` is unchanged and stays at 0.1.0. This release has
breaking changes: the visibility tiers are renamed, the command-line login
endpoints moved, full text is no longer locked to a fixed tier, and host apps
call profile reads and edits through service functions.

### rp-sdk

- Rename the visibility tiers to `public`, `limited`, and `private` (were
  `public`, `internal`, and `restricted`). The old names are rejected, and
  validation names the replacement
- Add `researchInterests`: one concept per entry, with an optional signed
  weight, its method, and its evidence. `interests` and `not_interests` are now
  rebuilt from it. Add pinned OpenAlex and MeSH vocabularies for coding them
- Decide app and account-key access per part (`none`, `read`, or `write`) in
  `rp agent` and `rp push`, with an `insufficient_access` refusal
- Move command-line login to standard RFC 8628 endpoints: `POST /api/auth/device`
  and `POST /api/auth/token` replace `/api/manage/cli-auth` and its poll route
- Make the `paper_fulltext` tier a default instead of a fixed floor, and drop
  `locked` and `lock_reason` from artifact tier reports
- Add push modes (`replace`, `merge`, `prune`) with a preflight diff, and
  `GET /capabilities` so a client can check what a server accepts
- Make `rp publish` prune removed profiles and handle failures more safely
- Add read routes for record views, paged lists, full text in pages, and
  passage search, and serve profile files beside `profile.jsonld`
- Add section and per-attribute visibility, with clinical trial and clinical
  artifacts, and withhold embeddings and chunks the reader may not see
- Label served manifest entries with `accessRights`
- Add interview self-report profiles
- Add rid aliases, so a merged profile's old slug still resolves, and ORCID login
  proof
- Add `Caller`, `Hooks`, and `Service` to the API: host apps call profile reads
  and edits as service functions that raise typed errors, and read helpers no
  longer take a `Request`
- Make every AI-written profile field editable by the owner, and merge partial
  edits into single-object fields
- Add `isVersionOf` links between a preprint and its journal paper, and a
  collaborator relationship term
- Record input digests on generated files
- Add feed search fallbacks and paper matching, route all OpenAlex calls through
  one keyed client, and let callers set the fastembed batch size
- Rename the `grants` source to `mygrants`
- Fail validation on garbled paper text, and warn instead of failing on an
  unbuilt index
- Fix a partial manifest deleting files on push, a doubled `profile.jsonld` in
  compliance checks, and citation tags for some paper id shapes
- Fix the bugs found in two pre-release reviews

### rp-browser

- Switch in-browser embeddings to `@huggingface/transformers`
- Let a host app supply the header brand, and add `homeHero` slots for a logo
  and extra actions
- Accept a slug in the profile page URL and build explore links on the `/p` route
- Show each person once across sources, and name sources from their own cards
- Show a file table with visibility, link each paper's summary and full text,
  and always show the profile tabs
- Give data tables a distinct header and row style
- Drop the ORCID round-trip badge from the validator

### rp-ui-lib

- Add sized read types for the new paged read routes
- Add a `fullTextHref` prop to `PaperRow` and `PapersList` (with an exported
  `FullTextHref` type) and a "Summary · Full text · Publisher" action row
- Add `SectionTier` and `SectionTierReport` types, and fields for research
  interests, section visibility, and therapeutic areas
- Remove `locked` and `lock_reason` from artifact tiers

## [0.1.0] - 2026-09-25

First tagged release of every part: `rp-sdk` 0.1.0, `scholarcore` 0.1.0,
`rp-browser` 0.1.0, and `rp-ui-lib` 0.1.0.
