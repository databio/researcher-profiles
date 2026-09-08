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
build tool emits should get an explicit term here (see the freeze policy below)
or, at `v2`, a proper mapping.

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

## Freeze policy

**`v1` is immutable once published.** A change of meaning means minting `v2` at a
new IRI (`https://profiles.databio.org/context/v2.jsonld`).

The **only** permitted post-publication edit to `v1` is adding a term definition
whose expanded IRI is byte-identical to what `@vocab` already produced for that
key, i.e. `"foo": "rp:foo"`. Such an addition cannot change the meaning of any
document already in the wild, because the key already expanded to exactly that
IRI.

Every other change mints `v2`:

- retyping a term (`"@type": "@id"`, `xsd:gYear`, ...)
- adding or removing a `@container`
- redirecting a key from `rp:` to a `schema:` IRI (or the reverse)
- adding or changing a type-scoped or property-scoped context
- changing a prefix

`context/v1.lock.json` records the sha256 of `context/v1.jsonld`, and
`tests/test_guardrails.py::TestContextParity::test_context_bytes_match_the_lock`
recomputes it. The lock's purpose is to make an
accidental edit a **red test** rather than a silent break of every published
profile.

To mint `v2`: add `context/v2.jsonld` and `context/v2.lock.json`, bump
`CONTEXT_URL` / `PROFILE_FORMAT_IRI` in `src/researcher_profiles/jsonld.py`, and
leave `v1` untouched forever.
