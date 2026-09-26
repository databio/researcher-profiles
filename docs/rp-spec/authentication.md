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
| `internal` | Public + internal artifacts |
| `restricted` | All artifacts |

An unauthenticated caller defaults to the `public` tier. A server that does not
implement tiers treats all callers as `public`.

When viewer tiers are active, responses SHOULD include an `X-RP-Viewer-Tier`
header indicating the caller's effective tier.

A tier is not a property of an API key. A server that implements
[per-part access](#per-part-access) decides what a key reads from the key's
parts table, not from a tier. The one exception: an app key (`rpk_`) MAY carry
a minted tier that governs its reads of a profile no person has claimed, since
no owner has set a table for it there.

---

## The key model

A server that mints credentials SHOULD use prefixed, opaque bearer tokens so a
key names its own family on sight. Two families are defined.

| Prefix | Family | Held by | Purpose |
|--------|--------|---------|---------|
| `rpk_` | app | An application, a script, or a person's own command line | Read, push, and other whole-application powers |
| `rpa_` | account | An assistant or tool acting for one person's whole account | Read and write, part by part, what that account reaches |

Scopes name whole-application powers only. What a key may read and write of
one profile is not a scope: it is the key's [parts table](#per-part-access).
An `rpa_` key MUST NOT carry any app scope other than `read` and `push_own`
(below), and a server MUST refuse a request that needs another app scope with
[`insufficient_scope`](#insufficient-scope). On data endpoints the prefix is a
label, never an authorization input: scopes and tables decide, and a key of
either family is judged by what it holds. Only on the management surfaces
(such as [`GET /api/manage/agent/whoami`](dynamic-api.md#get-apimanageagentwhoami))
MUST a server refuse a key of the other family with `400` rather than `401`,
naming the endpoint that would have worked.

An account key is bound to a person's account, never to one profile. It
reaches every profile the account owns, co-owns, or edits, and every profile
of a person who granted the account a permission. It never writes where the
account itself may not write.

A key's plaintext MUST be shown exactly once, at mint time. A server MUST store
only a hash of it, never the plaintext. A server SHOULD store a non-secret
display prefix so a person can recognize a key in a list without revealing it.

A server MUST reject a key that is unknown, revoked, or past its expiry, and
MUST make those three cases indistinguishable in the response, so an attacker
learns nothing from probing.

### Scopes

| Scope | Family | Grants |
|-------|--------|--------|
| `read` | either | Read profiles. What is read is decided by the key's table (or, for an app key on an unclaimed profile, its minted tier) |
| `match` | app | Cross-profile matching |
| `persona` | app | The persona endpoints |
| `push` | app | Upload any profile |
| `push_own` | either | Upload whole profiles the key's owning person holds an `owner` or `editor` role on. On an account key this is the ["Replace whole profiles"](#replace-whole-profiles) switch |
| `resolve` | app | Resolve a person descriptor to a rid, minting an internal stub on a miss |

A server MAY define other app scopes.

`push_own` is what the [command-line login flow](dynamic-api.md#142-command-line-login)
mints, and it is why the flow is safe to expose to any person with an account: a
key that escapes can still only touch profiles that person already controls.

## Per-part access

A server that lets people share parts of their profile with other software
SHOULD decide every read and write by an app or account key **per part, per
audience**.

An **audience** is the Public, one app, or one account key. A **part** is one
section of the profile document, one kind of file (a manifest `role`), or, for
apps and keys only, the account's lenses. Each audience holds one **level** per
part: `none`, `read`, or `write`. `write` includes `read`. A part a table does
not list is `none`.

### The parts

Sections, and the document fields each governs:

| Part | Fields |
|------|--------|
| `summary` | `summary` |
| `expertise` | `expertise` (the label list), and the `personality/expertise.md` file |
| `focus` | `field`, `subfields`, `interests`, `not_interests`, `weighted_interests` |
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

Apps and keys only: `lenses`.

Every part is writable by an app or key holding `write` on it.

### Who sets each table

| Audience | Table set by | Covers |
|----------|--------------|--------|
| The Public | The profile's owner, per profile | Reads only. The Public reading any part is what "published" means |
| An app (`rpk_`) | Each person, for that app | Every profile the person owns and every lens they wrote |
| An account key (`rpa_`) | The person who minted it | Everything the key's account reaches |

A key minted without a table reads every part and writes nothing. Only the
account holder may change a key's table; no key may change a table, including
its own.

### Reads

An app or key never reads less than the Public: its effective reads on a
profile are its table's `read` and `write` parts, united with what the Public
reads there. A part or artifact the caller may not read returns `404`; a
profile the caller may read nothing of returns `404` too.

An app or key that reads some parts but not all is served at viewer tier
`public` with a copy of the document rewritten for it: the parts it may read
are declared `public`, the rest `restricted`. Those declarations describe that
caller's view, not the open web's.

### Writes

A write by an app or key MUST hold `write` on every part it touches:

- a metadata patch, on the part of each field it names;
- a SOUL write, on `soul`; a works edit, on `works`;
- a whole-profile upload, on every part the upload changes (see
  [the push rule](dynamic-api.md#put-profilesslug)).

A write missing any of these MUST be refused with
[`insufficient_access`](#insufficient-access) and MUST change nothing.

### Replace whole profiles

A separate switch, off by default. An app holds it in its table (the
pseudo-part `replace_profiles`); an account key holds it as the `push_own`
scope. With it on, an upload replaces the whole profile at once: it is not
compared part by part, and it may change what no part covers, including what
the Public reads.

### Acts no app or key may hold

A server MUST NOT let any table or switch delegate the following. They are
decisions belonging to the person the profile describes.

| Act | Why |
|-----|-----|
| Publish or unpublish | Publishing is a decision by the person the profile describes. |
| Change what the public sees | Only the owner decides what the public may read. |
| Delete the profile | Only the owner may delete a profile. |
| Grant access | Only the account holder may give access, including minting another key. |

"Change what the public sees" has one exception: a caller holding
[Replace whole profiles](#replace-whole-profiles) writes the whole document,
including what it tells the Public. No edit endpoint may change it, whatever the
caller holds.

A server SHOULD publish this list at
[`GET /api/manage/agent/whoami`](dynamic-api.md#get-apimanageagentwhoami) as
`never_delegable`, so a key can tell its user what it cannot be given. A request
to perform one of these acts, or to change a metadata field no part covers,
MUST return `403` with `detail.error` equal to `"not_delegable"` and a `hint`.

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
| `needs_replace` | list[string] | Uploads only. Changes no part covers (for example what the public sees, a field such as `provenance_note`, an infrastructure file), as short phrases. Only [Replace whole profiles](#replace-whole-profiles) allows them |
| `hint` | string | One or two sentences naming what is missing and who can allow it |

```json
{
  "detail": {
    "error": "insufficient_access",
    "required": ["paper_summary", "summary"],
    "missing": ["summary"],
    "needs_replace": [],
    "hint": "This needs Write on summary. The account holder can allow it on the Privacy page."
  }
}
```

### Insufficient scope

A request whose key is valid but lacks a required [scope](#scopes) MUST return
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
    "hint": "Consumer 'lab-app' lacks scope 'match'. Ask your operator to add it."
  }
}
```
