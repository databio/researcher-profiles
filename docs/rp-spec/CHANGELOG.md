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
