# Agent editing: what a management host implements

rp-sdk's edit endpoints are gated by `app.state.owner_verifier` (see
[the read hooks](read-seam.md) and root `AGENTS.md`). A host that wants a
separate, narrower-scoped agent credential, distinct from a full owner
session, builds the pieces below on top of that hook. rp-sdk supplies the
hook points (`require_owner`, `check_write_scope`); the host supplies the
credential format, the principal lookup, and the scope rules.

## The `rpa_` prefix

A host distinguishing agent keys from app keys can use a prefix convention
(e.g. `rpa_` vs `rpk_`) so a leaked credential is identifiable from a log
line. The format is up to the host, e.g. `rpa_<handle>_<random>`.

## Authentication flow

1. The host resolves the credential to whatever identity object it uses
   internally (the principal).
2. The host's `owner_verifier` checks that the principal holds an editor grant
   on the profile, which satisfies rp-sdk's `require_owner` dependency.
3. rp-sdk's `check_write_scope` calls the host's `write_scope_verifier` with
   the action and its detail; the verifier raises 403 if any required scope is
   missing, before the write is applied.

## The write-scope verifier

Every edit route in `api/routes_edit.py` calls
`check_write_scope(request, action, detail)` (defined in `api/deps.py`) after
`require_owner` passes and before it touches the profile. `check_write_scope`
looks up `app.state.write_scope_verifier`; when it is `None` (bare rp-sdk) the
check is a no-op and any owner may make any write.

A host that wants field-level scopes writes a callable with this shape and
installs it on the app:

```python
def write_scope_verifier(request: Request, action: str, detail: dict) -> None:
    # Raise HTTPException(403) when a required scope is missing.
    ...


app.state.write_scope_verifier = write_scope_verifier
```

The actions rp-sdk passes:

| action | detail | Required scope |
|---|---|---|
| `"metadata"` | `{"fields": ["name", "summary"]}` | one scope per field; the host keeps a map keyed by `researcher_profiles.edit.EDITABLE_METADATA_FIELDS` |
| `"soul"` | `{}` | `profile:narrative` |
| `"visibility"` | `{"slug": "...", "profile_visibility": "...", "artifacts": [...]}` | `profile:visibility`, narrow-only |

The 403 it raises looks like this:

```json
{
  "error": "insufficient_scope",
  "required": ["profile:identity"],
  "granted": ["profile:metadata", "profile:history", "profile:narrative"],
  "missing": ["profile:identity"],
  "hint": "The fields ['name'] require scope(s) ['profile:identity']."
}
```

## Two discovery endpoints

A host serves two discovery endpoints; rp-sdk does not.

`GET /api/manage/agent/whoami`: any valid agent key, no scope required.
Returns the agent's identity, owner, profiles, scopes, and never-delegable acts.
See `docs/rp-spec/dynamic-api.md` for the spec-level definition of this
endpoint.

`GET /api/manage/agent/scopes`: unauthenticated. Returns the scope
definitions with descriptions, endpoints, fields, dangerous flag, and defaults.

## Scope families

App scopes (`read`, `match`, `persona`, `push`) and agent scopes
(`profile:metadata`, `profile:history`, `profile:identity`,
`profile:narrative`, `profile:visibility`) cannot mix. A host's key-minting
code should raise on any attempt to combine them.

## See also

- `api/deps.py`: `require_owner`, `require_scope`, `check_write_scope` (the rp-sdk hooks a host builds on)
- `rp-sdk/skills/profile-agent/SKILL.md`: the agent-facing instructions
