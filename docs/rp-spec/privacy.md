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
| `public` | Anyone can access |
| `internal` | Restricted audience (e.g., your lab, authenticated users) |
| `restricted` | Never shared, stays on your machine |

By default, artifacts are `public`. To restrict an artifact, set `visibility`
on its manifest entry:

```json
{
  "role": "cv",
  "contentUrl": "sources/cv.md",
  "encodingFormat": "text/markdown",
  "visibility": "restricted"
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
    { "section": "methods", "visibility": "restricted" }
  ]
}
```

The sections are a closed set: `summary`, `expertise`, `focus` (the subfields
and the interests, weighted and plain), `methods`, `soul`, `clinical`,
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

## What's restricted by default

Two separate mechanisms hold artifacts back from `public`, and they are not
interchangeable.

### 1. Role defaults (a default, not a floor)

Some roles default to a tier more restrictive than `public` when the artifact
does not declare its own `visibility`: `paper_fulltext`, `cv`, `web`, `grant`
(the singular grant-derived embedding chunk, not the plural `grants`
bibliographic record), and `embedding_index_sqlite` (the build-local sqlite
index; the servable form is the flat `embeddings/` export) default to
`restricted`; `trials` defaults to `internal`. These are **defaults, not
floors**: the owner may re-tier any of them freely (up or down), and an
explicit `"visibility"` on the artifact wins. Nothing is a legal constraint,
and no role is reported as `locked` — that concept has been removed.

`paper_fulltext` is an ordinary role here: it defaults to `restricted` so
extracted full text never becomes public by accident, but the owner may raise
it to `internal` or `public` through the visibility editor or
`PATCH .../visibility`, exactly like any other artifact.

### 2. Path prefixes, not tiers

The SDK's `.cache/` (serve-time derived caches) and `.keys/` (signing key
material) are excluded unconditionally, regardless of any declared visibility.
These never enter the tier computation at all; they are written into
`.publishignore` directly, along with `.publishignore` itself. They are the
only artifacts withheld from every viewer, the owner included.

| Artifact | Mechanism | Owner may re-tier? |
|----------|-----------|------------------------|
| `sources/papers/*.md` (full text, role `paper_fulltext`) | Role default (`restricted`) | Yes |
| `sources/cv.md` (role `cv`) | Role default (`restricted`) | Yes |
| `sources/web/*.md` (role `web`) | Role default (`restricted`) | Yes |
| Grant embedding chunks (role `grant`) | Role default (`restricted`) | Yes |
| `.cache/`, `.keys/` | Path prefix, excluded unconditionally | No — never enters tier computation |

---

## Derived content

If you create content derived from a restricted source, the derived content
inherits that restriction. A summary that reproduces large portions of
copyrighted text is still restricted.

**Exception:** Authored synthesis (like paper summaries or expertise.md) that
describes sources rather than reproducing them does NOT inherit the restriction.
Otherwise every summary would be restricted because it is derived from full text.

The inheritance is **transitive**: a `derivedFrom` chain is walked to the end,
so a public blurb derived from a public digest derived from a restricted CV is
restricted. Two shapes are errors rather than tiers. A `derivedFrom` value that
names no `paperId` and no `role` in the manifest is a restriction that silently
failed to apply, and validation reports it. A cycle makes the rule
self-referential, and an implementation must refuse to answer rather than
settle on whichever tier it reached first.

---

## `.publishignore`

When you build a profile, tooling generates `.publishignore`, a list of all
non-public artifacts. This lets simple sync tools (rsync, aws s3 sync) respect
your privacy settings:

```bash
rsync -a --exclude-from=.publishignore <profile>/ <destination>/
```

---

## How implementations use this

The spec defines how to encode shareability. Implementations decide how to
enforce it:

- Local tools read `visibility` to know what's safe to sync
- Registries (the kind the [management tier](dynamic-api.md#14-management-api)
  serves) may add a consumer layer that shows different artifacts to different
  users based on their role
- Public hosts only receive `public` artifacts in the first place

The manifest always lists all artifacts (including restricted ones) so
authorized consumers know what exists. A consumer without access skips
those entries.
