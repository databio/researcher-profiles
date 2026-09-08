# CLI reference

The `researcher-profiles` package installs a single console script, `rp`. Run
`rp <subcommand> --help` for per-command usage.

```
rp <subcommand> [options]
```

| Subcommand | Purpose | Extra required |
|---|---|---|
| [`index`](#index) | Build or update a profile's embedding index | `[vectors,st]` |
| [`export-embeddings`](#export-embeddings) | Write the servable flat embedding form (`embeddings/`) from the sqlite index | `[vectors,st]` |
| [`export`](#export) | Render a profile as one text blob (plus metadata) for a knowledge base | core |
| [`search`](#search) | Query a profile's embedding index | `[vectors,st]` |
| [`rank-works`](#rank-works) | Rank new candidate works against a profile's embedding vector | `[vectors,st]` |
| [`schema`](#schema) | Export JSON Schema files for the profile format | core |
| [`graph`](#graph) | Build or inspect the derived profile graph (coauthor / COI / advising edges) | core |
| [`db`](#db) | Push, pull and inspect profiles in a SQL profile store | `[sql]` |
| [`push`](#push) | Upload a built profile directory to a remote API server | `[client]` |
| [`render`](#render) | Render `index.html` into a profile folder and refresh its manifest and `.publishignore` (in place) | core |
| [`site`](#site) | Write the collection files (index.json, by-rid.json, SKILL.md, sitemap.xml, ...) for a set of profiles | core |
| [`install`](#install) | Pull profiles from a registry into the local cache | `[client]` |
| [`seek`](#seek) | Print the local path of an installed profile | core |
| [`list`](#list) | List profiles in the local cache | core |
| [`listr`](#listr) | List profiles available on a registry | `[client]` |
| [`login`](#login) | Log in to a profile server and store a push key | `[client]` |
| [`logout`](#logout) | Forget the stored login | core |
| [`whoami`](#whoami) | Show who the stored login is and what it may push | `[client]` |
| [`skill`](#skill) | Print or install the consumer skill for reading published profiles | core |
| [`where`](#where) | Print the profile directory for a rid or a slug | core |
| [`mint-local-id`](#mint-local-id) | Mint a `local:` rid for a researcher who has no ORCID | core |
| [`validate`](#validate) | Validate a profile directory against the JSON Schema spec | core |
| [`manifest`](#manifest) | Show or regenerate a profile's manifest | core |
| [`sign`](#sign) | Sign a profile's `profile.jsonld` with a `key_signature` proof | `[signing]` |
| [`sign-verify`](#sign-verify) | Verify the `key_signature` proof(s) on a `profile.jsonld` against a JWK Set | `[signing]` |
| [`agent`](#agent) | Agent credential and identity commands | `[client]` |
| [`profile`](#profile) | Agent profile editing (pull, diff, push, visibility) | `[client]` |

The table above lists every verb the parser registers. Every row has a
dedicated section below with its flags, exit codes, and a worked example.

The HTTP server has a separate entry point, `python -m researcher_profiles.api`;
see the [API reference](api.md#running-the-server).

## Naming a profile

Every verb that takes a `profile` argument accepts three spellings of it:

| Spelling | Example |
|---|---|
| A path to a profile directory | `./profiles/jane-doe` |
| A rid | `0000-0002-1825-0097` |
| A directory slug in the local cache | `jane-doe` |

A path that exists on disk always means itself; only a name that is nothing on
disk is looked up in the cache, so a local directory is never shadowed by a
same-named entry in the cache. `--root DIR` names the cache to look in, and
otherwise the usual order applies: `$RESEARCHER_PROFILES_ROOT`, else
`~/researcher-profiles`. A name that resolves to nothing exits `2` and prints
the root it searched.

This is why `rp install jane-doe` and `rp index jane-doe` fit together: install
prints the slug, and every later verb takes it.

---

## index

Build (or incrementally update) the embedding index for a profile. The index is
written to `<profile>/.cache/embeddings.sqlite`.

```
rp index <profile> [--root DIR] [--force] [--backend SPEC]
```

| Argument / flag | Description |
|---|---|
| `profile` | A profile directory, a rid, or a slug (see [Naming a profile](#naming-a-profile)). |
| `--root DIR` | Profiles root to resolve a rid or slug against. Default: `$RESEARCHER_PROFILES_ROOT`, else `~/researcher-profiles`. |
| `--force` | Drop and rebuild the index from scratch. |
| `--backend SPEC` | Backend spec, e.g. `st:all-MiniLM-L6-v2`. Defaults to the profile's recorded backend or the package default. |

Prints a one-line report:

```
backend=st:all-MiniLM-L6-v2 added=4 updated=0 skipped=0 removed=0 duration=5.17s
```

Requires the `[vectors,st]` extras.

---

## export-embeddings

Write the servable flat embedding form (`<profile>/embeddings/`) from the
sqlite index at `<profile>/.cache/embeddings.sqlite`. The flat form is what a
static or dynamic server ships; the sqlite form is a local build artifact.

```
rp export-embeddings <profile> [--root DIR]
```

| Argument | Description |
|---|---|
| `profile` | A profile directory, a rid, or a slug (see [Naming a profile](#naming-a-profile)). |
| `--root DIR` | Profiles root to resolve a rid or slug against. Default: `$RESEARCHER_PROFILES_ROOT`, else `~/researcher-profiles`. |

```console
$ rp export-embeddings $(rp where jane-doe)
wrote embeddings/jane-doe.bin backend=st:all-MiniLM-L6-v2 count=42 dim=384 dropped=3
```

When there is no sqlite index, or no public row survives the privacy tier
filter, it prints `no flat form written` and exits `0`. Any stale flat form
on disk is removed and `hasEmbeddingIndex` is left `false`. A name that resolves to no
directory at all exits `2`; a directory that exists but holds no index takes
the same no-op path as an empty one.

Requires the `[vectors,st]` extras.

---

## search

Query a profile's embedding index and print ranked hits.

```
rp search <profile> <query> [--root DIR] [-k N] [--type SOURCE_TYPE] [--json]
```

| Argument / flag | Default | Description |
|---|---|---|
| `profile` | none | A profile directory, a rid, or a slug (see [Naming a profile](#naming-a-profile)). |
| `--root DIR` | none | Profiles root to resolve a rid or slug against. Default: `$RESEARCHER_PROFILES_ROOT`, else `~/researcher-profiles`. |
| `query` | none | Free-text query. |
| `-k N` | `5` | Number of hits to return. |
| `--type SOURCE_TYPE` | none | Filter by source type: `paper_summary`, `paper_abstract`, `expertise`, `soul`, `grant`, `cv`, or `web`. |
| `--json` | off | Emit the hits as a JSON array instead of the text listing. |

Each hit prints its score, source, chunk index, section, and a text preview:

```
[0.595] expertise:expertise#1  Region set analysis
   ## Region set analysis Jane developed methods for enrichment analysis ...
```

Requires the `[vectors,st]` extras.

---

## rank-works

Rank new candidate works against a profile's embedding vector: the "what's
new that fits this researcher" query. Needs a built embedding index
(`rp index`).

```
rp rank-works <profile> [--root ROOT] [--since YYYY-MM-DD] [--openalex]
                        [--works FILE] [-k N]
                        [--kind {centroid,summary,expertise}] [--threshold X]
                        [--mailto EMAIL] [--exclude-types T1,T2] [--all-types]
                        [--json]
```

| Argument / flag | Default | Description |
|---|---|---|
| `profile` | none | A profile directory, a rid, or a slug. |
| `--root ROOT` | none | Profiles root, used to resolve a rid/slug. |
| `--since YYYY-MM-DD` | 30 days back | Earliest publication date, for `--openalex`. |
| `--openalex` | off | Fetch candidates from the OpenAlex API. Needs the `[client]` extra. |
| `--works FILE` | none | JSON array of candidate works (raw OpenAlex works or `PaperRecord` dicts), instead of `--openalex`. |
| `-k N` | `10` | Number of ranked works to return. |
| `--kind {centroid,summary,expertise}` | `centroid` | Which profile vector to rank against. |
| `--threshold X` | none | Drop works scoring below `X`. |
| `--mailto EMAIL` | none | OpenAlex polite-pool contact. |
| `--exclude-types T1,T2` | a default deposit-type set | OpenAlex work types to drop from `--openalex` candidates (software, book, etc.). |
| `--all-types` | off | Keep every work type; disables the deposit-type filter. |
| `--json` | off | Emit the ranked works as a JSON array. |

Exactly one of `--openalex` or `--works` is required.

```bash
rp rank-works voss-elena --openalex --since 2026-07-01
rp rank-works voss-elena --works candidates.json -k 10 --json
```

**Exit codes.** `0` ok (including "no candidate works survived ranking") ·
`1` the profile could not be loaded, the profile has no subfields/interests
or OpenAlex work ids to seed a query with, the OpenAlex fetch failed, or the
`[vectors,st]` extras are missing · `2` neither `--openalex` nor `--works` was
given, `--works FILE` could not be read as JSON, or the index has not been
built (run `rp index` first).

Requires the `[vectors,st]` extras; `--openalex` additionally needs `[client]`.

---

## export

Render a profile as a single deterministic text blob for a knowledge base, or
the whole export bundle (identity, links, the corpus DOI list, the text, and a
content hash) as canonical JSON.

```
rp export <profile> [--json] [-o FILE] [--max-papers N] [--char-budget N]
                    [--no-soul] [--no-expertise-doc]
                    [--explore-base URL] [--profile-url URL] [--allow-nonpublic]
```

| Argument / flag | Description |
|---|---|
| `profile` | A profile directory, a rid, or a slug (see [Naming a profile](#naming-a-profile)). |
| `--root DIR` | Profiles root to resolve a rid or slug against. Default: `$RESEARCHER_PROFILES_ROOT`, else `~/researcher-profiles`. |
| `--json` | Emit the export bundle as canonical JSON instead of the bare markdown. |
| `-o`, `--out FILE` | Write to `FILE` instead of stdout. |
| `--max-papers N` | Cap on papers whose text enters the blob (default 40). |
| `--char-budget N` | Soft cap on blob characters (default 120000). Whole paper blocks are dropped; a body is never truncated. |
| `--no-soul` | Omit `personality/SOUL.md`. |
| `--no-expertise-doc` | Omit `personality/expertise.md`. |
| `--explore-base URL` | Base URL of a profile browser app, used to build the backlink. No default: without it the bundle carries no `explore_url`. |
| `--profile-url URL` | Override the published profile URL (default: the document's `url`). |
| `--allow-nonpublic` | Export a profile whose `visibility` is above `public`. |

```bash
rp export $(rp where jane-doe)                              # the blob
rp export $(rp where jane-doe) --json                       # the bundle
rp export $(rp where jane-doe) --json -o jane-doe.export.json
```

The blob is deterministic: two runs against an unchanged directory produce
byte-identical output, which is what lets a knowledge base upsert only when
`content_hash` changes.

**Exit codes.** `0` ok · `1` the profile could not be loaded · `4` the profile's
`visibility` is above `public` and `--allow-nonpublic` was not given; the
message names the flag.

See [Export for knowledge bases](python-api.md#export-for-knowledge-bases).

---

## schema

Export JSON Schema files for the profile format.

```
rp schema export <out_dir>
rp schema export-wire <out_file>
```

`export` writes one file per model. `export-wire` writes a single combined
HTTP wire-contract schema, consumed by `rp-ui-lib/scripts/gen-types.mjs` to
generate the browser's TypeScript types.

| Argument | Description |
|---|---|
| `out_dir` (`export`) | Directory to write `<model>.schema.json` files into. Created if absent. |
| `out_file` (`export-wire`) | Output file for the combined wire schema, e.g. `rp-ui-lib/schemas/wire.schema.json`. |

`export` writes nine files and prints each path:

```
wrote schemas/profile_jsonld.schema.json
wrote schemas/papers_jsonld.schema.json
wrote schemas/grants_jsonld.schema.json
wrote schemas/summary_file.schema.json
wrote schemas/embedding_index.schema.json
wrote schemas/profile_list.schema.json
wrote schemas/collection.schema.json
wrote schemas/topic_index.schema.json
wrote schemas/profile_export_bundle.schema.json
```

```bash
rp schema export-wire rp-ui-lib/schemas/wire.schema.json
```

See the [schema reference](schemas.md).

---

## validate

Validate a profile directory against the JSON Schema spec: schema conformance
plus manifest agreement. Core; no extra required.

```
rp validate <profile> [--root DIR] [--json]
```

| Argument / flag | Description |
|---|---|
| `profile` | A profile directory, a rid, or a slug (see [Naming a profile](#naming-a-profile)). |
| `--root DIR` | Profiles root to resolve a rid or slug against. Default: `$RESEARCHER_PROFILES_ROOT`, else `~/researcher-profiles`. |
| `--json` | Output the report as JSON instead of text. |

```bash
rp validate $(rp where voss-elena)
rp validate ./profiles/voss-elena --json
```

A pass with undeclared JSON-LD terms prints a warning banner on stderr; that
is not a conformance failure.

**Exit codes.** `0` conformant · `4` a conformance violation. Re-read the
violations printed, or fetch them keyed by JSON pointer with `--json`.

---

## graph

Build (or inspect) the derived profile graph (coauthor, COI, and advising
edges) over every profile in a store.

```
rp graph build [--root ROOT] [--json]
```

| Argument / flag | Description |
|---|---|
| `--root ROOT` | Profiles root to scan. Default: the resolved cache. |
| `--json` | Emit the build summary as JSON. |

Writes `<root>/.cache/graph.sqlite`.

```console
$ rp graph build --root ~/researcher-profiles
built graph over 12 profiles: 34 nodes, 58 edges -> /home/jane/researcher-profiles/.cache/graph.sqlite
```

**Exit codes.** `0` ok · `2` an unknown `graph` subcommand was given (`build`
is the only one today).

---

## db

Push, pull and inspect profiles in a [SQL profile store](../how-to/sql-layer.md).
The store is a **peer backend**: a profile in it is a profile,
and `rp db pull` reproduces the directory byte for byte. Pick one backend per
deployment. Nothing should read the same profile from a directory and from the
store at once.

```
rp db init                   [--database-url URL] [--json]
rp db push <ref>... | --all  [--database-url URL] [--root DIR] [--include-binary] [--json]
rp db pull <rid|slug>        [--database-url URL] [--to DIR] [--with-build] [--json]
rp db list                   [--database-url URL] [--json]
rp db rm <rid|slug>          [--database-url URL] [--json]
```

| Option | Description |
|---|---|
| `--database-url` | Store URL. Default: `$RESEARCHER_PROFILES_DATABASE_URL`. |
| `--root` (`push`) | Profiles root to read from. Default: the resolved cache. |
| `--include-binary` (`push`) | Store binary artifact bodies too (`.cache/embeddings.sqlite` can be tens of MB). |
| `--to` (`pull`) | Destination directory. Default: `./<slug>`. |
| `--with-build` (`pull`) | Also write the build sidecar, which is never part of the published record. |

The URL resolves, highest precedence first: `--database-url`, then
`$RESEARCHER_PROFILES_DATABASE_URL`. There is no built-in default, because a guessed
database is a whole profile store written somewhere nobody meant. A command
with no URL exits `2` (invalid usage). Every command prints which database it
acted on.

```console
$ export RESEARCHER_PROFILES_DATABASE_URL="postgresql://user@host/db"
$ rp db init
created the rp_* tables on postgresql://user@host/db
$ rp db push --all
jane-doe                 0000-0002-1825-0097
john-smith               0000-0004-4600-113X
pushed 2 profile(s) to postgresql://user@host/db
$ rp db pull jane-doe --to ./pulled
pulled jane-doe (0000-0002-1825-0097) from postgresql://user@host/db -> pulled
```

Exit `2` when a reference matches nothing in the store or when no database URL
is configured, `1` when at least one profile failed to push.

---

## push

Upload a built profile directory to a remote API server
(`PUT /api/v1/profiles/{slug}`). The directory's contents are tarred
(dotfiles like `.archive/` excluded) and swapped into place atomically on the
server; the profile is immediately listable, and matchable when it includes a
built `.cache/embeddings.sqlite`.

```
rp push <profile> [--root DIR] [--url BASE_URL] [--slug SLUG] [--token TOKEN]
                  [--include-fulltext] [--json]
```

| Option | Description |
|---|---|
| `profile` | A built profile directory, a rid, or a slug (see [Naming a profile](#naming-a-profile)); the directory must contain `profile.jsonld`. |
| `--root DIR` | Profiles root to resolve a rid or slug against. Default: `$RESEARCHER_PROFILES_ROOT`, else `~/researcher-profiles`. |
| `--url` | Server base URL, e.g. `http://localhost:8109`. Default: the server you ran [`rp login`](#login) against. |
| `--slug` | Target slug on the server. Default: the directory name. |
| `--token` | Bearer token. Default: `RESEARCHER_PROFILES_TOKEN`, else the stored login's key for that server. |
| `--include-fulltext` | Also upload `sources/papers/` extracted fulltext. Off by default. |
| `--json` | Emit the server's summary as JSON. |

```console
$ rp push profiles/jane-doe --url http://localhost:8109
pushed jane-doe (Jane A. Doe, level=lite, indexed=True)
```

**Exit codes.** `0` ok · `1` the server could not be reached or would not
answer · `2` no server URL resolved, the directory is unreadable, or the target
refused the upload. See the [API reference](api.md#put-apiv1profilesslug) for
validation rules.

---

## render

Render a single profile's `index.html` in place, and refresh the two derived
files that must stay in step with the profile on disk: its manifest
(`hasPart` / `subjectOf` in `profile.jsonld`) and `.publishignore` (the list of
`internal`/`restricted` artifacts a dumb sync must not carry). There is no
transform and no separate output tree: the folder is publishable by
construction.

```
rp render <profile> [--root DIR] [--base-url URL] [--no-index]
```

| Argument / flag | Default | Description |
|---|---|---|
| `profile` | none | Path to a single profile directory. |
| `--base-url URL` | none | Base URL used when building links in the rendered page. |
| `--no-index` | off | Add `noindex` directives so crawlers skip the rendered page. |

Exit code 2 (with a message on stderr) when the profile directory cannot be
found.

---

## site

Write the collection files for a *set* of profiles into an output directory:
`index.json`, `by-rid.json`, `SKILL.md`, `style.css`, `sitemap.xml`, and the
rest. Deployment is then a dumb sync of the folders. `site` produces the
collection-level index, not per-profile pages (use [`render`](#render) for
those).

```
rp site <profiles_dir> --out DIR [--base-url URL]
                         [--no-index] [--now ISO8601]
```

| Argument / flag | Default | Description |
|---|---|---|
| `profiles_dir` | none | Root directory containing the profile subdirectories. |
| `-o`, `--out DIR` | none | Output directory for the collection files. Required. |
| `--base-url URL` | none | Base URL for `sitemap.xml` and `robots.txt`. |
| `--no-index` | off | Add `noindex` directives. |
| `--now ISO8601` | now | Pin timestamps for deterministic output. |

Any warnings are printed to stderr. Exit code 2 (with a message on stderr) when
the profiles directory cannot be found.

---

## install

Pull one or more profiles from a registry into the local cache
(`$RESEARCHER_PROFILES_ROOT`, default `~/researcher-profiles`).

```
rp install <slug> [<slug> ...] [--url URL] [--root ROOT] [--token TOKEN]
                  [--force] [--json]
```

| Argument / flag | Default | Description |
|---|---|---|
| `slug` | none | One or more profile slugs to install. |
| `--url URL` | `$RESEARCHER_PROFILES_REGISTRY_URL`, else the `rp login` server | Registry base URL(s), comma-separated. |
| `--root ROOT` | `$RESEARCHER_PROFILES_ROOT`, else `~/researcher-profiles` | Local profiles root. |
| `--token TOKEN` | `$RESEARCHER_PROFILES_TOKEN`, else the stored login's key | Bearer token, for a private registry. |
| `--force` | off | Re-download even if already cached. |
| `--json` | off | Emit one JSON record per requested slug. |

```console
$ rp install voss-elena jane-doe --url https://profiles.example.org
installed voss-elena (Elena Voss, level=lite, no fulltext) -> /home/jane/researcher-profiles/voss-elena
voss-elena: already installed at ... (--force to replace)
```

**Exit codes.** `0` every slug installed (or already present) · `2` at least
one slug failed (a bad registry, a missing token, or a slug the registry does
not have); the command keeps going and reports each failure, then exits `2`
if any occurred.

Requires the `[client]` extra.

---

## seek

Print the local path of an already-installed profile, by directory slug.

```
rp seek <slug> [--root ROOT] [--json]
```

| Argument / flag | Description |
|---|---|
| `slug` | Directory slug of an installed profile. |
| `--root ROOT` | Local profiles root. |
| `--json` | Emit `{slug, path, root}` as JSON. |

```bash
rp seek voss-elena
cat $(rp seek voss-elena)/profile.jsonld
```

`seek` takes a directory slug only. To resolve an ORCID or a rid, use
[`where`](#where).

**Exit codes.** `0` ok · `2` no such slug is installed in the resolved root.

---

## list

List the profile slugs present in the local cache. Core; no extra required.

```
rp list [--root ROOT] [--json]
```

| Argument / flag | Description |
|---|---|
| `--root ROOT` | Local profiles root. |
| `--json` | Emit `{root, profiles}` as JSON. |

```bash
rp list
rp list --root ~/work/som/profiles
```

An empty cache prints `no profiles installed in <root>` (to stdout in text
mode) plus, on stderr, which root was used and how to populate it. Exits `0`
either way.

---

## listr

List the profiles available on one or more registries.

```
rp listr [--url URL] [--token TOKEN] [--json]
```

| Argument / flag | Default | Description |
|---|---|---|
| `--url URL` | `$RESEARCHER_PROFILES_REGISTRY_URL`, else the `rp login` server | Registry base URL(s), comma-separated. |
| `--token TOKEN` | `$RESEARCHER_PROFILES_TOKEN`, else the stored login's key | Bearer token, for a private registry. |
| `--json` | off | Emit one JSON record per remote profile, tagged with which registry answered. |

```bash
rp listr --url https://profiles.example.org
RESEARCHER_PROFILES_REGISTRY_URL=https://profiles.example.org rp listr --json
```

**Exit codes.** `0` ok · `1` every named registry failed to answer (returning
`0` with an empty list would read exactly like "the registry is empty", which
is not what happened) · `2` no registry URL was given and none is stored.

Requires the `[client]` extra.

---

## skill

Print the consumer skill (the one that teaches an agent to read a *published*
profile), or install the whole skill tree to a local directory.

```
rp skill [--print | --install] [--dir DIR]
```

| Flag | Description |
|---|---|
| `--print` | Print `SKILL.md` to stdout. Default. |
| `--install` | Install the skill files to a local directory. |
| `--dir DIR` | Installation directory. Default: `~/.claude/skills/researcher-profile/`. |

```bash
rp skill
rp skill --install
rp skill --install --dir ~/.claude/skills/researcher-profile
```

**Exit codes.** `0` ok · `2` the packaged `SKILL.md` could not be found (a
broken install).

---

## where

Resolve a rid or a directory slug to the profile's local directory. A
directory name is a display handle, never an identity; `where` is how you go
from either one to a path.

```
rp where <ref> [--root ROOT] [--json]
```

| Argument / flag | Description |
|---|---|
| `ref` | A rid (ORCID or `local:...`) or a directory slug. |
| `--root ROOT` | Profiles root. |
| `--json` | Emit `{ref, path, root}` as JSON. |

```bash
rp where voss-elena
rp where 0000-0002-1825-0097
```

**Exit codes.** `0` ok · `2` nothing in the resolved root matches `ref`.

---

## mint-local-id

Mint a `local:` rid for a researcher who has no ORCID.

```
rp mint-local-id --name NAME
```

| Flag | Description |
|---|---|
| `--name NAME` | The researcher's name. Required. |

```console
$ rp mint-local-id --name "Elena Voss"
local:elena-voss-4a2fd1
```

Exit `0` always; there is no failure path.

---

## manifest

Show, regenerate, or check a profile's manifest: the `hasPart` / `subjectOf`
entries in `profile.jsonld` that enumerate every artifact the directory
contains.

```
rp manifest <profile> [--root DIR] [--write] [--check] [--json]
```

| Flag | Description |
|---|---|
| (none) | Print one line per manifest entry (`role` and `contentUrl`). |
| `--write` | Regenerate the manifest by walking the directory and store it back into `profile.jsonld`. |
| `--check` | Exit `4` when the recorded manifest disagrees with what is on disk. |
| `--json` | Emit the manifest entries and any drift as JSON. |

Drift is reported in both directions: files on disk that the manifest does not
list, and manifest entries whose file is gone. A manifest that has silently
drifted is worse than no manifest at all, because a consumer trusts it.

```console
$ rp manifest profiles/jane-doe
soul             personality/SOUL.md
expertise        personality/expertise.md
works            sources/papers.jsonld
paper_summary    sources/summaries/doe2019methods.summary.md
```

---

## sign

Sign a profile's `profile.jsonld` with a `key_signature` proof (detached JWS
over an Ed25519 key) and (re)write the well-known JWK Set beside it.

```
rp sign <profile> [--root DIR] [--key KEY] [--base-url BASE_URL] [--gen-key]
```

| Flag | Default | Description |
|---|---|---|
| `profile` | none | A profile directory or its `profile.jsonld`, a rid, or a slug (see [Naming a profile](#naming-a-profile)). |
| `--key KEY` | `<profile>/.keys/signing.pem` | Ed25519 private-key PEM. Generated (mode `0600`) if absent. |
| `--base-url BASE_URL` | the document's `url` | Published base URL, used to build `verificationMethod` (`.../.well-known/...#kid`). |
| `--gen-key` | off | Generate a fresh signing key even if one already exists. |

```bash
rp sign $(rp where voss-elena)
rp sign ./profiles/voss-elena --base-url https://profiles.example.org/voss-elena
```

The private key lives at `<profile>/.keys/signing.pem`; `.keys/` is always
excluded from a publish (see [Privacy](../../rp-spec/privacy.md)), so keep it
there rather than moving it into the published tree. A document already
carrying a `key_signature` proof has that one proof replaced; other proof
kinds are left alone.

**Exit codes.** `0` ok · `2` no `profile.jsonld` at the given path, no base
URL resolved (pass `--base-url` or set the document's `url`), or the document
fails schema validation before signing (refuses to sign an invalid document).

Requires the `[signing]` extra.

---

## sign-verify

Verify the `key_signature` proof(s) on a `profile.jsonld` against a JWK Set.

```
rp sign-verify <profile> [--root DIR] [--keys KEYS]
```

| Flag | Default | Description |
|---|---|---|
| `profile` | none | A profile directory or its `profile.jsonld`, a rid, or a slug (see [Naming a profile](#naming-a-profile)). |
| `--keys KEYS` | `<profile>/.well-known/researcher-profile-keys.json` | JWK Set path. |

```bash
rp sign-verify $(rp where voss-elena)
rp sign-verify ./profiles/voss-elena --keys ./keys/researcher-profile-keys.json
```

**Exit codes.** `0` at least one proof verifies · `1` a proof is present but
none verifies · `2` no `profile.jsonld` at the given path, or no JWK Set to
check against.

Requires the `[signing]` extra.

---

## login

Log in to a profile server from the command line and store the key it mints.
The server runs a device-authorization flow: `rp` asks for a code, prints a
link and a short user code, and polls until you approve the request in a
browser. See
[Command-line login](../../rp-spec/dynamic-api.md#142-command-line-login) for
the wire protocol.

```
rp login [server] [--label NAME] [--no-browser] [--json]
```

| Argument / flag | Default | Description |
|---|---|---|
| `server` | the server already logged in to | Server base URL, e.g. `https://profiles.example.org`. |
| `--label NAME` | the machine's hostname | Name shown for this machine on the approval page. |
| `--no-browser` | off | Print the link only; do not open a browser. |
| `--json` | off | Emit `{url, orcid, name}` as JSON. |

```console
$ rp login https://profiles.example.org
Open this link in your browser to approve the login:

  https://profiles.example.org/cli-auth?code=WQTX-9F4K

Code: WQTX-9F4K
Waiting for approval...
logged in to https://profiles.example.org as Jane Doe; key stored in ~/.config/researcher-profiles/credentials.json
```

The key is written to `~/.config/researcher-profiles/credentials.json` with
mode `0600` (or under `$XDG_CONFIG_HOME` when that is set). It is scoped to the
one server it came from, and it carries only `push_own`, so it can upload the
profiles you own or edit and nothing else. Once it is stored,
[`push`](#push), `rp install`, `rp listr` and [`whoami`](#whoami) need no
flags at all.

A server that does not implement the
[management tier](../../rp-spec/dynamic-api.md#14-management-api) answers `404`
on the login request, and the command fails with
`does not offer command-line login`.

**Exit codes.** `0` ok · `1` the server refused, timed out, or was unreachable
· `2` no server URL was given and none is stored.

Requires the `[client]` extra.

---

## logout

Delete the stored login. Takes no options.

```
rp logout
```

```console
$ rp logout
logged out; removed /home/jane/.config/researcher-profiles/credentials.json
```

Exits `0` whether or not a file was there; with nothing stored it prints
`not logged in (nothing stored)`.

---

## whoami

Ask a server who the stored token belongs to and which profiles it may push
(`GET /api/manage/whoami`).

```
rp whoami [--url BASE_URL] [--token TOKEN] [--json]
```

| Option | Default | Description |
|---|---|---|
| `--url` | the stored login's server | Server base URL. |
| `--token` | `RESEARCHER_PROFILES_TOKEN`, else the stored login's key | Bearer token. |
| `--json` | off | Emit the server's identity record as JSON. |

```console
$ rp whoami
https://profiles.example.org: Jane Doe (0000-0002-1825-0097)
scopes: push_own
can push:
  jane-doe	0000-0002-1825-0097	owner
```

**Exit codes.** `0` ok · `1` the server rejected the token or could not be
reached · `2` nothing is logged in and no `--url`/`--token` was given.

Requires the `[client]` extra.

---

## agent

Introspect an **agent** credential: the `rpa_` key an assistant holds to edit
one profile on one server. Distinct from [`login`](#login), which handles a
person's own `rpk_` push key.

```
rp agent whoami [--host NAME] [--json]
rp agent scopes [--host NAME] [--json]
rp agent config [--host NAME]
```

| Subcommand | Calls | Prints |
|---|---|---|
| `whoami` | `GET /api/manage/agent/whoami` | The agent's label and handle, its owner, viewer tier, granted and missing scopes, and the profiles it is bound to. |
| `scopes` | `GET /api/manage/agent/scopes` | The scope catalog, with the dangerous and default-on flags. |
| `config` | nothing (local) | Which credential resolved, and from where. |

| Option | Description |
|---|---|
| `--host NAME` | Which `[hosts.<name>]` block in `credentials.toml` to use. Default: the file's `default =`, or `$RESEARCHER_PROFILES_AUTH_HOST`. |
| `--json` | (`whoami`, `scopes`) Emit the server's response verbatim. |

The credential resolves in four steps; the first hit wins
(`researcher_profiles.agent.resolve_credential`):

1. Explicit `key` and `url` arguments passed to `resolve_credential` in Python.
   The command line has no `--key` / `--url`; it selects a host with `--host`.
2. The `RESEARCHER_PROFILES_AGENT_KEY` and `RESEARCHER_PROFILES_API_URL` environment variables, both set.
3. The nearest `.env` walked up from the working directory, same two names.
4. `~/.config/researcher-profiles/credentials.toml`, which must be mode `0600`.

The scope vocabulary is normative in
[Agent scopes](../../rp-spec/authentication.md#agent-scopes).

```console
$ rp agent whoami
Agent:   laptop-assistant (rpa_7f3a91c2b40e)
Owner:   Jane Doe (0000-0002-1825-0097)
Tier:    restricted
Scopes:  profile:history, profile:metadata, profile:narrative, profile:read
Missing: profile:identity, profile:visibility
Profile: jane-doe (editor)
```

**Exit codes.** `0` ok · `1` no credential resolved, or the server refused
· `2` no subcommand was given.

Requires the `[client]` extra.

---

## profile

Edit a profile through an agent credential, as a round trip through one local
file. The target profile is not a command-line argument: it comes from
`profile =` in the resolved `[hosts.<name>]` block of `credentials.toml`.

```
rp profile pull [-o FILE] [--host NAME]
rp profile diff <file> [--host NAME]
rp profile push <file> [--if-match HASH] [--force] [--dry-run] [--host NAME]
rp profile visibility get|set [--tier TIER] [--host NAME]
```

| Option | Description |
|---|---|
| `-o`, `--output FILE` (`pull`) | Where to write the document. Default: `profile.md`. |
| `--if-match HASH` (`push`) | Base hash to send instead of the one in the frontmatter. |
| `--force` (`push`) | Send no `base_hash`, so the write is last-writer-wins. |
| `--dry-run` (`push`) | Print what would change and send nothing. |
| `--tier TIER` (`visibility set`) | `public`, `internal`, or `restricted`. Required for `set`. |
| `--host NAME` | Which `[hosts.<name>]` block to use. |

The pull document is YAML frontmatter followed by the SOUL narrative as the
body. The frontmatter carries `slug`, `rid`, `base_hash`, and every editable
metadata field the server returned; the editable set is normative in
[`PATCH /profiles/{slug}/metadata`](../../rp-spec/dynamic-api.md#patch-profilesslugmetadata).

```console
$ rp profile pull -o profile.md
Wrote profile.md
base_hash: sha256:6b1c...
$ rp profile diff profile.md
Would change: summary, soul
$ rp profile push profile.md
```

`push` sends the frontmatter's `base_hash` by default, so an owner edit that
landed since the pull returns `409` rather than silently overwriting it.
Re-pull and re-apply, or pass `--force` to overwrite anyway. Changed
metadata goes to `PATCH /api/v1/profiles/{slug}/metadata` and a changed body to
`PUT /api/v1/profiles/{slug}/soul`; visibility is never part of a push, only of
`rp profile visibility set`, and an agent key may narrow a tier but never widen
one.

**Exit codes.** `0` ok · `1` no credential resolved, no `profile =` configured,
the file is missing, or the server refused (including a `403` for a missing
scope, which you should report rather than retry) · `2` no subcommand was
given, or `set` was called with no `--tier`.

Requires the `[client]` extra.
