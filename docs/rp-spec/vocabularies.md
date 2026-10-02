# Vocabularies for research interests

A [research interest](index.md#research-interests) points at a concept. A coded
concept names a term in a controlled vocabulary, so two profiles that mean the
same thing say it with the same code and a matcher can compare them. This page
says which vocabularies to use and what the `system` and `version` fields hold
for each.

## Which vocabulary

Use them in this order:

1. **OpenAlex topics.** About 4,500 research topics with ids like `T10222`, in
   a four-level tree (topic, subfield, field, domain). Every OpenAlex work is
   already tagged with them, so an interest in a topic can be counted against a
   person's papers and matched against new papers directly.
2. **MeSH.** The U.S. National Library of Medicine's Medical Subject Headings,
   about 30,000 biomedical descriptors with ids like `D057890`, released yearly.
   Use it when a biomedical interest is finer than any OpenAlex topic.
3. **Text-only.** A `label` with `unmapped: true`, when no term fits. Never
   force an interest onto a wrong term; an honest text-only entry is better
   than a confident wrong code.

## System URIs

| Vocabulary | `system` | Term `@id` |
|---|---|---|
| OpenAlex topics | `https://openalex.org/topics` | `https://openalex.org/T10222` |
| MeSH | `http://id.nlm.nih.gov/mesh` | `http://id.nlm.nih.gov/mesh/D057890` |

No standard identifier exists for the OpenAlex topic list as a scheme, so
`https://openalex.org/topics` is this format's choice. MeSH's own term IRIs use
`http`, so its system URI does too.

## What `version` means

`version` is the release of the pinned copy the concept was looked up in, not a
term's own update date and not the date anyone fetched it.

- **MeSH**: the MeSH year, e.g. `2026`.
- **OpenAlex topics**: OpenAlex publishes no release number, so the snapshot
  month of the pinned copy, e.g. `2026-09`.

The reference implementation keeps these pinned copies in rp-sdk
(`researcher_profiles/vocab/`) and refreshes them with `rp vocab refresh`
every year or two. Every tool that resolves a code reads the same copy, so a
stored code means the same thing everywhere even after OpenAlex changes its
list. A code the pinned copy does not know is kept and flagged, not dropped.

## Mappings between vocabularies

Mappings (for example an OpenAlex topic and a MeSH descriptor that mean the
same thing) belong beside the vocabulary files as SKOS match statements
(`skos:exactMatch`, `skos:closeMatch`), never inside interest entries. An
interest entry states one person's weight on one concept.

A consumer MUST NOT carry a weight across a mapping, or up from a topic to its
parent subfield, as if the person had declared it. A borrowed or rolled-up
weight counts as inferred, and it never triggers a -1 hard exclude.

## Precedent

The weighted link from a person to a topic follows the Weighted Interest
Ontology (Dell'Aglio et al., 2010, `http://purl.org/ontology/wi/core#`), the
only published model found that puts a weight on that link. FOAF
(`foaf:topic_interest`), schema.org (`knowsAbout`), VIVO, ORCID and Wikidata
link a person to a topic without a weight. This spec adds what the Weighted
Interest Ontology leaves open: the -1 to 1 range and the meaning of 0.
