# JSON Schema reference

The `schemas/` directory holds JSON Schema (draft 2020-12) files. Every
schema is generated from the package's Pydantic models in
`researcher_profiles.schema` by `schema_export.py`. They exist so a consumer
can validate profiles without installing this package, in any language with
a draft-2020-12 validator.

## Profile schemas

A profile is one tree, authored and served identically, so each file validates
one on-disk artifact. There is no separate set of "published" schemas.
`profile.jsonld` and `sources/papers.jsonld` are validated by the same schemas
whether the profile is on disk or on the web.

| File | Validates (on-disk path) | Source model | Root type | `additionalProperties` |
|---|---|---|---|---|
| `profile_jsonld.schema.json` | `profile.jsonld` | `ProfileDocument` | object + `$defs` | allowed (tolerant) |
| `papers_jsonld.schema.json` | `sources/papers.jsonld` | `PapersDocument` (wraps `PaperRecord` via `$defs`) | object + `$defs` | allowed on `PaperRecord` (tolerant) |
| `grants_jsonld.schema.json` | `sources/grants.jsonld` | `GrantsDocument` (wraps `GrantRecord` via `$defs`) | object + `$defs` | allowed on `GrantRecord` (tolerant) |
| `summary_file.schema.json` | frontmatter of `sources/summaries/*.summary.md` | `SummaryFile` | object | **forbidden (strict)** |

## Interchange schemas

One schema in `schemas/` is not an on-disk artifact. It describes a payload
handed to another system at export time, and nothing ever writes it into a
profile directory. It is published here so a non-Python consumer can validate
the payload without installing this package.

| File | Payload | Model | Extra keys |
|---|---|---|---|
| `profile_export_bundle.schema.json` | the bundle `rp export --json` / `build_export_bundle()` hands a knowledge base | `ProfileExportBundle` | **forbidden (strict)** |

Its `content_hash` covers every field except itself and `built_at`, so a
consumer upserts only when the hash changes. See
[Export for knowledge bases](python-api.md#export-for-knowledge-bases).

## Site and index schemas

These validate the collection-level and embedding artifacts of the researcher-profile
specification.

| File | Validates (path) | Source model |
|---|---|---|
| `embedding_index.schema.json` | `embeddings/index.json` | `EmbeddingIndex` |
| `profile_list.schema.json` | A hand-authored or dynamically served profile list (see [Static API §8](../../rp-spec/static-api.md#8-profile-lists)); not what `rp site` writes to a static site | `ProfileListDocument` |
| `collection.schema.json` | Collection bundle | `ProfileCollection` |
| `topic_index.schema.json` | `collection/topics.json` | `TopicIndexDocument` |

See the [researcher-profile specification](../../rp-spec/index.md) for the normative requirements
that go beyond what JSON Schema can express (artifact resolution, manifest
fidelity, fragment uniqueness, embedding blob integrity, privacy tiers). Those
are checked by the two conformance validators over the shared corpus in
`spec/conformance/`.

The schemas emit the JSON-LD names: `@context`, `@id`, `@type`,
`conformsTo`, `sameAs`, `hasPart`, `subjectOf`, not the Python attribute names
(`conforms_to`, `id_`, `same_as`). `tests/test_schema.py` pins that, so a
model-config change cannot silently publish Python names into the contract.

"Tolerant" models (`profile_jsonld`, `PaperRecord`, `GrantRecord`) set
`additionalProperties: true`: unknown keys validate and are preserved.
Conformance means the baseline fields are present and well-formed; additional
keys are permitted, and consumers must ignore unknown keys rather than treat
them as failures. The
strict model `summary_file` sets `additionalProperties: false`.

Optional fields render in the schema as `anyOf [ {type}, {"type": "null"} ]`;
this reference writes that as `type | null`.

---

## Regeneration workflow and invariant

The checked-in `schemas/*.json` files are generated artifacts. Regenerate
them from the models with the CLI:

```bash
rp schema export schemas/
```

or programmatically:

```python
from researcher_profiles.schema_export import export_schemas, build_schemas

export_schemas("schemas")  # writes <name>.schema.json files
build_schemas()  # returns {name: schema_dict} without writing
```

The checked-in `schemas/*.json` must be regenerated whenever a model in
`researcher_profiles.schema` changes. A schema file that disagrees with its
model is a bug. The models are authoritative. This is a maintainer convention:
run the export command after editing a model and commit the regenerated files.
There is currently no automated test that compares the checked-in bytes to a
fresh export (see [Test coverage](#test-coverage) below).

Output format (so a reviewer can eyeball a diff): one `<name>.schema.json` per
model, JSON with sorted keys, 2-space indent, and a trailing newline.

---

## Validation without installing the package

Both recipes below use only third-party tools, with no `researcher-profiles`
install required.

### (a) Python with the third-party `jsonschema` package

```python
import json
from jsonschema import Draft202012Validator

schema = json.load(open("schemas/profile_jsonld.schema.json"))
data = json.load(open("jane-doe/profile.jsonld"))
Draft202012Validator(schema).validate(data)  # raises on mismatch
```

Notes:

1. Every published artifact is JSON. `profile.jsonld`,
   `sources/papers.jsonld`, and `sources/grants.jsonld` load with `json.load`,
   with no YAML step. Only summary frontmatter is YAML (`yaml.safe_load`).
2. `$defs` resolve internally. The published schemas contain `$defs`; a
   compliant draft-2020-12 validator resolves these from the single file, with
   no extra registry or reference wiring needed.
3. Pin the dialect. Use `Draft202012Validator` explicitly rather than
   `validate(...)` auto-detection.
4. Schema validity is not conformance. JSON Schema cannot express
   "`conformsTo` must equal this exact IRI" as a hard gate that every
   validator enforces identically, so check it yourself: a document whose
   `conformsTo` is absent or different is not a conforming profile, whatever a
   validator says.

### (b) `check-jsonschema` CLI (no Python authoring)

```bash
check-jsonschema --schemafile schemas/profile_jsonld.schema.json jane-doe/profile.jsonld
```

`check-jsonschema` parses both JSON and YAML natively, so the same command works
for every artifact. Each per-schema section below refers to "the recipe above"
plus its own schema/instance pair.

---

## The `level` field

`level` is a string enum on `profile_jsonld`:

- Type: `string`
- Allowed values: `"lite"`, `"full"`, `"deep"`
- Default (in the schema): `"full"`
- Written explicitly on every profile a build tool emits. A
  published standard does not leave its depth tier implicit; the schema default
  exists only so a hand-written minimal document validates.

This reference documents the field's type/enum/default only. For what the
`lite`/`full`/`deep` tiers *mean* and how they gate the build, see
[Profile format](../profile-format.md).

### The deep-input fields live in the build sidecar

Four fields form the supplied-input contract the `deep` level is defined by:
`grants_source`, `reporter_supplement`, `cv_source`, and `websites`. They are
not in `profile_jsonld`: they are build inputs, and what a profile
publishes is the result (a CV manifest entry, web pages, a grants collection).
They live in the build sidecar (`.build/<slug>/meta/build_state.json`), which
has no published schema.

They are always *schema*-optional: nothing in JSON Schema can express "required
when `level` is deep". That precondition ("a deep build needs at least one
supplied source") belongs to the producing pipeline, not to this format spec. A
deep profile whose sources are all absent is still a structurally valid profile
directory.

---

## `profile_jsonld` (`profile.jsonld`)

The profile record: a `schema:Person` node carrying identity, provenance,
metadata, and the manifest (`hasPart` / `subjectOf`). Source model:
`ProfileDocument` (tolerant, extra keys allowed, string values
whitespace-stripped). `name`, `rid`, and `provenance` are required.

This is the one and only profile document: the same `profile.jsonld` is
authored and served, validated by this schema either way. See
[the profile document](../../rp-spec/index.md#31-the-profilejsonld-document).

| Field | Type | Required? | Default | Constraints / enum | Description |
|---|---|---|---|---|---|
| `@context` | string \| null | no | the context IRI | | The hosted vocabulary. One line per document; never inlined. |
| `@id` | string \| null | no | derived | | Subject IRI: `https://orcid.org/<rid>` for an ORCID rid, else `url`, else `#me`. |
| `@type` | string \| null | no | `"Person"` | | |
| `conformsTo` | string | no | the format IRI | must equal `https://profiles.databio.org/context/v1.jsonld` | The format gate. A document with any other value fails to load. |
| `name` | string | **yes** | none | `minLength: 1` | Researcher display name. |
| `rid` | string | **yes** | none | a canonical ORCID (regex + ISO 7064 checksum) or `local:<slug>-<6 hex>` | The identity and the single cross-system join key. There is no separate `orcid` key. It is derived from this. |
| `provenance` | string enum | **yes** | none | `orcid_verified` \| `self_published` \| `third_party` \| `synthetic` \| `historical` | Who asserted this profile and on what basis. No default: an unlabeled published assertion is the failure mode this field prevents. |
| `verifiedAt` | string \| null | no | `null` | required when `provenance` is `orcid_verified` | When the ORCID round-trip was checked. |
| `license` | string \| null | no | `null` | an IRI | Reuse terms for the published record. |
| `url` | string \| null | no | `null` | | The published profile URL. Required by `orcid_verified`. |
| `dateModified` | string \| null | no | `null` | | |
| `sameAs` | list[string] | no | `[]` | | Other URLs for the same person (Scholar, lab site, homepage). |
| `identifier` | list[`Identifier`] | no | `[]` | `PropertyValue` nodes | Non-`@id` identifiers (OpenAlex, Scopus, ...). |
| `hasPart` | list[`ArtifactRef`] | no | `[]` | | The manifest: every artifact in the profile, relatively linked. |
| `subjectOf` | list[`ArtifactRef`] | no | `[]` | | The persona documents (SOUL, expertise). |
| `level` | string enum | no | `"full"` | `lite` \| `full` \| `deep` | Profile depth tier. See [The `level` field](#the-level-field). |
| `affiliation` | string \| null | no | `null` | | Institutional affiliation. |
| `scholar_url` | string \| null | no | `null` | | Google Scholar profile URL. |
| `openalex_id` | string \| null | no | `null` | | OpenAlex author ID. |
| `field` | string \| null | no | `null` | | Primary field. |
| `subfields` | list[string] | no | `[]` | | Subfield labels. |
| `summary` | string \| null | no | `null` | | Prose overview. |
| `training` | list[`Training`] | no | `[]` | see `Training` sub-table | Education / training history. |
| `career` | list[`CareerEntry`] | no | `[]` | see `CareerEntry` sub-table | Positions held. |
| `expertise` | list[string] | no | `[]` | | Expertise topic labels. |
| `interests` | list[string] | no | `[]` | | Research interests. |
| `not_interests` | list[string] | no | `[]` | | Explicit non-interests. |
| `methodological_commitments` | list[string] | no | `[]` | | Methodological stances. |
| `recurring_positions` | list[string] | no | `[]` | | Positions taken repeatedly. |
| `intellectual_lineage` | list[string] | no | `[]` | | Intellectual influences. |
| `critiques` | list[string] | no | `[]` | | Recurring critiques. |
| `researchOutputs` | list[`ResearchOutput`] | no | `[]` | see `ResearchOutput` sub-table | Software / datasets / other outputs. |
| `collaborators` | list[string \| object] | no | `[]` | each item is a string OR `{name, affiliation, relationship}` object | Collaborators. |
| `anchor` | `Anchor` \| null | no | `null` | see `Anchor` sub-table | Disambiguation evidence. |
| `paper_stats` | `PaperStats` \| null | no | `null` | see `PaperStats` sub-table | Author-position counts and year range. |
| `career_stage` | `CareerStage` \| null | no | `null` | see `CareerStage` sub-table | Date-anchored eligibility facts. |

### Nested `$defs` sub-objects

**`Training`** (strict; `kind`, `degree` and `institution` required). A postdoc
is a `kind: postdoc` span entry, never a degree. For a degree, `year_end` is the
completion year:

| Field | Type | Required? | Default |
|---|---|---|---|
| `kind` | `degree` \| `postdoc` \| `clinical_training` | **yes** | none |
| `degree` | string | **yes** | none |
| `institution` | string | **yes** | none |
| `year_start` | integer \| null | no | `null` |
| `year_end` | integer \| null | no | `null` |
| `advisor` | string \| null | no | `null` |

**`CareerEntry`** (strict; `role` and `institution` required). A `null`
`end_year` on a held position means "to present"; a `null` `start_year` means
the start is unknown:

| Field | Type | Required? | Default |
|---|---|---|---|
| `role` | string | **yes** | none |
| `institution` | string | **yes** | none |
| `start_year` | integer \| null | no | `null` |
| `end_year` | integer \| null | no | `null` |

**`CareerStage`** (strict). Date-anchored eligibility facts, never verdicts:
an evaluator applies a funder's rule to these at evaluation time. See
[the spec](../../rp-spec/index.md#the-career_stage-object)
for the full member table and the NIH R01-equivalent list. `as_of`,
`tenure_status`, `independence`, `evidence`, and `confidence` are required;
every other member may be `null`/`unknown`, meaning "not determinable".

**`ResearchOutput`** (tolerant; `type` and `name` required). This is the base
type for every research output, not only the non-paper ones: `PaperRecord`
subclasses it, and future output kinds (grants, presentations, patents) are
meant to subclass it too. It is a `JsonLdModel`, so every output may carry
`@id` and `@type`. The `type` token is free text; the recommended vocabulary is
`software`, `dataset`, `protocol`, `reagent`, `model`, `grant`, `abstract`,
`presentation`, `patent`, `standard`:

| Field | Type | Required? | Default |
|---|---|---|---|
| `type` | string | **yes** | none |
| `name` | string | **yes** | none |
| `description` | string \| null | no | `null` |
| `url` | string \| null | no | `null` |
| `@context` | string \| null | no | `null` |
| `@id` | string \| null | no | `null` |
| `@type` | string \| null | no | `null` |

**`Anchor`** (tolerant; all optional):

| Field | Type | Default |
|---|---|---|
| `disambiguation_evidence` | string \| null | `null` |
| `confidence` | string \| null | `null` |

**`PaperStats`** (tolerant; all integer, default `0`): `first`, `last`,
`middle`, `unknown`, `corresponding`, `total`, `year_min`, `year_max`.

Minimal valid example (`profile.jsonld`):

```json
{
  "@context": "https://profiles.databio.org/context/v1.jsonld",
  "@id": "https://orcid.org/0000-0002-1825-0097",
  "@type": "Person",
  "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
  "name": "Jane Doe",
  "rid": "0000-0002-1825-0097",
  "provenance": "third_party",
  "field": "Computational biology",
  "expertise": ["genomics", "data standards"],
  "training": [
    { "kind": "degree", "degree": "PhD", "institution": "Example University",
      "year_end": 2015 }
  ]
}
```

Validate with the recipe above:
`check-jsonschema --schemafile schemas/profile_jsonld.schema.json jane-doe/profile.jsonld`.

---

## `papers_jsonld` (`sources/papers.jsonld`)

The wrapper is a `Collection` node whose `hasPart` holds the works. A bare
top-level list is rejected: the model's `_reject_bare_list` validator refuses
it, and the JSON schema independently rejects it because the
root type is `object`, not `array`.

| Field | Type | Required? | Default | Description |
|---|---|---|---|---|
| `@context` | string \| null | no | the context IRI | |
| `@type` | string \| null | no | `"Collection"` | |
| `conformsTo` | string | no | the format IRI | The format gate. |
| `about` | string \| null | no | `null` | The `@id` of the person this collection belongs to. |
| `dateModified` | string \| null | no | `null` | When the collection was last reviewed. |
| `hasPart` | list[`PaperRecord`] | no | `[]` | The works. `$ref`s `PaperRecord` in `$defs`. |

A work's `@id` resolves to the DOI IRI, else the OpenAlex IRI, else
`#paper/<paper_id>`. Bibliographic keys use the schema.org names: `name`
(title), `datePublished` (`xsd:gYear` string), `isPartOf` (a `Periodical`
node), `author` (a list of `Person` nodes).

**`PaperRecord`** (lives under `$defs`; tolerant, extra keys allowed; only
`name` required). It subclasses `ResearchOutput`: a paper is a research output,
so it inherits `name`, `type`, `description` and `url` and adds the
bibliographic, authorship and access fields. No build field appears here:
`identity_verified`, `status`, and `contaminated` all live in the unpublished
`build_state.json` sidecar instead. The table below has two
name columns because several fields are exposed under an ergonomic
Python attribute name but stored on disk under the schema.org key
(`PaperRecord` in `schema/_sources.py`):

| On-disk key | Python attribute | Type | Required? | Default | Notes |
|---|---|---|---|---|---|
| `name` | `name` (read as `.title`) | string | **yes** | none | Inherited from `ResearchOutput`. `title=` is accepted on construction and `.title` reads it back. |
| `datePublished` | `year` | integer \| null | no | `null` | `xsd:gYear` string on disk, coerced to `int`. |
| `isPartOf` | `journal` | string \| null | no | `null` | A `Periodical` node on disk, flattened to a string. |
| `author` | `authors` | list[string] \| null | no | `null` | A list of `Person` nodes on disk, flattened to strings. |
| `@context` | `context` | string \| null | no | `null` | |
| `@id` | `id_` | string \| null | no | `null` | Resolves DOI IRI -> OpenAlex IRI -> `#paper/<paper_id>` when absent. |
| `@type` | `type_` | string \| null | no | `"ScholarlyArticle"` | |
| `paper_id` | `paper_id` | string \| null | no | `null` | Citation key; ties papers to summaries. |
| `doi` | `doi` | string \| null | no | `null` | |
| `pmid` | `pmid` | string \| null | no | `null` | |
| `pmcid` | `pmcid` | string \| null | no | `null` | |
| `openalex_id` | `openalex_id` | string \| null | no | `null` | |
| `venue` | `venue` | string \| null | no | `null` | |
| `type` | `type` | string \| null | no | `null` | Work type, e.g. `authored`. Unrelated to `@type`. |
| `first_author` | `first_author` | string \| null | no | `null` | |
| `last_author` | `last_author` | string \| null | no | `null` | |
| `citation` | `citation` | string \| null | no | `null` | |
| `cited_by_count` | `cited_by_count` | integer \| null | no | `null` | |
| `abstract` | `abstract` | string \| null | no | `null` | |
| `summary` | `summary` | string \| null | no | `null` | Short inline summary (distinct from the summary file). |
| `author_position` | `author_position` | string \| null | no | `null` | Free string; the reference build uses `first` / `middle` / `last` / `unknown`. |
| `author_index` | `author_index` | integer \| null | no | `null` | |
| `total_authors` | `total_authors` | integer \| null | no | `null` | |
| `is_corresponding` | `is_corresponding` | boolean \| null | no | `null` | |
| `open_access` | `open_access` | boolean \| null | no | `null` | |
| `is_oa` | `is_oa` | boolean \| null | no | `null` | |
| `oa_status` | `oa_status` | string \| null | no | `null` | |
| `oa_url` | `oa_url` | string \| null | no | `null` | |
| `pdf_url` | `pdf_url` | string \| null | no | `null` | |
| `url` | `url` | string \| null | no | `null` | |
| `full_text_link` | `full_text_link` | string \| null | no | `null` | |
| `access` | `access` | string \| null | no | `null` | |
| `source` | `source` | string \| null | no | `null` | |

Minimal valid example (`sources/papers.jsonld`), a real excerpt from
`rp-sdk/tests/fixtures/jane-doe/sources/papers.jsonld`:

```json
{
  "@context": "https://profiles.databio.org/context/v1.jsonld",
  "@type": "Collection",
  "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
  "about": { "@id": "https://orcid.org/0000-0002-1825-0097" },
  "hasPart": [
    {
      "@id": "#paper/doe2016example",
      "@type": "ScholarlyArticle",
      "name": "ExampleOverlap: enrichment analysis of example region sets",
      "paper_id": "doe2016example",
      "datePublished": "2016",
      "isPartOf": { "@type": "Periodical", "name": "Journal of Synthetic Genomics" },
      "author_position": "first"
    }
  ]
}
```

Counter-example: a bare list fails validation:

```json
// INVALID: top-level list, not {"hasPart": [...]}
[
  { "name": "A representative paper", "paper_id": "smith2020" }
]
```

Validate with the recipe above:
`check-jsonschema --schemafile schemas/papers_jsonld.schema.json jane-doe/sources/papers.jsonld`.

---

## `grants_jsonld` (`sources/grants.jsonld`)

Grant records for a `deep` profile. `lite` and `full` profiles do not have this
file; its absence is not an error.

The wrapper mirrors `papers_jsonld`: a `Collection` node whose `hasPart` holds
the grants. A bare top-level list is rejected, by the model's
`_reject_bare_list` validator and independently by the schema's `object` root
type.

| Field | Type | Required? | Default | Description |
|---|---|---|---|---|
| `@context` | string \| null | no | the context IRI | |
| `@type` | string \| null | no | `"Collection"` | |
| `conformsTo` | string | no | the format IRI | The format gate. |
| `about` | string \| null | no | `null` | The `@id` of the person. |
| `hasPart` | list[`GrantRecord`] | no | `[]` | The grants. `$ref`s `GrantRecord` in `$defs`. |

schema.org has no crisp "this person received this grant" relation, so the
link from a person to a grant is `rp:heldGrant`, an `rp:` term. See
`context/README.md`.

**`GrantRecord`** (lives under `$defs`; tolerant, extra keys allowed; `id` and
`name` required):

| Field | Type | Required? | Default | Constraints / enum |
|---|---|---|---|---|
| `@type` | string \| null | no | `"MonetaryGrant"` | |
| `id` | string | **yes** | none | Stable key for the grant; also the chunk `source_id` in the index. |
| `name` | string | **yes** | none | The grant title. |
| `funder` | string \| null | no | `null` | e.g. `NIH`, `NSF`. Serializes as an `Organization` node; stays a `str` in Python. |
| `identifier` | string \| null | no | `null` | Award / application number. |
| `activity_code` | string \| null | no | `null` | NIH activity code (`R01`, `K99`, ...), so R01-equivalent history is recoverable without parsing the award number. |
| `role` | string enum \| null | no | `null` | `pi` \| `co_pi` \| `co_i` \| `other` |
| `status` | string enum \| null | no | `null` | `funded` \| `pending` \| `completed` |
| `start` | string \| null | no | `null` | Start date. |
| `end` | string \| null | no | `null` | End date. |
| `abstract` | string \| null | no | `null` | Indexed alongside the title as `grant` chunks. |
| `source` | string enum \| null | no | `null` | `grants-data` \| `manual` \| `reporter`: provenance. |
| `url` | string \| null | no | `null` | |

Minimal valid example (`sources/grants.jsonld`):

```json
{
  "@context": "https://profiles.databio.org/context/v1.jsonld",
  "@type": "Collection",
  "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
  "hasPart": [
    {
      "@type": "MonetaryGrant",
      "id": "nih-r01-example",
      "name": "Scalable epigenome data infrastructure"
    }
  ]
}
```

Counter-example: a bare list fails validation:

```json
[ { "id": "nih-r01-example", "name": "Scalable epigenome data infrastructure" } ]
```

Validate with the recipe above:
`check-jsonschema --schemafile schemas/grants_jsonld.schema.json jane-doe/sources/grants.jsonld`.

### `cv.md` and `web/` have no schema

The other two deep sources are Markdown, not structured records, so there is no
JSON Schema for them. Both carry a YAML frontmatter provenance block:
`source`/`url` plus `fetched_at`, which is stripped before indexing. Their
correctness is checked at index time (a source present on disk must produce
chunks) rather than by schema validation.

---

## `summary_file` (frontmatter of `sources/summaries/*.summary.md`)

Source model: `SummaryFile` (strict, `additionalProperties: false`). This
schema validates the frontmatter mapping only, never the Markdown body.

| Field | Type | Required? | Default | Description |
|---|---|---|---|---|
| `paper_id` | string | **yes** | none | Citation key of the summarized paper. |
| `source_kind` | string enum | **yes** | none | `fulltext` \| `abstract`. |
| `source_hash` | string | **yes** | none | SHA-256 of the input text. |
| `written_at` | string \| null | no | `null` | ISO-8601 timestamp. |

### Applies to the frontmatter block only

The on-disk file is Markdown with an optional YAML frontmatter block. The
schema validates the frontmatter mapping; the Markdown body is never validated.
Because the schema forbids extra keys, a consumer must extract the
frontmatter and validate that mapping. A summary file with no frontmatter is
not a schema violation.

Guidance:

- Split on the leading `---` fence.
- If there is no frontmatter, there is nothing to validate against this schema:
  skip it, do not reject.
- If frontmatter is present, validate the parsed mapping with the recipe above.

```python
import yaml
from jsonschema import Draft202012Validator
import json


def frontmatter(text):
    if not text.startswith("---"):
        return None  # no frontmatter: nothing to validate
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    return yaml.safe_load(parts[1])


schema = json.load(open("schemas/summary_file.schema.json"))
fm = frontmatter(open("jane-doe/sources/summaries/smith2020.summary.md").read())
if fm is not None:
    Draft202012Validator(schema).validate(fm)
```

Minimal valid frontmatter:

```yaml
paper_id: smith2020
source_kind: fulltext
source_hash: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
written_at: "2026-07-18T00:00:00Z"
```

---

## SQL profile store

The package ships an optional SQL backing store in
`researcher_profiles.db` + `researcher_profiles.store.sql`. It is a peer
backend. A profile in the `rp_*` tables is a profile: it round-trips back to a
byte-identical directory, and `SqlArtifactStorage` reads and writes it through the same
`ArtifactStorage` interface the filesystem backend implements.
It requires the `sql` extra; see the [SDK overview](../index.md#install) for
the checkout install.

Artifact-to-table mapping:

| JSON-Schema artifact | SQL row / table |
|---|---|
| `profile_jsonld` (`profile.jsonld`) | `ProfileRow.document` (table `rp_profiles`) |
| `papers_jsonld` entries (`PaperRecord`) | `PaperRow.record` (table `rp_papers`) |
| `grants_jsonld` entries (`GrantRecord`) | `GrantRow.record` (table `rp_grants`) |
| `profile_jsonld.expertise[]` strings | `ExpertiseTopicRow` (table `rp_expertise_topics`) |
| `profile_jsonld.hasPart` / `.subjectOf` + every file they name | `ArtifactRow` (table `rp_artifacts`) |
| `build_state.json` | `BuildStateRow` (table `rp_build_state`), not published |

### How the tables relate to the JSON schema

- The document is stored whole and is the record. `rp_profiles.document`
  holds the entire `profile.jsonld` payload; `rp_papers.record` and
  `rp_grants.record` hold whole `PaperRecord` / `GrantRecord` nodes. Nothing is
  shredded, because every model here inherits `extra="allow"` and the format
  promises unknown terms round-trip untouched. Shredding would drop them.
- Every scalar column is a derived projection, rebuilt from the document on
  every write by exactly one writer, never read back into a model. They exist so
  a store can be queried (for example, "which profiles are `level=deep` with no
  R01-equivalent year"), not so it can be reconstructed.
- `rid` is the primary key. `slug` is unique and indexed. That is a store
  constraint (one store cannot hold two profiles under one handle), not an
  identity claim. `slug` is not part of `profile_jsonld`; it is the directory
  name, so ingest passes it explicitly. Every child table references
  `profile_rid`.
- Papers are keyed by position. There is no `UNIQUE(profile_rid, paper_id)`:
  `paper_id` is a generated citekey and collides within a real profile.
- `date_modified` is nullable and never defaulted. Most published profiles
  legitimately carry no vintage, so the column stays empty rather than getting
  an invented date.
- There are no build columns. `rp_papers` has no `status` and no
  `identity_verified`; those live in `rp_build_state`, which is outside the
  published set and can be dropped.
- Only portable JSON is used: `sqlmodel.JSON`, never Postgres `JSONB`, so the
  same tables load on SQLite.

The JSON Schemas remain the authoritative on-disk contract; the tables are
the same content in a second, lossless representation. See
[How to store profiles in a database](../how-to/sql-layer.md) for usage.

---

## Test coverage

`tests/test_schema.py` guards the export machinery and the `level`
contract. It asserts that:

- the core models are covered by the exporter (`profile_jsonld`,
  `papers_jsonld`, `grants_jsonld`), and `profile_jsonld`
  requires `name`, `rid`, and `provenance`;
- the exported schemas speak JSON-LD names (`@id`, `conformsTo`, `sameAs`), not
  Python attribute names;
- published documents keep `additionalProperties: true`;
- the deep-input fields live in the build sidecar, not in `profile_jsonld`;
- `level` is present on `profile_jsonld` with enum `{lite, full, deep}`
  defaulting to `full`, and round-trips through the models;
- export writes files, each a titled JSON-Schema object.

It does not compare the checked-in `schemas/*.json` bytes to a fresh
`export_schemas()` run. The [regeneration invariant](#regeneration-workflow-and-invariant)
is a maintainer convention today, not automated drift enforcement.

---

## See also

- [Validate a profile](../how-to/validate-a-profile.md): the two validation levels.
- [Store profiles in a database](../how-to/sql-layer.md): the SQL profile store.
- [Profile format](../profile-format.md): what the `level` tiers mean.
