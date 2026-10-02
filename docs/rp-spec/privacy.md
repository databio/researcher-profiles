# Privacy

## What this is for

A Researcher Profile (RP) can be lightweight (summary details only) or
detailed (the full text of papers and grants you have written, even drafts of
work in progress). The specification is designed to make a profile
shareable, but you may not want to share all of it, all of the time. Instead
of a binary choice between shared and not shared, the Privacy scheme lets an
RP specify sharing granularity. You might share paper summaries publicly but
withhold the full text of papers or grants. You might share funded grants
with certain verified partners while keeping work in progress visible only to
your own private AI agents.

This document says how to encode that intent in the profile itself, so
software can tell what is shareable without any outside configuration.

---

## How it works

Every artifact in a profile has a **visibility** attribute that says who can access it:

| Visibility | Meaning |
|------------|---------|
| `public` | Anyone may access |
| `limited` | Only readers the owner or the host has granted access (named apps, keys, collaborators, or a host-defined signed-in group) |
| `private` | Only the owner and agents acting for the owner. Never served to anyone else |

Who is in the `limited` audience is a host decision, not part of this spec.

The tiers are ordered `public < limited < private`. An artifact's effective
tier is the most restrictive of the profile default, its own `visibility`, and
everything it is derived from (see [Derived content](#derived-content)).

By default, artifacts are `public`. To restrict an artifact, set `visibility`
on its manifest entry:

```json
{
  "role": "cv",
  "contentUrl": "sources/cv.md",
  "encodingFormat": "text/markdown",
  "visibility": "private"
}
```

To restrict an entire profile, set `visibility` on the profile document itself.
All artifacts inherit it unless they override.

### Inline sections

Some content is not a file: `summary`, the interests, the methodological
commitments and the optional clinical fields live inside `profile.jsonld`
itself. A file exclusion list cannot redact a field out of a document, so
those fields carry their own tier in `rp:sectionVisibility`:

```json
{
  "rp:sectionVisibility": [
    { "section": "methods", "visibility": "private" }
  ]
}
```

The sections are a closed set: `summary`, `expertise`, `focus` (the subfields
and the interests, typed and plain), `methods`, `soul`, `clinical`,
`site_capabilities`, `regulatory_experience`, `contact`, `background`. A
section's effective tier is the more restrictive of its own and the profile's,
so a section can never be more public than the profile carrying it.

Everything that serves a profile document — the JSON-LD read, the metadata and
list views, the rendered page, the archive, the static site and the knowledge
base export — serves it projected to the reader's tier. An identity key the
format requires (`name`, `rid`) has no section of its own: it is governed by
the whole profile's visibility, so a hidden name and a public profile is not a
combination an interface should offer.

---

## What's private by default

Two separate mechanisms hold artifacts back from `public`, and they are not
interchangeable.

### 1. Role defaults (a default, not a floor)

Some roles default to a tier more restrictive than `public` when the artifact
does not declare its own `visibility`: `paper_fulltext`, `cv`, `web`, `grant`
(the singular grant-derived embedding chunk, not the plural `grants`
bibliographic record), and `embedding_index_sqlite` (the build-local sqlite
index; the servable form is the flat `embeddings/` export) default to
`private`; `trials` defaults to `limited`. These are **defaults, not
floors**: the owner may re-tier any of them freely (up or down), and an
explicit `"visibility"` on the artifact wins. Nothing is a legal constraint,
and no role is reported as `locked` — that concept has been removed.

`paper_fulltext` is an ordinary role here: it defaults to `private` so
extracted full text never becomes public by accident, but the owner may raise
it to `limited` or `public` through the visibility editor or
`PATCH .../visibility`, exactly like any other artifact.

### 2. Path prefixes, not tiers

The SDK's `.cache/` (serve-time derived caches) and `.keys/` (signing key
material) are excluded unconditionally, regardless of any declared visibility.
These never enter the tier computation at all. They are the only artifacts
withheld from every viewer, the owner included.

| Artifact | Mechanism | Owner may re-tier? |
|----------|-----------|------------------------|
| `sources/papers/*.md` (full text, role `paper_fulltext`) | Role default (`private`) | Yes |
| `sources/cv.md` (role `cv`) | Role default (`private`) | Yes |
| `sources/web/*.md` (role `web`) | Role default (`private`) | Yes |
| Grant embedding chunks (role `grant`) | Role default (`private`) | Yes |
| `.cache/`, `.keys/` | Path prefix, excluded unconditionally | No — never enters tier computation |

---

## Derived content

If you create content derived from a private source, the derived content
inherits that restriction. A summary that reproduces large portions of
copyrighted text is still private.

**Exception:** Authored synthesis (like paper summaries or expertise.md) that
describes sources rather than reproducing them does NOT inherit the restriction.
Otherwise every summary would be private because it is derived from full text.

The inheritance is **transitive**: a `derivedFrom` chain is walked to the end,
so a public blurb derived from a public digest derived from a private CV is
private. Two shapes are errors rather than tiers. A `derivedFrom` value that
names no `paperId` and no `role` in the manifest is a restriction that silently
failed to apply, and validation reports it. A cycle makes the rule
self-referential, and an implementation must refuse to answer rather than
settle on whichever tier it reached first.

---

## Static export

A static host serves files to anyone who can reach it, so it serves exactly one
audience. A static host serving audience T MUST receive only:

- profiles whose own `visibility` T may see;
- artifacts whose effective tier T may see (`tier_allows(T, effective)`);
- a `profile.jsonld` **projected** to T: every inline section whose tier T may
  not see is removed from the document before it is written.

A profile folder copied as is does not meet this rule. Its `profile.jsonld`
holds every inline section, and an exclude list of files cannot remove a field
from inside a file. Serving several audiences means several exports, one per
host.

The reference implementation is `rp publish --who <tier>`, which writes a
folder that any sync tool can then upload without filtering.

---

## How implementations use this

The spec defines how to encode shareability. Implementations decide how to
enforce it:

- Local tools read `visibility` to decide what each audience's export holds
- Registries (the kind the [management tier](dynamic-api.md#14-management-api)
  serves) may add a consumer layer that shows different artifacts to different
  users based on their role
- Public hosts only receive `public` artifacts in the first place

The manifest always lists all artifacts (including private ones) so
authorized consumers know what exists. A consumer without access skips
those entries.

---

## Mapping to other vocabularies

No standard defines an ordered three-level visibility scale for parts of a
profile, so rp keeps its own terms. Each tier is a `skos:closeMatch` (not an
exact match) to a term in the recognized access-rights vocabularies, because
the definitions differ slightly. For example, ORCID's `private` is also
visible to the item's source system.

| rp | Meaning | EU access-right (DCAT-AP) | COAR access right | ORCID |
|---|---|---|---|---|
| `public` | Anyone | `PUBLIC` | `c_abf2` (open access) | `public` |
| `limited` | Readers the owner or host has granted access | `RESTRICTED` | `c_16ec` (restricted access) | `limited` |
| `private` | The owner and agents acting for the owner | `NON_PUBLIC` | none | `private` |

- EU Publications Office access-right table (version 20260923-0), base IRI
  `http://publications.europa.eu/resource/authority/access-right/`. DCAT-AP 3.0
  limits `dcterms:accessRights` to `PUBLIC`, `RESTRICTED`, and `NON_PUBLIC`.
- COAR Access Rights vocabulary (v1.1), namespace
  `http://purl.org/coar/access_right/`.
- ORCID visibility levels have no IRIs; the ORCID column is a prose
  cross-reference only.

Note that the EU and COAR "restricted" is rp's *middle* tier (`limited`), not
its owner-only tier. In the JSON-LD context, a `visibility` value expands to
`rp:Public`, `rp:Limited`, or `rp:Private`, and the context document defines
those three concepts with the `skos:closeMatch` links above.
