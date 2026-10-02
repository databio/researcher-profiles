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
