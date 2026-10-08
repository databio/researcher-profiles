# Agent editing: what a management host implements

rp-sdk's edits are service functions in
[`api.service`](../../../rp-sdk/src/researcher_profiles/api/service.py):
`edit_metadata`, `edit_work`, `add_work`, `remove_work` and `set_visibility`.
Each takes the app's `Service`, an explicit `Caller` (see
[the read hooks](read-seam.md)) and its arguments, holds every check, and
raises typed errors (`researcher_profiles.errors`). The edit routes and a
host's MCP tools are thin adapters over the same functions, so a check never
lives only in a route or only in a tool. rp-sdk supplies the hook points on
`app.state.hooks` (`edit_gate`, `write_scope`, `record_edit`); the host
supplies the credential format, the principal lookup, and the access rules.

## The `rpa_` prefix

A host distinguishing agent keys from app keys can use a prefix convention
(e.g. `rpa_` vs `rpk_`) so a leaked credential is identifiable from a log
line. The format is up to the host, e.g. `rpa_<handle>_<random>`.

## What every edit function does, in order

1. Loads the **stored** profile (never a caller's read view), or `NotFound`.
2. Runs `hooks.edit_gate(caller, profile, read_ok=False)`: may this caller edit
   this profile at all. It raises `Unauthenticated` (nobody signed in),
   `Forbidden` (signed in, may not), or `NotFound` (may not even see it). With
   no gate installed (bare rp-sdk), only the operator token edits, or anyone in
   open mode.
3. Checks the version token: a stale `base_hash` (profile) or `base_version`
   (paper) is `Conflict` carrying the current one.
4. Runs `hooks.write_scope(caller, profile, action, detail)` for each action
   the edit makes: may this caller make this specific write. It raises
   `InsufficientScope` (the credential lacks a scope; asking again could help)
   or `Forbidden` (only the owner may change this).
5. Writes, in one write unit. A patch that would produce an invalid document
   is `Invalid` and changes nothing.
6. Calls `hooks.record_edit(caller, profile, action, fields, content_hash)` once
   per action that ran, with the new content hash. This is where a host writes
   its audit row, so an edit made through any adapter is audited.
7. Drops the caches (`Service.invalidate`).

The HTTP mapper (`api/_errors.py`) turns the typed errors into replies:
`NotFound` 404, `Unauthenticated` 401, `Forbidden` 403 (with the host's
`detail` dict when it gives one), `InsufficientScope` 403 with
`{"error": "insufficient_access", "required", "missing", "hint"}`, `Conflict`
409 with `{"error": "conflict", "current", "message"}` and `X-RP-Content-Hash`
or `X-RP-Paper-Version`, `Invalid` 400, `RateLimited` 429 with `Retry-After`.

## The write-scope hook

A host that wants field-level scopes writes a callable with this shape and
installs it:

```python
def write_scope(caller: Caller, profile, action: str, detail: dict) -> None:
    # Raise InsufficientScope or Forbidden when this caller may not make this write.
    ...


app.state.hooks.write_scope = write_scope
```

The actions rp-sdk passes, and what Prosopia (the reference host) requires
for each. Prosopia gates writes part by part: each key has a table giving
every part `none`, `read`, or `write`, and a write needs `write` on every part
it touches.

| action | detail | Prosopia requires |
|---|---|---|
| `"metadata"` | `{"fields": ["name", "summary"]}` | `write` on each field's part (`summary`, `focus`, `background`, ..., or `works` for `research_outputs`); every editable field, AI-written ones included, has a part in the host's map, keyed by `researcher_profiles.edit.EDITABLE_METADATA_FIELDS` |
| `"soul"` | `{}` | `write` on `soul` |
| `"works"` | `{"paper_id": "smith2023protein", "fields": ["doi"]}` | `write` on `works`; `fields` is `["*"]` for a whole-record `PUT` and `[]` for a `DELETE` |
| `"visibility"` | `{"slug": "...", "profile_visibility": "...", "artifacts": [...]}` | never allowed for a key (`403 not_delegable`) |

A refusal the host raises as `Forbidden(detail={...})` reaches the client as
that dict under `detail`; Prosopia's looks like this:

```json
{
  "error": "insufficient_access",
  "required": ["background", "summary"],
  "missing": ["background"],
  "hint": "This needs Write on background. The account holder can allow it on the Privacy page."
}
```

A whole-profile push (`PUT /api/v1/profiles/{slug}`, what `rp push` sends) is
gated the same way in Prosopia: the push is compared with the profile it
replaces, and it lands only if every part it changes is `write`. Its 403 adds
`needs_replace`, the changes no part covers, which only a key with the
"Replace whole profiles" switch may make. `rp push` prints both lists.

## Discovery

A host serves one discovery endpoint; rp-sdk does not.

`GET /api/manage/agent/whoami`: any valid agent key. Returns the key's
identity, owner, parts table, "Replace whole profiles" switch, the profiles the
account reaches (each with the parts the key may write there), and the acts no
key may ever do (`never_delegable`). `rp agent whoami` prints it. There is no
separate catalog endpoint for keys; Prosopia lists the parts at
`GET /api/account/parts`.

## See also

- `api/service.py`: the edit and read functions, `require_edit`
- `api/hooks.py`: the hooks a host builds on; `api/caller.py`: the caller
- `api/deps.py`: `get_caller`, `get_service`, `require_scope`
- `rp-sdk/skills/profile-agent/SKILL.md`: the agent-facing instructions
