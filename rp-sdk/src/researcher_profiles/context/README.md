# The researcher-profile `@context`

`v1.jsonld` is the JSON-LD context every published researcher profile references
from exactly one line:

```json
"@context": "https://profiles.databio.org/context/v1.jsonld"
```

## Why the context lives inside the package

The context is package data, not documentation. `researcher_profiles` reads it at
runtime through `importlib.resources` (see `context_document_text()` in
`jsonld.py`), so it has to sit in the importable tree and ship in every install
layout: wheel, sdist, and editable. A plain `pip install` can then self-host the
context when it publishes a profile site, without fetching anything. The copy
here is the single canonical copy; the hosted IRI serves these same bytes.

## The canonical IRI

The context IRI is **`https://profiles.databio.org/context/v1.jsonld`**, and the
vocabulary namespace is that IRI plus `#` (so `rp:rid` expands to
`…/context/v1.jsonld#rid`). `jsonld.py` records it as `CONTEXT_URL`, and
`PROFILE_FORMAT_IRI` carries the same string as the `conformsTo` gate.

The history behind this IRI, the hosting arrangement, and the option of a later
`w3id.org` redirect are in the jot note
`researcher_profiles/context_iri_hosting.md`.

## The runtime never fetches it

Loading and validating a profile is a **pure local operation** against the
Pydantic models in `researcher_profiles.schema`. The IRI is an identifier and a
documentation pointer, not a runtime dependency. `tests/test_guardrails.py`
asserts that no HTTP client is importable from the profile load path, so the IRI
resolving is never a blocker. A profile written today is valid whether or not the
context URL is currently serving.

## Why `@vocab` ships

The context declares `"@vocab": "https://profiles.databio.org/context/v1.jsonld#"`, so a
key that has no explicit term definition still expands to a valid `rp:` IRI
instead of being **dropped**, which is what a JSON-LD processor does with an
unmapped term.

This is third-party tolerance, not a catch-all for ongoing build output.
`@vocab` exists to guarantee that a publisher who adds their own keys is still
readable, not to license new unmodeled keys from a build tool. A new key a
build tool emits should get an explicit term here (see the change policy below).

## Term mapping

JSON-LD keywords (`@context`, `@id`, `@type`) are used **literally**. `id` and
`type` are not aliased to them, so a plain `type:` key on an
artifact or a paper record means what it says and cannot be confused with the
node type.

### Person / profile document (`profile.jsonld`)

| on-disk key | expands to | notes |
|---|---|---|
| `conformsTo` | `dcterms:conformsTo` (`@id`) | the format gate |
| `name` | `schema:name` | |
| `rid` | `rp:rid` | researcher id: an ORCID or a `local:` id |
| `provenance` | `rp:provenance` | `orcid_verified` \| `self_published` \| `third_party` \| `synthetic` \| `historical` |
| `provenanceNote` | `rp:provenanceNote` | one sentence on how the document was produced (interview-built profiles) |
| `verifiedAt` | `rp:verifiedAt` | required when `provenance` is `orcid_verified` |
| `license` | `schema:license` (`@id`) | reuse terms |
| `url` | `schema:url` (`@id`) | the published profile URL |
| `dateModified` | `schema:dateModified` | |
| `level` | `rp:level` | `lite` \| `full` \| `deep` |
| `affiliation` | `schema:affiliation` | string shorthand; an `Organization` node only when a ROR IRI exists |
| `jobTitle` | `schema:jobTitle` | |
| `email` | `schema:email` | |
| `field` | `rp:field` | |
| `subfields` | `rp:subfield` (`@set`) | |
| `summary` | `schema:description` | |
| `sameAs` | `schema:sameAs` (`@id`, `@set`) | homepage, lab site, Google Scholar, and other external links |
| `identifier` | `schema:identifier` (`@set`) | `PropertyValue` nodes (for example `openalex`) |
| `expertise` | `schema:knowsAbout` (`@set`) | the expertise topic labels |
| `interests` | `rp:interest` (`@set`) | |
| `not_interests` | `rp:notInterest` (`@set`) | |
| `methodological_commitments` | `rp:methodologicalCommitment` (`@set`) | |
| `recurring_positions` | `rp:recurringPosition` (`@set`) | |
| `intellectual_lineage` | `rp:intellectualLineage` (`@set`) | |
| `critiques` | `rp:critique` (`@set`) | |
| `collaborators` | `schema:knows` (`@set`) | string or node |
| `training` | `rp:training` (`@set`) | scoped: `kind`, `degree`, `institution`, `year_start`, `year_end`, `advisor` |
| `career` | `rp:career` (`@set`) | scoped: `role` → `rp:careerRole`, `institution`, `start_year`, `end_year` |
| `researchOutputs` | `rp:researchOutput` (`@set`) | scoped: `type` → `rp:researchOutputType`, `name`, `url` |
| `anchor` | `rp:anchor` | scoped: `disambiguation_evidence`, `confidence`, `sources` (`@id`, `@set`), `created_at` |
| `paper_stats` | `rp:paperStats` | scoped: `first`, `last`, `middle`, `unknown`, `corresponding`, `total`, `year_min`, `year_max` |
| `hasPart` | `schema:hasPart` (`@set`) | the manifest |
| `subjectOf` | `schema:subjectOf` (`@set`) | persona documents |

`award` (`schema:award`), `knowsAbout`, `knows`, and `description` are defined as
aliases so a third-party document written directly in schema.org names is read
correctly.

`training` and `career` stay `rp:` node lists rather than `schema:alumniOf` /
`schema:OrganizationRole`: the context uses integer start and end years
(`year_start`/`year_end` on `training`, `start_year`/`end_year` on `career`),
and schema.org's
date-typed role properties have no clean slot for a split start/end pair on
these node shapes. The integers are preserved as given; no dates are invented.

### Manifest entries (`hasPart` / `subjectOf`)

| key | expands to |
|---|---|
| `name` | `schema:name` |
| `encodingFormat` | `schema:encodingFormat` |
| `contentUrl` | `schema:contentUrl` (`@id`): always a **relative** path |
| `role` | `rp:role` |
| `paperId` | `rp:paperId` |

### Works (`sources/papers.jsonld`, and inlined in `profile.jsonld`)

Work nodes carry `"@type": "ScholarlyArticle"`, which activates a type-scoped
context in which `summary` means `rp:summary` (the profile-level `summary` means
`schema:description`).

| on-disk key | expands to |
|---|---|
| `name` | `schema:name` (the title) |
| `datePublished` | `schema:datePublished`, `xsd:gYear` |
| `isPartOf` | `schema:isPartOf` (a `Periodical` node from `journal` / `venue`) |
| `author` | `schema:author` (`@set` of `schema:Person`) |
| `abstract` | `schema:abstract` |
| `citation` | `schema:citation` |
| `is_oa` | `schema:isAccessibleForFree` |
| `url` | `schema:url` (`@id`) |
| `paper_id` | `rp:paperId` |
| `doi`, `pmid`, `pmcid`, `openalex_id` | `rp:doi`, `rp:pmid`, `rp:pmcid`, `rp:openalexId` |
| `venue` | `rp:venue` |
| `type` | `rp:resourceType` |
| `first_author`, `last_author` | `rp:firstAuthor`, `rp:lastAuthor` |
| `author_position`, `author_index`, `total_authors`, `is_corresponding` | `rp:authorPosition`, `rp:authorIndex`, `rp:totalAuthors`, `rp:isCorresponding` |
| `cited_by_count` | `rp:citedByCount` |
| `open_access`, `oa_status`, `oa_url` | `rp:openAccess`, `rp:oaStatus`, `rp:oaUrl` |
| `pdf_url`, `full_text_link`, `access`, `source` | `rp:pdfUrl`, `rp:fullTextLink`, `rp:access`, `rp:source` |
| `summary` | `rp:summary` (type-scoped) |

A work's `@id` resolves in this order: the DOI IRI (`https://doi.org/<doi>`),
then the OpenAlex IRI (`https://openalex.org/<W...>`), then the relative
fragment `#paper/<paper_id>`.

### Grants (`sources/grants.jsonld`)

Grant nodes carry `"@type": "MonetaryGrant"`, activating a type-scoped context
with these eight keys:

| on-disk key | expands to |
|---|---|
| `id` | `rp:grantId` |
| `funder` | `schema:funder` (an `Organization` node) |
| `activity_code` | `rp:activityCode` |
| `role` | `rp:grantRole` (`pi` \| `co_pi` \| `co_i` \| `other`) |
| `status` | `rp:grantStatus` |
| `start`, `end` | `rp:startDate`, `rp:endDate` |
| `abstract` | `rp:abstract` |

The remaining grant keys are not type-scoped; they resolve from the top-level
context, the same as everywhere else in the document:

| on-disk key | expands to |
|---|---|
| `name` | `schema:name` |
| `identifier` | `schema:identifier` (`@set` of `PropertyValue` nodes: `propertyID`/`value`, e.g. the award number), not a bare string |
| `url` | `schema:url` |
| `source` | `rp:source` |

**schema.org has no crisp "this person received this grant" relation.**
`schema:funding` runs the other way (the *work* that was funded), and
`schema:funder` names the funding organization, not the recipient. The
person→grant link is therefore `rp:heldGrant`, an `rp:` decision, not
an oversight.

## Change policy

The format is not versioned yet: `v1` is the only context, and it is still
being shaped. Edits to `v1.jsonld` are allowed, but they must be deliberate.

`context/v1.lock.json` records the sha256 of `context/v1.jsonld`, and
`tests/test_guardrails.py::TestContextParity::test_context_bytes_match_the_lock`
recomputes it. The lock's purpose is to make an accidental edit a **red test**
rather than a silent change to what every profile means. When you change the
context on purpose, update the hash in the lock file in the same change.

Prefer additive edits that change no existing term: new prefixes, and new term
definitions for keys that had none. Retyping a term, changing a `@container`,
or redirecting a key to a different IRI changes the meaning of documents
already written, so treat it as a format change.

Wizard and clinical extensions use explicit `rp:` names (`rp:sectionVisibility`,
`rp:therapeuticAreas`, `rp:siteCapabilities`, `rp:regulatoryExperience`, and
`rp:researcherRole`). They need no term definition; JSON-LD processors expand
them through the already-defined `rp` namespace.

## Research interests

The context carries what `rp:researchInterests` needs:

- prefixes `skos:` (`http://www.w3.org/2004/02/skos/core#`), `wi:` (the
  Weighted Interest Ontology, `http://purl.org/ontology/wi/core#`), `foaf:`
  and `prov:`;
- a term for `rp:researchInterests` (`@set`) whose scoped context maps each
  entry: `concept` → `wi:topic`, `weight` → `wi:weight` (`xsd:decimal`),
  `method` → `rp:method`, `generator` → `rp:generator`, `assertedAt` →
  `prov:generatedAtTime` (`xsd:dateTime`), `evidence` → `rp:evidence`
  (`papers` → `rp:evidencePaper`, `share` → `rp:paperShare`);
- inside `concept`: `system` → `skos:inScheme` (`@id`), `code` →
  `skos:notation`, `display` → `skos:prefLabel`, `version` →
  `rp:sourceVocabularyVersion`, `label` → `rp:labelSnapshot`, `unmapped` →
  `rp:unmapped`. The concept's own `@id` is the term IRI
  (`https://openalex.org/T10222`, `http://id.nlm.nih.gov/mesh/D057890`);
- `ResearchInterest` → `rp:ResearchInterest`, the `@type` every entry carries
  (a subclass of `wi:WeightedInterest`);
- `topics` → `rp:openalexTopic` (`@set`) on works, the OpenAlex topic ids a
  paper is tagged with.

The term is keyed by its compact IRI, `rp:researchInterests`, because that is
the on-disk key; JSON-LD 1.1 applies a term definition keyed that way.
