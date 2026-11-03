# Authentication

The Researcher Profiles specification does not require authentication. A server
MAY serve profiles publicly with no credentials. This is the norm for static
file hosts (S3, R2, GitHub Pages).

A server MAY require authentication on any endpoint. When it does, it MUST use
bearer tokens:

```
Authorization: Bearer <token>
```

An unauthenticated request to a protected endpoint MUST receive `401` with
`{"detail": "invalid or missing bearer token"}`.

A request for a profile or artifact above the caller's access level returns
`404`, indistinguishable from a nonexistent resource.

---

## Viewer tiers

A server MAY implement viewer tiers to control which artifacts a caller can see.
Three tiers are defined:

| Tier | Sees |
|------|------|
| `public` | Public artifacts only |
| `limited` | Public + limited artifacts |
| `private` | All artifacts |

An unauthenticated caller defaults to the `public` tier. A server that does not
implement tiers treats all callers as `public`.

When viewer tiers are active, responses SHOULD include an `X-RP-Viewer-Tier`
header indicating the caller's effective tier.

A tier is not a property of a credential. A server that implements
[per-part access](#per-part-access) decides what a credential reads from its
parts table, not from a tier.

---

## Credentials and scopes

How a server mints, stores and names its credentials is the server's business.
A server that mints them SHOULD show a credential's plaintext exactly once,
store only a hash of it, and reject an unknown, revoked or expired credential
with the same response in all three cases, so probing learns nothing.

A server MAY give a credential **scopes**: named whole-server powers such as
reading profiles, matching across profiles, or uploading. What a credential
may read and write of one profile is not a scope; it is the credential's
[parts table](#per-part-access). A request that needs a scope the credential
lacks is refused with [`insufficient_scope`](#insufficient-scope).

## Per-part access

A server that lets people share parts of their profile with other software
SHOULD decide every read and write by an app or key **per part, per
audience**.

An **audience** is the Public or one credential (an app's, or a key acting for
a person). A **part** is one section of the profile document or one kind of
file (a manifest `role`). Each audience holds one **level** per part: `none`,
`read`, or `write`. `write` includes `read`. A part a table does not list is
`none`. Who sets each table, and how, is up to the server; the profile's owner
SHOULD be the one who decides.

### The parts

Sections, and the document fields each governs:

| Part | Fields |
|------|--------|
| `summary` | `summary` |
| `expertise` | `expertise` (the label list), and the `personality/expertise.md` file |
| `focus` | `field`, `subfields`, `interests`, `not_interests`, `research_interests` |
| `methods` | `methodological_commitments` |
| `soul` | the `personality/SOUL.md` narrative (role `soul`) |
| `clinical` | `therapeutic_areas` |
| `site_capabilities` | `site_capabilities` |
| `regulatory_experience` | `regulatory_experience` |
| `contact` | `email` |
| `background` | `name`, `affiliation`, `job_title`, `training`, `career`, `same_as`, `identifier`, `affiliation_id`, `career_stage` |

Files, by manifest `role`: `works` (also the document fields
`research_outputs` and `paper_stats`), `paper_summary`, `paper_abstract`,
`paper_fulltext`, `grants`, `cv`, `interview`, `web`, `trials`, `citations`,
and `other_files` (every role not named here and not infrastructure).
Infrastructure roles (`profile`, `context`, `agent_entry_point`, `html`,
`embedding_index`, `embedding_index_sqlite`, `embedding_chunks`) belong to no
part.

Every part is writable by an app or key holding `write` on it.

### Reads

An app or key never reads less than the Public: its effective reads on a
profile are its table's `read` and `write` parts, united with what the Public
reads there. A part or artifact the caller may not read returns `404`; a
profile the caller may read nothing of returns `404` too.

An app or key that reads some parts but not all is served at viewer tier
`public` with a copy of the document rewritten for it: the parts it may read
are declared `public`, the rest `private`. Those declarations describe that
caller's view, not the open web's.

### Writes

A write by an app or key MUST hold `write` on every part it touches:

- a metadata patch, on the part of each field it names;
- a SOUL write, on `soul`; a works edit, on `works`;
- a whole-profile upload, on every part the upload changes (see
  [the push rule](dynamic-api.md#put-profilesslug)).

A write missing any of these MUST be refused with
[`insufficient_access`](#insufficient-access) and MUST change nothing.

### Replacing whole profiles

A server MAY let a credential replace a whole profile at once, without the
part-by-part comparison. That power covers what no part covers, including
what the Public reads, so a server SHOULD keep it off unless the owner turns
it on.

### Acts no app or key may hold

A server MUST NOT let any table or switch delegate the following. They are
decisions belonging to the person the profile describes.

| Act | Why |
|-----|-----|
| Publish or unpublish | Publishing is a decision by the person the profile describes. |
| Change what the public sees | Only the owner decides what the public may read. |
| Delete the profile | Only the owner may delete a profile. |
| Grant access | Only the owner may give access, including minting another key. |

"Change what the public sees" has one exception: a caller allowed to
[replace whole profiles](#replacing-whole-profiles) writes the whole document,
including what it tells the Public. No edit endpoint may change it, whatever the
caller holds.

A server SHOULD tell a credential which of these acts it cannot be given. A
request to perform one of these acts, or to change a metadata field no part
covers, MUST return `403` with `detail.error` equal to `"not_delegable"` and a
`hint`.

## Refusal bodies

Both bodies below are carried in the `detail` member of the response:
`{"detail": {...}}`. A client MUST NOT retry on either. It should report what
is missing and stop.

### Insufficient access

A write by an app or key that lacks `write` on a part it touches MUST return
`403` with:

| Field | Type | Description |
|-------|------|-------------|
| `error` | string | The literal `"insufficient_access"` |
| `required` | list[string] | Every part the write touches, sorted |
| `missing` | list[string] | The parts in `required` the caller may not write, sorted |
| `needs_replace` | list[string] | Uploads only. Changes no part covers (for example what the public sees, a field such as `provenance_note`, an infrastructure file), as short phrases. Only [replacing the whole profile](#replacing-whole-profiles) allows them |
| `hint` | string | One or two sentences naming what is missing and who can allow it |

```json
{
  "detail": {
    "error": "insufficient_access",
    "required": ["paper_summary", "summary"],
    "missing": ["summary"],
    "needs_replace": [],
    "hint": "This needs Write on summary. The profile's owner can allow it."
  }
}
```

### Insufficient scope

A request whose key is valid but lacks a required [scope](#credentials-and-scopes) MUST return
`403` with:

| Field | Type | Description |
|-------|------|-------------|
| `error` | string | The literal `"insufficient_scope"` |
| `required` | list[string] | Scopes this request needs |
| `granted` | list[string] | Scopes the key holds |
| `missing` | list[string] | `required` minus `granted` |
| `hint` | string | One sentence naming the fix |

```json
{
  "detail": {
    "error": "insufficient_scope",
    "required": ["match"],
    "granted": ["read"],
    "missing": ["match"],
    "hint": "This credential lacks scope 'match'. Ask the server's operator to add it."
  }
}
```
