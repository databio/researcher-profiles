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

---

## The key model

A server that mints credentials SHOULD use prefixed, opaque bearer tokens so a
key names its own family on sight. Two families are defined.

| Prefix | Family | Held by | Purpose |
|--------|--------|---------|---------|
| `rpk_` | app | An application, a script, or a person's own command line | Read, push, and other whole-application powers |
| `rpa_` | agent | An assistant acting for one person, on one profile | Narrow, named write scopes |

A single key MUST NOT mix families. An `rpa_` key MUST NOT carry app scopes, and
an `rpk_` key MUST NOT carry agent scopes. A server MUST reject a mint request
that mixes them, and MUST refuse a key presented at an endpoint belonging to the
other family with `400` rather than `401`, naming the endpoint that would have
worked.

A key's plaintext MUST be shown exactly once, at mint time. A server MUST store
only a hash of it, never the plaintext. A server SHOULD store a non-secret
display prefix so a person can recognize a key in a list without revealing it.

A server MUST reject a key that is unknown, revoked, or past its expiry, and
MUST make those three cases indistinguishable in the response, so an attacker
learns nothing from probing.

### App scopes

These attach to `rpk_` keys. A server MAY define others.

| Scope | Grants |
|-------|--------|
| `read` | Read profiles at the key's viewer tier |
| `match` | Cross-profile matching |
| `persona` | The persona endpoints |
| `push` | Upload any profile |
| `push_own` | Upload only profiles the key's owning person holds an `owner` or `editor` role on |
| `resolve` | Resolve a person descriptor to a rid, minting an internal stub on a miss |

`push_own` is what the [command-line login flow](dynamic-api.md#142-command-line-login)
mints, and it is why the flow is safe to expose to any person with an account: a
key that escapes can still only touch profiles that person already controls.

### Agent scopes

These attach to `rpa_` keys. They are the vocabulary published by
[`GET /api/manage/agent/scopes`](dynamic-api.md#get-apimanageagentscopes). Every
one except `profile:read` is a write scope. There is no wildcard, and neither
family implies the other.

| Scope | Covers | Fields | Dangerous | Default |
|-------|--------|--------|-----------|---------|
| `profile:read` | Read this profile in full, including internal and restricted content. Grants no ability to change anything. | | no | on |
| `profile:metadata` | `PATCH /profiles/{slug}/metadata` | `field`, `subfields`, `summary`, `expertise`, `interests`, `not_interests` | no | on |
| `profile:history` | `PATCH /profiles/{slug}/metadata` | `training`, `career` | no | on |
| `profile:identity` | `PATCH /profiles/{slug}/metadata` | `name`, `affiliation`, `job_title`, `same_as` | no | off |
| `profile:narrative` | `PUT /profiles/{slug}/soul` | | no | on |
| `profile:visibility` | `PATCH /profiles/{slug}/visibility` | | **yes** | off |

`profile:visibility` is marked dangerous because an agent holding it on a
published profile can put content behind a wall. It MUST allow narrowing only. A
request that would raise a tier (`restricted` to `internal`, or `internal` to
`public`) MUST be refused with `403` naming the artifact, its current tier, and
the requested tier.

### Acts no agent key may hold

A server MUST NOT define a scope that delegates any of the following. These are
decisions belonging to the person the profile describes.

| Act | Why |
|-----|-----|
| Publish or unpublish | Publishing is a decision by the person the profile describes. |
| Delete the profile | Only the owner may delete a profile. |
| Grant or revoke access | Only the owner may grant access, including minting another agent. |
| Mint or revoke an agent key | A leaked key must not be able to mint a successor. |
| Widen visibility | Making something more visible is an exposure decision. |

A server SHOULD publish this list at
[`GET /api/manage/agent/whoami`](dynamic-api.md#get-apimanageagentwhoami) as
`never_delegable`, so an agent can tell its user what it cannot be given.

### Insufficient scope

A request whose key is valid but lacks the required scope MUST return `403` with
this body, not `401` and not a bare `{"detail": ...}`:

| Field | Type | Description |
|-------|------|-------------|
| `error` | string | The literal `"insufficient_scope"` |
| `required` | list[string] | Scopes this request needs |
| `granted` | list[string] | Scopes the key holds |
| `missing` | list[string] | `required` minus `granted` |
| `hint` | string | One sentence naming the blocked fields and the fix |

```json
{
  "error": "insufficient_scope",
  "required": ["profile:identity"],
  "granted": ["profile:metadata", "profile:history", "profile:narrative"],
  "missing": ["profile:identity"],
  "hint": "The fields ['name'] require scope(s) ['profile:identity']. Ask your owner to add them."
}
```

A client MUST NOT retry on this response. It should report the missing scope and
stop.
