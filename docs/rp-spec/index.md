# Researcher Profile Specification

## 1. What a Researcher Profile is

The Researcher Profile specification defines two things:

1. A schema defining what a Researcher Profile is. The `profile.jsonld`
   document extends [schema:Person](https://schema.org/Person), adding
   researcher-specific fields (expertise, provenance, career stage, paper
   statistics) to the standard Person schema, plus a manifest that lists all
   associated files.

2. A file structure for deeper profile data: content too large, or the
   wrong shape, to embed in JSON, such as Markdown prose (expertise,
   summaries), JSON collections (papers, grants), and binary data
   (embeddings).

The document references these files via its manifest (`hasPart` and `subjectOf`
arrays). Together they form the profile.


### Example

#### The core `profile.jsonld` document

This document contains the core metadata about the researcher, and it also
serves as the manifest, with a list of pointers to external information that
did not fit inside this core JSON document.

```json
{
  "@context": "https://profiles.databio.org/context/v1.jsonld",
  "@id": "https://orcid.org/0000-0002-1825-0097",
  "@type": "Person",
  "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
  "name": "Ada Lovelace",
  "rid": "0000-0002-1825-0097",
  "provenance": "third_party",
  "license": "https://creativecommons.org/licenses/by/4.0/",
  "level": "full",
  "affiliation": "Analytical Society",
  "field": "Mathematics",
  "summary": "Mathematician who wrote the first published algorithm intended for a machine.",
  "expertise": [
    "symbolic-computation",
    "exposition-of-machinery"
  ],
  "paper_stats": {
    "corresponding": 2,
    "first": 2,
    "last": 0,
    "middle": 0,
    "total": 2,
    "unknown": 0,
    "year_max": 1843,
    "year_min": 1843
  },
  "hasPart": [
    {
      "@type": "Collection",
      "name": "Works",
      "visibility": "public",
      "role": "works",
      "encodingFormat": "application/ld+json",
      "contentUrl": "sources/papers.jsonld"
    },
    {
      "@type": "DigitalDocument",
      "name": "Summary: lovelace1843notes",
      "visibility": "public",
      "role": "paper_summary",
      "paperId": "lovelace1843notes",
      "encodingFormat": "text/markdown",
      "contentUrl": "sources/summaries/lovelace1843notes.summary.md"
    }
  ],
  "subjectOf": [
    {
      "@type": "DigitalDocument",
      "name": "Expertise",
      "visibility": "public",
      "role": "expertise",
      "encodingFormat": "text/markdown",
      "contentUrl": "personality/expertise.md"
    }
  ]
}
```

#### Additional profile documents (the backing files)

```
lovelace/
  profile.jsonld
  personality/
    expertise.md
    SOUL.md
  sources/
    papers.jsonld
    summaries/
      lovelace1843.summary.md
      ...
  ...
```

## 2. Terminology

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD",
"SHOULD NOT", "RECOMMENDED", "NOT RECOMMENDED", "MAY", and "OPTIONAL" are
interpreted as described in RFC 2119/8174 when in ALL CAPITALS.

| Term | Definition |
|------|------------|
| **Researcher Profile (RP)** | A directory of files conforming to this specification describing one researcher. |
| **Profile** | Convenience alias for *Researcher Profile* when used in this context. |
| **Base URL** (`<base>`) | The URL prefix under which a profile's files are served. |
| **Artifact** | Any file the profile enumerates in its manifest. |
| **Publisher** | The entity serving the profile at Base URL `<base>`. |
| **Subject** | The researcher the profile describes. Exactly one per profile. |
| **Consumer** | Software that reads a published profile. |

## 3. Specification

### 3.1. The `profile.jsonld` document

The `profile.jsonld` file is a JSON-LD document that extends [schema:Person](https://schema.org/Person)
with researcher-specific fields. It serves two roles. It is structured metadata
about the researcher, and it is the external file manifest: the `schema:hasPart`
and `schema:subjectOf` arrays list every external file attached to this profile.

#### Top-level fields

Three fields are REQUIRED for validation: a document missing any of them
fails to load. Five more are required on the wire; every conforming
published document carries them, but the reference implementation fills
them in when absent, so an authoring tool may omit them and still produce a
conformant document once loaded and re-saved.

| Field | Required | Description |
|-------|----------|-------------|
| `name` | REQUIRED | Non-empty display name |
| `rid` | REQUIRED | Canonical ORCID or `local:<slug>-<hex6>` |
| `provenance` | REQUIRED | `orcid_verified`, `self_published`, `third_party`, `synthetic`, or `historical` |
| `@context` | required on the wire, defaulted on load | `"https://profiles.databio.org/context/v1.jsonld"` (exact string); absent is filled in with this value |
| `@type` | required on the wire, defaulted on load | `"Person"`; absent is filled in |
| `conformsTo` | required on the wire, defaulted on load | Equals the `@context` IRI; a present but wrong value fails to load, an absent value is filled in |
| `@id` | required on the wire, defaulted on load | ORCID IRI, profile URL, or `#me`; absent is filled in with `url` if set, else `#me` |
| `level` | required on the wire, defaulted on load | `lite`, `full`, or `deep` (see below); absent is filled in as `full` |
| `license` | RECOMMENDED | SPDX IRI or license URL |
| `dateModified` | RECOMMENDED | ISO 8601 timestamp |
| `visibility` | optional | `public`, `internal`, or `restricted` (default: `public`) |
| `expertise` | optional | Array of topic labels |
| `not_interests` | optional | Authoritative non-interests |
| `career_stage` | optional | Date-anchored eligibility facts (see below) |
| `collaborators` | optional | Declared connections to other researchers (see below) |
| `researchOutputs` | optional | Research outputs other than papers: grants, software, datasets, protocols, etc. (see below) |

##### Profile levels

The `level` field describes how much content a profile contains:

| Level | Description |
|-------|-------------|
| `lite` | Public bibliometric identity: metadata + works list. No summaries, no persona. |
| `full` | Complete persona: paper summaries, `expertise.md`, `SOUL.md`, full embedding index. |
| `deep` | Everything in `full` plus grants, CV, and web sources. |

##### The `career_stage` object

The `career_stage` field is an object (not a simple value) that records facts
for grant eligibility evaluation:

| Field | Required | Description |
|-------|----------|-------------|
| `as_of` | REQUIRED | ISO date when facts were assessed |
| `terminal_degree_year` | optional | Integer or null |
| `first_r01_equivalent_year` | optional | First R01/DP1/DP2/etc. |
| `tenure_status` | REQUIRED | Enum value |
| `independence` | REQUIRED | Enum value |
| `evidence` | REQUIRED | Citation of sources |
| `confidence` | REQUIRED | `high`, `medium`, or `low` |

##### The `collaborators` array

The `collaborators` field records the researcher's declared connections.
It is mapped to `schema:knows` in the JSON-LD context.

Each entry is one of:

- A string: a name alone (`"Alice Smith"`)
- An object with structured details:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | REQUIRED | Display name |
| `affiliation` | optional | Institutional affiliation |
| `relationship` | optional | How they are connected (see recommended values below) |
| `url` | optional | The collaborator's profile base URL (enables network traversal) |

The `relationship` field is a free string: publishers MAY use any descriptive
value. No formal ontology for researcher-to-researcher relationship types
exists; in its absence, publishers SHOULD use one of these RECOMMENDED values,
listed from strongest to weakest:

| Priority | Value | Meaning |
|----------|-------|---------|
| 1 | `advisor` | Served as this researcher's advisor (doctoral, postdoctoral) |
| 2 | `advisee` | Was advised by this researcher |
| 3 | `coauthor` | Has co-authored papers with this researcher |
| 4 | `colleague` | Works at the same institution or in the same group |
| 5 | `collaborator` | General research collaboration |

Each collaborator entry carries a single `relationship` value. When multiple
relationships apply (e.g. an advisor who is also a coauthor), publishers
SHOULD use the strongest one (the highest in the priority list). Weaker
relationships like co-authorship are already discoverable from the paper
record, so declaring the relationship that is not in the bibliography adds
more information.

Consumers MUST handle unknown relationship values gracefully.

The `url` field, when present, points to a hosted researcher profile.
Because the JSON-LD context defines `url` as `schema:url` with `@type: @id`,
a JSON-LD processor treats it as an IRI, linking this person node to the
collaborator's profile. Plain JSON consumers see it as an ordinary URL string.

Connections are unidirectional: if profile A lists B, B does not
automatically list A. Each profile declares its own connections independently.

**Examples:**

Name-only:

```json
"collaborators": ["Alice Smith", "Bob Jones"]
```

Structured with profile link:

```json
"collaborators": [
  {
    "name": "Alice Smith",
    "affiliation": "Example University",
    "relationship": "coauthor",
    "url": "https://profiles.example.org/p/alice-smith"
  }
]
```

Using `@id` to make the collaborator entry itself a linked-data node
(pointing at the collaborator's profile):

```json
"collaborators": [
  {
    "@id": "https://profiles.example.org/p/alice-smith",
    "@type": "Person",
    "name": "Alice Smith",
    "affiliation": "Example University",
    "relationship": "coauthor"
  }
]
```

In this form a JSON-LD processor resolves the `@id` as the collaborator's
identity IRI. This is the strongest form of linking: it asserts that the
collaborator is the person described at that URL, rather than only that a URL
exists for them.

##### The `researchOutputs` array

The `researchOutputs` field records non-paper outputs the subject produced:
software, datasets, standards, and anything else that is a research product but
not a publication. Papers are not listed here; they live in
`sources/papers.jsonld`. This array is about what the researcher made, not
about the files in the profile directory (those are the manifest, below).

Each entry is a **research output**, the format's base type for everything a
researcher produces. Every research output carries the same core:

| Field | Required | Description |
|-------|----------|-------------|
| `type` | REQUIRED | Free-form token naming the kind of output, e.g. `software`, `dataset`, `standard` |
| `name` | REQUIRED | Display name of the output |
| `description` | optional | One or two sentences on what it is |
| `url` | optional | Where the output lives |
| `@id` | optional | Identity IRI for the output: a DOI, a repository URL, a grant IRI |
| `@type` | optional | JSON-LD node type, when the output has a meaningful one |

**Recommended `type` vocabulary.** The following tokens are RECOMMENDED so that
consumers can group outputs consistently:

`software`, `dataset`, `protocol`, `reagent`, `model`, `grant`, `abstract`,
`presentation`, `patent`, `standard`.

This is a recommended list, not a closed enum. The `type` vocabulary is open:
publishers MAY use other values, and consumers MUST handle unknown values
gracefully rather than rejecting the entry.

**Specializations.** A research output type MAY specialize the base by adding
its own fields while keeping the core above. A paper record in
`sources/papers.jsonld` is exactly such a specialization: it is a research
output whose `name` is the work's title and whose `@type` is
`ScholarlyArticle`, plus the bibliographic, authorship and access fields a
scholarly work needs. Future output kinds — grants, presentations, patents and
the rest of the recommended vocabulary — are expected to specialize the base
the same way, each adding the fields its kind requires. A consumer that
understands only the core fields can therefore read every research output,
whatever its kind.

**Example:**

```json
"researchOutputs": [
  {
    "name": "KinTool",
    "type": "software",
    "url": "https://kintool.example.org"
  }
]
```

#### The external file manifest

The `hasPart` and `subjectOf` arrays form the external file manifest, a list
of every file attached to the profile:

- `subjectOf`: persona documents (expertise, SOUL)
- `hasPart`: everything else (works, grants, summaries, embeddings)

Each entry is an `ArtifactRef`:

| Field | Required | Description |
|-------|----------|-------------|
| `contentUrl` | REQUIRED | Relative path from profile base |
| `@type` | optional | Defaults to `DigitalDocument` |
| `name` | optional | Human-readable label |
| `role` | optional | Machine token (see vocabulary below) |
| `encodingFormat` | optional | MIME type |
| `paperId` | optional | Links summary/fulltext to its paper |
| `visibility` | optional | Artifact privacy tier (see [Privacy](privacy.md)) |
| `derivedFrom` | optional | Roles or paper ids this artifact was derived from; its effective tier is the most restrictive of its own and its sources' |
| `bytes` | optional | Size for fetch budgeting |
| `sha256` | optional | Integrity check |

Publishers SHOULD set `name`, `role`, and `encodingFormat` on every entry; a
consumer can only find an artifact by `role` when it is present.

A consumer MUST NOT construct artifact URLs by convention. Find the entry
by `role` (or `paperId`) and read its `contentUrl`.

##### Role vocabulary

| Token | Array | Artifact |
|-------|-------|----------|
| `expertise` | subjectOf | `personality/expertise.md` |
| `soul` | subjectOf | `personality/SOUL.md` |
| `topics` | subjectOf | `personality/topics.json` |
| `works` | hasPart | `sources/papers.jsonld` |
| `grants` | hasPart | `sources/grants.jsonld` |
| `citations` | hasPart | `sources/citations.json` |
| `paper_summary` | hasPart | `sources/summaries/<paper_id>.summary.md` |
| `paper_fulltext` | hasPart | `sources/papers/<paper_id>.md` |
| `cv` | hasPart | `sources/cv.md` |
| `web` | hasPart | `sources/web/<n>-<host>.md` |
| `embedding_index` | hasPart | `embeddings/index.json` |
| `embedding_index_sqlite` | hasPart | `.cache/embeddings.sqlite` (tier `restricted`) |
| `agent_entry_point` | hasPart | `SKILL.md` |
| `html` | hasPart | `index.html` |

Consumers MUST ignore unknown `role` values.

#### Provenance and proofs

`provenance` is a headline label for trust level:

| Value | Meaning |
|-------|---------|
| `orcid_verified` | ORCID record links back to this profile |
| `self_published` | Published by the subject, no ORCID verification |
| `third_party` | Published by someone other than the subject |
| `synthetic` | Not a natural person (AI agent, test fixture) |
| `historical` | Real person who cannot hold an ORCID |

The optional `proof` array carries fine-grained verification:

| Proof kind | What it proves |
|------------|----------------|
| `orcid_roundtrip` | ORCID record points back |
| `domain_wellknown` | Control of domain via `.well-known` challenge |
| `key_signature` | Detached JWS over canonicalized document |

Consumers MUST ignore unknown proof kinds.

#### Vocabulary

Two vocabularies carry the data:

| Prefix | IRI | Purpose |
|--------|-----|---------|
| `schema:` | `https://schema.org/` | Person, works, grants, identifiers |
| `rp:` | `https://profiles.databio.org/context/v1.jsonld#` | Profile-specific terms |

The context also maps `conformsTo` to `dcterms:conformsTo` and declares the
`xsd` prefix for typed literals such as `datePublished`.

Consumers can read profiles as plain JSON without dereferencing the context.

### 3.2. File Structure

These files hold data that is too large, or the wrong shape, to embed in JSON.

```
<slug>/
  profile.jsonld              # REQUIRED. The record and manifest
  .publishignore              # derived exclude list (see Privacy)
  index.html                  # landing page (written by `rp render`)
  SKILL.md                    # agent instructions (optional)
  personality/
    expertise.md              # full/deep profiles
    SOUL.md                   # full/deep profiles
    topics.json               # LLM-labeled research topics (optional)
  sources/
    papers.jsonld             # REQUIRED. Works collection
    grants.jsonld             # when grant records exist
    citations.json            # citation graph (optional)
    summaries/<paper_id>.summary.md
    papers/<paper_id>.md      # full text. RESTRICTED, never served
    cv.md                     # deep profiles
    web/<n>-<host>.md         # deep profiles
  embeddings/
    index.json                # Searchable profiles
    <backend>.bin
    <backend>.chunks.json
  .cache/
    embeddings.sqlite         # derived local index. RESTRICTED, never served
  .keys/                      # signing keys. RESTRICTED, never served
```

Build state lives in `.build/<slug>/`, outside the profile directory.

#### File rules

- `<base>/profile.jsonld` MUST resolve: it is the single discovery endpoint.
- All served paths carry an explicit extension (`.jsonld`, `.json`, `.md`, etc.).
- There is no content negotiation, so the same layout works on every static host.
- `paper_id` is chosen by the producer and is opaque to consumers. It names files
  (`sources/summaries/<paper_id>.summary.md`) and fragments (`#paper/<paper_id>`),
  so it SHOULD contain no path separators or whitespace. The reference validator
  does not check its characters.

#### Works collection

`sources/papers.jsonld` is a `Collection` whose `hasPart` contains
`ScholarlyArticle` nodes. Reached via the `works` manifest entry, not by path.

| Field | Required | Description |
|-------|----------|-------------|
| `@type` | REQUIRED | `ScholarlyArticle` |
| `@id` | REQUIRED | DOI IRI > OpenAlex IRI > `#paper/<paper_id>` |
| `name` | REQUIRED | Paper title |
| `paper_id` | RECOMMENDED | Internal ID; joins to summaries |
| `datePublished` | optional | Year as string |
| `author` | RECOMMENDED | `Person` nodes (`{"@type": "Person", "name": ...}`), one per author, in order |

Build fields (`status`, `contaminated`) stay in `.build/`, never here.

#### Grants collection

`sources/grants.jsonld` holds `MonetaryGrant` nodes when present.

| Field | Description |
|-------|-------------|
| `@type` | `MonetaryGrant` |
| `@id` | `#grant/<grant_id>` |
| `name` | Grant title |
| `identifier` | Award number |
| `role` | `pi`, `co_pi`, `co_i`, `other` |
| `status` | `funded`, `pending`, `completed` |

### 3.3. Conformance

Two conformance levels are defined.

#### Base conformance

A profile conforms at Base level when:

1. `<base>/profile.jsonld` resolves via HTTP GET
2. The document validates against `schemas/profile_jsonld.schema.json`
3. `@context` references a known researcher-profile context version
4. `conformsTo` contains the `@context` value
5. `name`, `rid`, and `level` are present and well-formed
6. Every `hasPart` / `subjectOf` entry resolves at its `contentUrl`

#### Searchable conformance

Meets all Base requirements plus:

1. `<base>/embeddings/index.json` resolves and is well-formed
2. The embedding blob contains exactly `count * dim` float32 values
3. `backend_spec` names a baseline model (all-MiniLM-L6-v2, dim 384)
4. Chunk metadata file resolves with `count` entries

See [Embeddings](embeddings.md) for the full embedding format.

#### No self-declaration

Conformance is established by validation, not by declaration. The manifest
reveals whether artifacts exist, but a consumer MUST validate rather than trust.

#### What this spec excludes

- RO-Crate packaging: compatible with future wrapping but not required.
- Central discovery hub: this specification defines no central registry or
  federation protocol. A registry is any host that serves the API, static or
  dynamic, and each one publishes its own `collection.jsonld` listing.
  [Prosopia](https://village.databio.org/prosopia/) is one hosted registry.
- Chatbot interface: persona methods are SDK features, not format.
- SPARQL: JSON-LD is for crawlers and agents, not triplestores.
- Profile creation: how to build is out of scope.
- DIDs/Verifiable Credentials: identity uses a simpler proof envelope.
- Accounts and identity providers: how a server authenticates a person
  (institutional SSO, ORCID, e-mail) is out of scope. The
  [management tier](dynamic-api.md#14-management-api) specifies only what a
  credential looks like once issued.
