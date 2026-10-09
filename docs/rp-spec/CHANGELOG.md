# Spec changelog

## 0.1.0-alpha (unreleased)

First version of the specification. It defines the Researcher Profile document
(`profile.jsonld`, extending schema:Person) and its manifest of associated
files, the embedding index format, and privacy guidance. It defines three HTTP
tiers: the required static API for plain file hosts, the optional dynamic API
for listing, search, matching, and owner edits, and the optional management API
for command-line login (the OAuth 2.0 Device Authorization Grant, RFC 8628, at
`POST /api/auth/device` and `POST /api/auth/token`) and identity echo. Reads
and writes by apps and account keys are decided per part (a `none`, `read`, or
`write` table, plus a "Replace whole profiles" switch), with an
`insufficient_access` refusal body. Authentication, where used, is by bearer token. The version tracked here
is independent of the package versions (`rp-sdk`, `scholarcore`). A profile's
`conformsTo` names the context major version (the fixed IRI ending in
`context/v1.jsonld`), and the conformance suite gates on that IRI. Pre-1.0
versions MAY introduce incompatible changes between releases with no shims and
no deprecation period.

Research interests. `rp:researchInterests` replaces `rp:weightedInterests`.
Each entry links the person to one concept (coded, or text-only with
`unmapped: true`) with an optional signed weight from -1 to 1 (missing means
unknown, 0 means declared neutral), plus `method`, `generator`, `assertedAt`
and optional `evidence`. The accepted/rejected decision is gone. `interests`
and `not_interests` are rebuilt from the entries that count by precedence.
The recommended vocabularies (OpenAlex topics, then MeSH, then text-only) and
their system URIs are in [Vocabularies](vocabularies.md). The context
(`context/v1.jsonld`) gains the SKOS, Weighted Interest Ontology and PROV
mappings for these entries; no existing term changes meaning.

Visibility tiers renamed. The three tiers are now `public`, `limited`, and
`private` (were `public`, `internal`, and `restricted`), ordered
`public < limited < private`. `limited` means readers the owner or host has
granted access; `private` means the owner and agents acting for the owner.
The old names are rejected, with no aliases: validation names the
replacement. [Privacy](privacy.md#mapping-to-other-vocabularies) gains a
mapping to the EU access-right, COAR and ORCID vocabularies, and the context
expands `visibility` values to `rp:Public`, `rp:Limited`, and `rp:Private`,
each a `skos:closeMatch` to those terms. Served manifest entries carry
`accessRights` (`dcterms:accessRights`), the EU IRI of the entry's effective
tier; it is derived at serve time and never stored.

Push modes. `PUT /profiles/{slug}` with an archive takes a `mode` query
parameter that says what happens to live files the archive omits: `replace`
(the default; keeps a withheld class, full text or the search index, that the
archive carried none of), `merge` (keeps every omitted file), or `prune`
(deletes every omitted file). Kept files are spliced back into the manifest.
The push response gains `kept`, `spliced`, `manifest_counts`, and `mode`.
`GET /capabilities` lists the accepted modes and features so a client can
check before it pushes. The old line that full text "is stripped on ingest"
now says what is true: a server that does not accept full text drops incoming
`sources/papers/` members, but keeps the full text it already holds.

Text artifacts. `paper_fulltext` and `paper_summary` files must be readable
text: valid UTF-8, no NUL, no more than 1% U+FFFD and 2% control characters
(tab, newline, and carriage return excepted), and no raw PDF body. The
conformance suite already enforced this (`invalid/garbled-fulltext`); the
spec now states it, as Base conformance rule 7.
