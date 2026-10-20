# Spec changelog

## Unreleased

Command-line login endpoints are found, not fixed. A server that offers
command-line login publishes RFC 8414 metadata at
`/.well-known/oauth-authorization-server` naming its
`device_authorization_endpoint` and `token_endpoint`; clients read them from
there and check `issuer`. The fixed `/api/auth/device` and `/api/auth/token`
paths are gone. The approval page should offer a deny control.

Management endpoints moved under `/api/v1/manage/` (were `/api/manage/`).

Command-line login is a registered OAuth client. Clients sign in as the
public client `rp-cli` with scope `profiles:push`, by the authorization-code
flow with PKCE and a loopback redirect (RFC 8252; the server accepts any port
on `http://127.0.0.1/callback` and `http://[::1]/callback`) or by the device
flow. The server answers with a short-lived access token and a rotating
refresh token (RFC 6749 section 5.1 fields only; no `orcid`, `name` or `url`);
identity comes from `whoami`. Other client_ids get `invalid_client`. A server
may offer RFC 7009 revocation, listed as `revocation_endpoint`.

## 0.2.0 - 2026-10-12

Command-line login endpoints moved. Device login now follows the OAuth 2.0
Device Authorization Grant (RFC 8628) exactly, at `POST /api/auth/device` and
`POST /api/auth/token`, replacing `/api/manage/cli-auth` and its poll route.
A stock OAuth device-flow client works unchanged.

Full text tier is a default. `paper_fulltext` is no longer held at a fixed
floor; its tier is a default the owner can change. Artifact tier reports drop
`locked` and `lock_reason`.

Per-part access. Reads and writes by apps and account keys are decided per part
(a `none`, `read`, or `write` table, plus a "Replace whole profiles" switch),
with an `insufficient_access` refusal body.

New read routes. The dynamic API gains record views, paged lists (`cursor`,
`next_cursor`, `has_more`), full text and narrative in bounded pages
(`/text`), and passage search (`/passages`). Profile files are served beside
`profile.jsonld`.

Sections and new artifacts. Visibility can be set per section of the profile
document, including a `clinical` section. Interview self-reports and clinical
trials are new artifact roles. Profiles can carry rid aliases, so an old slug
still resolves after a merge, and an ORCID login proof.

Research interests. New `rp:researchInterests` entries record what a person
works on. Each entry links the person to one concept (coded, or text-only with
`unmapped: true`) with an optional signed weight from -1 to 1 (missing means
unknown, 0 means declared neutral), plus `method`, `generator`, `assertedAt`
and optional `evidence`. `interests`
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

Chunk tiers. A chunk's tier is now the most restrictive of the profile
default, the role default, and its source artifact's effective tier, so a tier
set on one file or on the `soul` section reaches its chunks. The rule applies
to every serve-time use of a chunk, including the `/match` score and its
evidence ([Embeddings §6](embeddings.md#6-privacy)).

## 0.1.0 - 2026-09-25

Initial specification release. The version tracked here is independent of the
package versions (`rp-sdk`, `scholarcore`), which follow the SDK's own release
cadence. A profile's `conformsTo` names the context major version (the fixed
IRI ending in `context/v1.jsonld`), and the conformance suite gates on that
IRI; this file tracks prose revisions within that major version. Pre-1.0
versions MAY introduce incompatible changes between minor increments with no
shims and no deprecation period.

Includes `POST /identity/resolve` and the `resolve` app scope.
