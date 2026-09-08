# rp-sdk Skill Reference

> The `researcher-profiles` package (CLI: `rp`) reads, validates, indexes, serves, and publishes researcher profiles: self-contained directories of JSON-LD and Markdown describing one researcher. A profile is a folder, so there is no database to provision and no build step to run. This page is the agent-facing quick reference; every claim here links to the page that documents it fully.

> Documentation links: links below point to relative `.md` files (e.g. `reference/cli.md`) within this docs tree. Every page on this docs site is also available as raw Markdown; append `.md` to any docs URL.

## Install

Install from a checkout (see the [SDK overview](index.md#install)). The extras
this page uses are `vectors,st` for the embedding index, `api` for the FastAPI
server, `client` for the HTTP storage backends, `sql` for the profile store,
and `llm` for the persona methods.

Python 3.12+. Core depends only on `pydantic`, `pyyaml`, and
[`scholarcore`](../../scholarcore/docs/index.md). A plain
`import researcher_profiles` never pulls in a vector store, an HTTP client, or a
model runtime: install the extra for the job. Full matrix in the
[Python API reference](reference/python-api.md).

## What a profile is

A profile is a directory whose one mandatory entry point is `profile.jsonld`,
a manifest that identifies the researcher and lists every artifact as a typed
link.
Everything else (persona documents, a works list, per-paper summaries, an
embedding index) is discovered through that manifest, never guessed from a
filename.

```
jane-doe/
  profile.jsonld                     # identity + manifest (required)
  personality/SOUL.md                # voice
  personality/expertise.md           # what they work on
  sources/papers.jsonld              # works list (required; may be an empty Collection)
  sources/summaries/*.summary.md     # one summary per paper
  .cache/embeddings.sqlite           # the search index (generated)
```

A profile's identity is its `rid`: a checksum-validated ORCID, or a
`local:<slug>-<hex>` id for a researcher with no ORCID. The directory name is a
display slug and carries no authority. Read
[the profile format](profile-format.md)
before writing one, and the
[specification](../rp-spec/index.md)
before implementing a consumer.

## Load and inspect a profile

```python
from researcher_profiles import ResearcherProfile

p = ResearcherProfile.from_files("profiles/jane-doe")
p.name, p.rid, p.level  # level is "lite" | "full" | "deep"
p.papers  # the works list
p.soul, p.expertise  # persona documents
p.summaries["doe2019methods"]  # one paper summary
```

`from_files(..., eager=True)` validates every artifact up front instead of
lazily; that is the schema-level validation check. Walkthrough:
[Getting started](tutorial.md).

## Validate

```bash
rp validate profiles/jane-doe          # exits 4 on a conformance violation
rp validate profiles/jane-doe --json   # machine-readable report
rp manifest profiles/jane-doe --check  # exits 4 when the manifest drifted from disk
```

To validate without installing anything, use the JSON Schemas in `schemas/`
directly. See
[Validate a profile](how-to/validate-a-profile.md)
and the [schema reference](reference/schemas.md).

## Index and search

```bash
rp index profiles/jane-doe                          # build .cache/embeddings.sqlite
rp index profiles/jane-doe --force                  # rebuild from scratch
rp search profiles/jane-doe "chromatin accessibility" -k 5
rp search profiles/jane-doe "methods" --type expertise
```

Requires `[vectors,st]`. Hits come back ranked by cosine similarity with their
source, section, and a text preview. Details:
[Build and search the embeddings index](how-to/embeddings-index.md).

## Serve profiles over HTTP

```bash
export RESEARCHER_PROFILES_ROOT=/path/to/profiles
export RESEARCHER_PROFILES_TOKEN="a-long-random-string"   # omit to run open
python -m researcher_profiles.api
```

```bash
curl http://127.0.0.1:8109/health
curl -H "Authorization: Bearer $RESEARCHER_PROFILES_TOKEN" \
     http://127.0.0.1:8109/api/v1/profiles
curl -X POST -H "Authorization: Bearer $RESEARCHER_PROFILES_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"query": "region set enrichment analysis", "k": 5}' \
     http://127.0.0.1:8109/api/v1/match
```

`/match` ranks every indexed profile against a free-text query and returns
`ranked_profiles` and `total_profiles` alongside the matches, so you can tell a
narrow result from a broken embedding path. `/health` answers `503`
with `status: "degraded"` when the host has flagged the embedding backend as
unusable; check it before trusting an empty match list. Every endpoint, field,
and environment variable:
[HTTP API reference](reference/api.md)
and
[Serve a profile API](how-to/serve-a-profile-api.md).

To read from a server instead of running one, see
[Access profiles over HTTP](how-to/access-profiles-over-http.md).

## Talk to a profile

With `[llm]` installed and `ANTHROPIC_API_KEY` set:

```python
resp = p.persona.ask("What is your approach to region-set analysis?", k=5)
resp = p.persona.review(material, focus="abstract")
resp = p.persona.innovate("single-cell epigenomics")
resp = p.persona.riff("what if enrichment were computed on the fly?")
```

Answers are grounded in the profile's own corpus; with `strict_corpus=True` the
call refuses rather than inventing when nothing relevant is retrieved. The same
four verbs are exposed as API endpoints. See
[Use the persona methods](how-to/persona-methods.md).

To consult a *published* profile with no package
at all, read the consumer skill a published site serves at
`<site base URL>/skills/researcher-profile/SKILL.md` and follow it. It teaches
progressive reading: manifest first, persona
documents next, paper summaries on demand. Most questions are then answered from
~30 KB instead of the full bundle.

## Store profiles in a database

```bash
export RESEARCHER_PROFILES_DATABASE_URL="postgresql://user@host/db"
rp db init
rp db push --all --include-binary     # --include-binary carries the search index
rp db list
rp db pull jane-doe --to ./pulled     # reproduces the directory byte for byte
```

The store is a peer backend. Pick one backend per
deployment. There is no default URL; a command with no URL exits `2`.
See [Store profiles in a database](how-to/sql-layer.md).

## Publish and push

```bash
rp render profiles/jane-doe --base-url https://example.org   # index.html, in place
rp site profiles --out ./site --base-url https://example.org # collection files
rp push profiles/jane-doe --url http://localhost:8109        # upload to a server
```

`render` writes into the profile folder; there is no separate output tree,
because a profile is publishable as it stands. It also refreshes
`.publishignore`, the list of non-public artifacts a plain sync must not copy.
`site` writes the
collection-level files for a *set* of profiles. Deployment is then an `rsync`.
See [How to host a profile](publishing.md)
and the [static API](../rp-spec/static-api.md)
for the normative CORS and content-type requirements.

## Export for a knowledge base

```bash
rp export profiles/jane-doe                 # one deterministic text blob
rp export profiles/jane-doe --json          # the full bundle + content hash
```

Two runs over an unchanged directory produce byte-identical output, so a
knowledge base can upsert only when `content_hash` changes. Exit `4` means the
profile's visibility is above `public` and `--allow-nonpublic` was not given.

## Privacy tiers

Every artifact carries a visibility of `public`, `internal`, or `restricted`,
and a served response is projected through the caller's tier. Never assume an
absent field means "no data"; it may mean "not visible to you". The rules are
normative and short:
[Privacy](../rp-spec/privacy.md).

## Related skills

The repository also ships task-specific skills, each a `SKILL.md` you can read
the same way:

| Skill | Use it when |
|---|---|
| `researcher_profiles/skill/SKILL.md` | Reading and answering questions from a published profile (`rp skill --install`) |
| `skills/publish-profile/SKILL.md` | Publishing profiles as a static site on Cloudflare, S3, nginx, or GitHub Pages |
| `skills/profile-agent/SKILL.md` | Acting as a researcher's agent with a scoped `rpa_` key |

## Where to look next

| Question | Page |
|---|---|
| Every subcommand and flag | [CLI reference](reference/cli.md) |
| Every endpoint and wire model | [HTTP API reference](reference/api.md) |
| Classes, methods, exceptions | [Python API reference](reference/python-api.md) |
| What a conforming profile must contain | [Specification](../rp-spec/index.md) |
| The shared Person / Paper / Award vocabulary | [scholarcore](https://github.com/databio/researcher-profiles/tree/master/scholarcore/docs) |
