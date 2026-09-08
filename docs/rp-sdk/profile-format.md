# The authoring format

> This page covers the on-disk directory you *build*. For the format
> you *publish* on the open web, see the
> [Researcher Profile specification](../rp-spec/index.md). The published
> tree is not a separate format: it is this directory, filtered by privacy tier
> (see [privacy-tiers.md](../rp-spec/privacy.md)), with a few artifacts generated
> in place at publish time (`index.html`, the flat `embeddings/` export). It is a
> filtered copy, not a transform.

The authoring format is what a profile directory contains, how the pieces
relate, and why a profile is treated as a portable directory of files. This
page is background reading. For
exact field lists, see the [JSON Schema reference](reference/schemas.md); for the
Python objects, see the [Python API reference](reference/python-api.md).

## A profile is a directory

A researcher profile is not a database row or a single document. It is a
directory of plain files, keyed by a slug, a lowercase hyphenated
identifier that is also the directory name.

```
<slug>/
├── profile.jsonld                      # THE record: identity, metadata, manifest
├── SKILL.md                            # agent entry point (optional)
├── personality/
│   ├── expertise.md                    # fixed rubric, citing paper_ids
│   ├── SOUL.md                         # narrative voice / mission
│   └── topics.json                     # LLM-labeled research topics (optional)
├── sources/
│   ├── papers.jsonld                   # Collection of ScholarlyArticle nodes
│   ├── grants.jsonld                   # Collection of MonetaryGrant nodes (any level)
│   ├── citations.json                  # citation graph over the papers (optional)
│   ├── papers/<paper_id>.md            # full text (optional)
│   ├── summaries/<paper_id>.summary.md # per-paper summary
│   ├── cv.md                           # ingested CV (deep only)
│   ├── web/<n>-<host>.md               # extracted web pages (deep only)
│   └── manual/                         # operator-supplied inputs (PDFs, curation.yaml); never ships
└── .cache/
    └── embeddings.sqlite               # derived embedding index (optional; sqlite-vec)
```

The build's own bookkeeping does not live in this tree. It sits in a sibling
build root outside the content directory,
`$RESEARCHER_PROFILES_ROOT/.build/<slug>/`, described in
[the published record versus the build](#the-published-record-versus-the-build)
below.

Everything except `profile.jsonld` is optional as far as loading goes. The SDK
tolerates a missing `sources/`, a missing `citations.json`, or an absent index.
Profiles are often incomplete while they are being assembled, and a partial
profile should still load and be inspectable.

The list above is also a whitelist of what a profile ships, with two
members that stay local: `sources/manual/` (operator-supplied inputs; the
archive builder never packs it) and, under `.cache/`, everything except the
sqlite index. `.cache/` is the SDK's own directory of precomputations: deleting
it costs time, never information, so nothing in it is part of the record. Raw scraped HTML is not a valid profile
member: there is no `sources/html/`. A profile is therefore text-only and a
few MB even carrying full paper text.

### The published record versus the build

A profile has two roots. The content root above holds the durable,
publishable artifacts. The build root is `$RESEARCHER_PROFILES_ROOT/.build/<slug>/`,
a sibling tree outside the content directory. It holds the build's disposable
bookkeeping under `meta/` (`build_state.json` and whatever else a build tool
keeps).

`.build/<slug>/meta/build_state.json` is the build sidecar. It holds the
build's bookkeeping: per-paper download attempts, rejection reasons,
contamination flags and corpus-authorship classifications,
identity-verification results, and the deep-level inputs a
human supplied. None of that is meant for anyone outside the build process, and
all of it is meaningless on a machine that will never re-run the build.

Consequently the sidecar is outside the content tree entirely: absent from the
manifest, absent from `build_profile_archive` (so it never leaves the machine
on a push or a serve), and in the SQL store kept in its own `rp_build_state`
table that is never published. `BuildState.load()`
returns an empty state when the file is missing, because a published profile
legitimately has none.

The build root is disposable: deleting `.build/<slug>` and rebuilding is a
supported way to start over. Anything that must survive a rebuild therefore
lives elsewhere. Corpus curation overrides, for example, live in
`sources/manual/curation.yaml`, outside the build root.

The sidecar keeps a plain integer `schema_version` of its own. That is not an
inconsistency: an integer counter is the right tool for a build-local file, and
exactly the wrong one for a published artifact (see `conformsTo` below).

### Why a directory of files

Keeping a profile as files rather than a single blob makes it portable,
inspectable, and composable. You can zip it, copy it, check it into version
control, or diff it. Every `contentUrl` in the manifest is a relative path, so
copying a profile to another server cannot break a link. The embedding index
lives inside the directory (`.cache/embeddings.sqlite`), so a profile carries
its own search index. Every artifact is human-readable
JSON-LD or Markdown except the binary sqlite index, so you can read a profile
without the package installed. A directory of profile directories is itself a
meaningful unit, the thing a
[`ProfileStore`](reference/python-api.md#profilestore-researcher_profilesstore) or the
[HTTP server](reference/api.md) sits over.

## Why JSON-LD

A published profile standard has to work for three different readers at once:
an LLM agent, a web crawler, and a browser-based profile browser. That rules
out a format that needs its own parser or a private vocabulary no other tool
shares.

JSON-LD is what makes one file serve all three. It is ordinary JSON, so any tool
can read it without knowing what JSON-LD is, but each key also carries a
globally-resolvable meaning through one line of context:

```json
"@context": "https://profiles.databio.org/context/v1.jsonld"
```

Documents stay nested (JSON-LD node objects), not flattened into a `@graph`:
a nested document is the one a human can read and a `jq` expression can walk.
Markdown files stay Markdown: `personality/SOUL.md`, `sources/summaries/*.md`
and the rest are linked from the JSON-LD, never inlined into it.

Nobody hand-edits these files, so JSON's ergonomic cost relative to YAML is
acceptable. In exchange, `profile.jsonld` is simultaneously the format, the
manifest, the crawler payload, and the LLM entry point.

The runtime never fetches the context: loading and validating a profile is a
pure local operation against the Pydantic models. See `context/README.md` for
the hosting arrangement, the full term table, and the freeze policy.

### `conformsTo` is the format gate

Every published document carries

```json
"conformsTo": "https://profiles.databio.org/context/v1.jsonld"
```

and a document carrying the wrong `conformsTo` fails to load; the error names
the files a profile consists of (`profile.jsonld` and `sources/papers.jsonld`).
An absent `conformsTo` is filled in with the current
format IRI. A counter with no external meaning cannot serve a published standard:
`conformsTo` is an IRI you can resolve, cite, and compare across publishers.

The loader reads `profile.jsonld` and nothing else.

## The files

### `profile.jsonld`: the record and the manifest

`profile.jsonld` is a single `schema:Person` node. It carries identity (`rid`,
`@id`), provenance and licence, the structured metadata (name, affiliation,
field and subfields, a prose summary, training and career history, expertise
topic labels, research interests,
[collaborators](how-to/link-researcher-connections.md), research outputs,
disambiguation evidence), and the manifest.

The `ProfileDocument` model validates it. The model is tolerant: it allows
extra keys and strips surrounding whitespace. It still enforces the invariants
that matter. `name` is non-empty, `rid` is a canonical ORCID or a `local:` id,
`conformsTo` is exact, and `provenance` is present.

The baseline is required and extras are allowed. Conformance means the core baseline
fields are present and well-formed; additional keys are permitted and preserved,
and the published JSON Schemas keep `additionalProperties: true`. Consumers must
ignore unknown keys and unknown role values rather than treat them as validation
failures.

### Identity: `rid`, `@id`, and `provenance`

`rid` is the identity and the single join key for every cross-system mapping. It
takes exactly one of two forms: a canonical ORCID, or an explicitly-prefixed
`local:` id. Neither form is more authoritative than the other. An ORCID is convenient because it is
globally resolvable; a local id is the ordinary identity of a researcher who has
no ORCID, of a historical figure, or of a synthetic agent. This format supports
all three directly. There is no `orcid` key. It is derived from `rid`; a second
copy on disk would be exactly the drift `rid` was invented to eliminate.

`@id` is the subject IRI. When the rid is an ORCID it is
`https://orcid.org/<rid>`. Failing that it is the profile's published `url`,
and failing that the self-referential fragment `#me` (which resolves against
wherever the document is served).

`provenance` is required and has no default. It records who asserted this
profile and on what basis:

| Value | Meaning |
|---|---|
| `orcid_verified` | The ORCID record round-trips: its website list contains this profile's `url`. Requires an ORCID `rid`, the ORCID IRI as `@id`, a `url`, and a `verifiedAt` timestamp. |
| `self_published` | The subject published it about themselves. |
| `third_party` | Someone built it about someone else. |
| `synthetic` | Not a real person (an AI agent, a test fixture). Requires a `local:` rid. |
| `historical` | A real person who cannot hold an ORCID. Requires a `local:` rid. |

An asserted ORCID is never a credential. What makes a profile ORCID-verified is
`provenance`, not the shape of `rid`, and only `orcid_verified` may claim the
subject endorsed the profile. An unlabeled published
assertion about a real person would wrongly imply the subject endorsed it.

`license` records reuse terms as an IRI. A published profile should always
carry one; a missing license leaves every consumer guessing, and the
conservative guess is "do not use it".

### The manifest: `hasPart` and `subjectOf`

The manifest stops an agent that lands on a published directory from having to
guess whether it holds `personality/SOUL.md`, `sources/summaries/*.md`, or a
search index. It enumerates one typed entry per artifact:

```json
{
  "@type": "DigitalDocument",
  "name": "Summary: doe2019methods",
  "role": "paper_summary",
  "paperId": "doe2019methods",
  "encodingFormat": "text/markdown",
  "contentUrl": "sources/summaries/doe2019methods.summary.md"
}
```

`subjectOf` carries the persona documents, things *about* the person.
`hasPart` carries everything else: the works and grants collections, the
citation graph, per-paper summaries and full texts, the CV, web pages, the
embedding index, and `SKILL.md`.

`contentUrl` is always relative. Regenerate the manifest with
`rp manifest <profile> --write`; check it against disk with
`rp manifest <profile> --check`. A build tool regenerates it as its last
step, so a built profile always enumerates exactly what is on disk.

### `personality/expertise.md` and `SOUL.md`

These two Markdown documents carry the qualitative voice of the profile that
structured metadata cannot.

`expertise.md` follows a fixed rubric, not per-topic sections. The headings
are `## Methods`, `## Intellectual lineage`, `## Recurring critiques`, and
`## Career trajectory`. Topic labels are not headings; they live in the profile
document's `expertise` list as rich phrases. A consumer that segments this file
by heading and treats each heading as a topic will be wrong. The topic index is
the manifest's expertise-label list, and the evidence is the paragraphs inside
the rubric sections. A consumer routes a question to the right section by
question type.

Prose cites supporting papers by `paper_id` in square brackets
(`[doe2016example]`), and current-generation files carry 40-70 such citations.
A consumer walks from a claim to its evidence through these citations. Older
profiles, `lite` profiles, partial builds, and third-party documents may carry
few or none, so branch on a declared capability flag on the document, never on
the profile's generation or vintage.

`SOUL.md` is a first-person-ish narrative of how the researcher frames problems,
what they value, and what they distrust.

Both are loaded as raw strings. The persona methods (`ask`, `review`, `innovate`,
`riff`) inject them into the LLM system prompt so generated text is grounded in
the researcher's own framing.

### `sources/papers.jsonld`: the corpus

`papers.jsonld` is a `Collection` node whose `hasPart` lists `ScholarlyArticle`
nodes; `about` names the person the collection belongs to. A paper record is a
research output — the format's common base for everything a researcher
produces — specialized with the bibliographic and authorship fields a scholarly
work needs. A bare top-level list is rejected, as is a document whose
`conformsTo` is absent or wrong.

Each work carries identity (`@id`, `paper_id`, `doi`, `pmid`, `pmcid`,
`openalex_id`) and bibliographic fields (`name`, `datePublished`, `isPartOf`,
`author`, `abstract`, access flags). A work's `@id` is the DOI IRI when there
is one, else the OpenAlex IRI, else the relative fragment `#paper/<paper_id>`.
A relative fragment is a legitimate answer.

Build fields such as `status`, `contaminated`, and `identity_verified` are not
here. They live in the build sidecar (`.build/<slug>/meta/build_state.json`).
Publishing a profile publishes the bibliographic record, not the build's
internal bookkeeping.

Python attribute names stay ergonomic even though the on-disk keys are
schema.org's: `paper.title` is a `str`, `paper.year` is an `int`,
`paper.journal` is a `str`. The JSON-LD shape lives in the serializer, not in
the attribute types.

Full text, when present, lives separately at `sources/papers/<paper_id>.md`,
extracted to plain text; raw HTML is not a valid profile member.

### `sources/summaries/<paper_id>.summary.md`

One Markdown summary per paper, named by the paper's `paper_id`. The SDK exposes
these as a lazy mapping of `paper_id -> markdown body`. Summaries may optionally
begin with a YAML frontmatter block conforming to the `SummaryFile` schema
(`paper_id`, `source_kind`, `source_hash`, `written_at`); when present that block
records provenance. The loader reads the whole file as the summary body and does
not require frontmatter.

### `sources/grants.jsonld`, `sources/cv.md`, `sources/web/`: research outputs beyond papers

These three are the supplied private resources that define a `deep` profile
(see [Profile depth levels](#profile-depth-levels)); `lite` and `full` profiles do
not have them.

`grants.jsonld` is a `Collection` of `MonetaryGrant` nodes. Like papers, each
grant is a research output carrying the same core fields (`type`, `name`,
`@id`), plus grant-specific fields: `funder`, `identifier` (the award number),
`role` (`pi`/`co_pi`/`co_i`/`other`), `status` (`funded`/`pending`/`completed`),
dates, abstract, provenance `source`, and URL. As with the works document, a bare
top-level list is rejected.

Non-paper research outputs that don't have their own collection file — software,
datasets, standards — live in the `researchOutputs` array in `profile.jsonld`
(see the [spec](../rp-spec/index.md#the-researchoutputs-array)).

schema.org has no crisp "this person received this grant" relation, so the
person-to-grant link is `rp:heldGrant`, an `rp:` extension documented in
`context/README.md`.

`cv.md` is the person's CV converted to Markdown, and `web/<n>-<host>.md` holds
extracted main text from each of their websites, one file per page. Both carry a
YAML frontmatter block recording provenance (`source`/`url` and `fetched_at`); the
frontmatter is stripped before indexing so it never becomes searchable content.

Each of the three is independently optional: a deep profile with grants but no CV
is well-formed. At least one must be present, because it is what makes the
profile `deep` rather than `full`.

### `.cache/embeddings.sqlite`

An optional per-profile vector index built by the `[vectors,st]` extras. It stores
chunk-level embeddings of the expertise sections, the SOUL document, and each
paper summary, along with index metadata (the backend name, schema version, build
timestamp). Because it lives inside the profile directory, the profile remains
self-contained and portable. See
[Build and search the embeddings index](how-to/embeddings-index.md).

This file is a build-time format; it is never published. The published tree uses
flat float32 blobs (`embeddings/<model>.bin`) and JSON sidecar files
(`embeddings/index.json`, `embeddings/<model>.chunks.json`) instead. See the
[Embeddings specification](../rp-spec/embeddings.md) for the published format.

What the index contains depends on the profile's [depth level](#profile-depth-levels).
A `full` profile indexes the expertise sections, the SOUL document, and the paper
summaries (`expertise`, `soul`, `paper_summary` chunks). A `lite` profile has none
of those, so its index is abstract-only: it holds `paper_abstract` chunks built
from each paper's abstract text and nothing else. A `deep` profile indexes
everything `full` does plus a `grant`, `cv`, or `web` chunk set for whichever
non-paper sources it actually carries.

The indexed text is expertise, SOUL, summaries, and abstracts; the JSON-LD
record itself is not chunked.

## A spec plus an SDK

The package plays two roles at once.

As a specification, the package defines what a valid profile is. The
authoritative definition is the set of Pydantic v2 models in
`researcher_profiles.schema` (published artifacts),
`researcher_profiles.build_state` (the build sidecar), and
`researcher_profiles.api_models` (HTTP wire types). JSON Schema files generated
from those models are checked into `schemas/`, so a consumer can validate a
profile directory without installing the package, using any JSON Schema
validator in any language.

As an SDK, the package gives you everything you can do with a conforming
profile: load and inspect it (`ResearcherProfile`), collect many of them
(`ProfileStore`), search over them (the `[vectors,st]` extras), serve them over
HTTP (the `[api]` extra), consume them remotely (the `[client]` extra), generate
grounded text as the researcher (the `[llm]` extra), and load them into a
relational database (the `[sql]` extra).

## Profile depth levels

Not every profile carries the same amount of content. A profile has a level
(`lite`, `full`, or `deep`) that records how deeply it was built. The field
lives in `profile.jsonld` and is written explicitly on every profile by
convention; it defaults to `full` when absent. A profile is *built at a level*
by whatever process emits it; the package only reads the level, it does not
set it.

The three tiers form a single axis of increasing depth:

- `lite`: a public bibliometric identity, meaning metadata plus the works list,
  with an abstract-only search index (`paper_abstract` chunks over paper
  abstracts). A `lite` profile has no per-paper summaries, no `expertise.md` or
  `SOUL.md`, and no persona. It is the shallowest useful profile.
- `full`: the complete bundle of paper summaries, `personality/expertise.md`,
  `personality/SOUL.md`, and a full index over all of those. A `full` profile is
  persona-ready.
- `deep`: everything `full` has, over an exhaustive corpus, plus non-paper
  sources: `sources/grants.jsonld`, `sources/cv.md`, and `sources/web/`. Its index
  carries `grant`/`cv`/`web` chunks alongside full's.

### Levels are also an input hierarchy

The more useful way to read the tiers is by what you must supply to reach
each one:

| Level | What you must provide | Where the content comes from |
|---|---|---|
| `lite` | name + rid | public APIs only (ORCID, OpenAlex) |
| `full` | name + rid | public APIs + public PDFs |
| `deep` | name + rid and grant records and/or a CV and/or website URLs | the above, plus supplied private resources |

`lite` and `full` are fully automatable from a seed identity: everything they
contain is publicly discoverable. `deep` is not, because grant records, a CV,
and a person's website URLs cannot be found through any public API. Someone
has to supply them, per person. A deep profile's value comes from
those supplied inputs, so a deep build with none of them configured is
functionally a `full` build, and a producer should refuse it rather
than emit it as deep.

The supplied inputs are named in the build sidecar
(`.build/<slug>/meta/build_state.json`, under `inputs`): `grants_source`
(`grants-data` / `manual` / `none`), `reporter_supplement` (an opt-in public NIH
RePORTER supplement), `cv_source` (a path or URL), and `websites` (a list of
URLs). They are build *inputs*, not published record. What the profile publishes
is the *result*: a CV manifest entry, web pages, a grants collection.

### Level and persona availability

The level decides whether a profile has a persona. A profile
has a *synthesized persona* when it carries both a non-empty `expertise.md` and
a non-empty `SOUL.md`, the two documents the persona methods build their system prompt from.
Only `full` and `deep` profiles expose a persona; a `lite` profile never
synthesizes those documents and so is never persona-ready.

This is exactly what the [`has_persona`](reference/python-api.md#researcherprofile)
property reports, and it is what the persona methods
([`ask` / `review` / `innovate` / `riff`](how-to/persona-methods.md)) gate on:
calling one on a profile without a synthesized persona raises
[`PersonaUnavailableError`](reference/python-api.md#researcherprofile) rather than
role-playing an empty persona. Over HTTP the same condition returns
[409](reference/api.md#the-four-persona-endpoints).

## Creation is out of scope

The package does not care how a profile is created. It does not tell you
how to build a profile, and it does not ship a build pipeline. A conforming
profile directory can be produced by any process that emits the format: written
by hand (as in the [tutorial](tutorial.md)), generated by a script, or assembled
by a larger pipeline. The package's contract is narrow and stable: define the
format, validate against it, and provide the tools to consume it. See
[How to create a profile](how-to/create-a-profile.md) for a rundown of these
options, including a prompt to bootstrap a starter profile with an LLM.

Validation is fully available in a core install: `rp validate` and the
`ResearcherProfile.validate()` method both run the on-disk format check with no
extra required. See [Validate a profile](how-to/validate-a-profile.md).
