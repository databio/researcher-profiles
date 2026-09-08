# Researcher Profiles SDK

A portable format specification and SDK for researcher profiles.

This package defines two formats. The authoring format is an on-disk directory
used to build a profile. The publication format is a set of static files used
to serve a profile on the open web. The package also loads, validates, queries,
serves over HTTP, and stores profiles in SQL.

The package does not build profiles. It defines and validates the format, so
any process that emits a conforming directory works: a person editing files by
hand, a script, or a generation pipeline.

## Install

The two Python packages (`scholarcore` and `rp-sdk`) are not on PyPI yet.
Install both from a checkout, `scholarcore` first, because `rp-sdk` depends on
it.

```bash
git clone https://github.com/databio/researcher-profiles.git
cd researcher-profiles
python -m venv .venv && source .venv/bin/activate
pip install -e ./scholarcore
pip install -e ./rp-sdk
```

Optional extras attach to the second command, for example
`pip install -e "./rp-sdk[vectors,st]"`.

| Extra | Adds |
|---|---|
| `vectors` | On-disk vector store and the cross-profile analytics math (`sqlite-vec`, `numpy`) |
| `st` | Local sentence-transformers encoder (pulls torch) |
| `fastembed` | Local ONNX encoder, same models as `st`, without torch |
| `openai` / `voyage` | Remote embedding backends |
| `client` | `ApiArtifactStorage` and `StaticArtifactStorage` HTTP backends |
| `api` | The FastAPI server |
| `llm` | Anthropic-backed `.ask`, `.review`, `.innovate`, `.riff` |
| `sql` | SQLModel relational profile store |
| `postgres` | `sql` plus the Postgres driver |
| `topics` | KMeans clustering for `store.match.cluster()` |
| `signing` | The `key_signature` proof (`cryptography`) |
| `docs` | `griffe`, to regenerate the Python API reference |
| `dev` | The supported test install |

Serving the API also needs a vector tier, so the full server install is
`pip install -e "./rp-sdk[api,vectors,st]"`.

Core depends only on `pydantic`, `pyyaml`, and
[`scholarcore`](../../scholarcore/docs/index.md), the shared academic
vocabulary the identity helpers and the training/career types come from. A
plain `import researcher_profiles` never pulls in `sqlite-vec`, `httpx`,
`anthropic`, or `sentence-transformers`. Python 3.12 or newer is required.
See the [Python API reference](reference/python-api.md) for the per-extra
capability matrix.

## Documentation map

### Tutorial

- [Getting started](tutorial.md): build a tiny profile by hand, load it,
  explore it in Python, and index it for search.

### How-to guides

- [Create a profile](how-to/create-a-profile.md)
- [Validate a profile](how-to/validate-a-profile.md)
- [Store profiles in a database](how-to/sql-layer.md)
- [Build and search the embeddings index](how-to/embeddings-index.md)
- [Serve a profile API](how-to/serve-a-profile-api.md)
- [Access profiles over HTTP](how-to/access-profiles-over-http.md)
- [Link researcher connections](how-to/link-researcher-connections.md)
- [Use the persona methods (ask / review / innovate / riff)](how-to/persona-methods.md)
- [Consume profiles efficiently](how-to/consume-profiles.md): progressive
  read order and citation-graph navigation.

### Specification

- [Specification](../rp-spec/index.md): terminology, the `profile.jsonld`
  document and its `hasPart`/`subjectOf` manifest, the file layout, the
  vocabulary, and the conformance levels
- [Privacy](../rp-spec/privacy.md): `public`/`internal`/`restricted`, the
  derivation rule, and `.publishignore`
- [Embeddings](../rp-spec/embeddings.md): the Searchable conformance level
- [Static API](../rp-spec/static-api.md): CORS, content types, caching,
  URL resolution, profile lists, and the normative hosting and transport
  requirements
- [Dynamic API](../rp-spec/dynamic-api.md): endpoints for listing, searching,
  matching, and interacting with profiles programmatically
- [Authentication](../rp-spec/authentication.md): bearer tokens and viewer tiers
- [Changelog](../rp-spec/CHANGELOG.md): spec version history

### Reference

- [AI Skill Reference](skill.md): the whole SDK on one page, written for an
  agent: install, load, validate, index, serve, publish
- [CLI](reference/cli.md): every subcommand and flag
- [HTTP API](reference/api.md): every endpoint and wire model
- [JSON Schemas](reference/schemas.md): the schema files in `schemas/`
- [Python API](reference/python-api.md): key classes and functions

### Explanation

- [The profile format](profile-format.md): the on-disk bundle layout, why it is
  JSON-LD, the `conformsTo` gate, provenance and licensing, the manifest, the
  depth levels (`lite`/`full`/`deep`), and why the package is a spec plus SDK.
- [How to host a profile](publishing.md): deploying a profile as static files
  with correct CORS and content types.

### Other components

- [rp-ui-lib](../rp-ui-lib/): the React + TypeScript presentational component
  library other apps embed to render a profile
- [rp-browser](../rp-browser/): the runnable web app that composes rp-ui-lib
  with a data layer

## For AI agents

Two things make these docs directly usable by an agent:

1. Every page is also raw Markdown. Append `.md` to any URL on this docs site,
   for example [`.../rp-sdk/reference/cli.md`](reference/cli.md), and you get
   the source instead of HTML, with links pointing at the `.md` twins so an
   agent can walk the whole corpus without parsing a page. The Copy page button
   at the top of each page copies the same text.
2. One page summarizes the rest: the
   [AI Skill Reference](skill.md) covers install through publish in one
   fetch, and links out to the page behind each claim.

To consult a *published profile* rather than the SDK, see below.

## Talk to a profile

Point any LLM agent at a published profile URL and have a grounded conversation
as that researcher. Paste this into any chat interface:

> Run `rp skill` to print the consumer skill (or `rp skill --install` to
> install it), then follow it to read the researcher profile at `<base URL>`,
> then answer my questions as that researcher.

The skill teaches the agent to read the profile progressively: manifest first,
persona documents next, individual paper summaries on demand. Most questions
are then answered from ~30 KB instead of the full ~3.5 MB. See
the walkthrough that ships beside the skill
(`researcher_profiles/skill/examples/walkthrough.md`) for traced examples
(using a Charles Darwin profile).

There are three ways to install the skill:

| Method | How |
|---|---|
| Zero-install | Paste the skill URL into any agent |
| pip | `rp skill --install` |
| Clone | `src/researcher_profiles/skill/SKILL.md` in this repo |

## License

MIT.
