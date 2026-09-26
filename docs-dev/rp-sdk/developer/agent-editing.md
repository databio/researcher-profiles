# Agent editing: what a management host implements

rp-sdk's edit endpoints are gated by `app.state.owner_verifier` (see
[the read hooks](read-seam.md) and root `AGENTS.md`). A host that wants a
separate, narrower agent credential, distinct from a full owner
session, builds the pieces below on top of that hook. rp-sdk supplies the
hook points (`require_owner`, `check_write_scope`); the host supplies the
credential format, the principal lookup, and the access rules.

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
   the action and its detail; the verifier raises 403 if the credential may not
   make this write, before the write is applied.

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
    # Raise HTTPException(403) when the credential may not make this write.
    ...


app.state.write_scope_verifier = write_scope_verifier
```

The actions rp-sdk passes, and what Prosopia (the reference host) requires
for each. Prosopia gates writes part by part: each key has a table giving
every part `none`, `read`, or `write`, and a write needs `write` on every part
it touches.

| action | detail | Prosopia requires |
|---|---|---|
| `"metadata"` | `{"fields": ["name", "summary"]}` | `write` on each field's part (`summary`, `focus`, `background`, ...); the host keeps a map keyed by `researcher_profiles.edit.EDITABLE_METADATA_FIELDS` |
| `"soul"` | `{}` | `write` on `soul` |
| `"works"` | `{"paper_id": "smith2023protein", "fields": ["doi"]}` | `write` on `works`; `fields` is `["*"]` for a whole-record `PUT` and `[]` for a `DELETE` |
| `"visibility"` | `{"slug": "...", "profile_visibility": "...", "artifacts": [...]}` | never allowed for a key (`403 not_delegable`) |

The 403 it raises looks like this (FastAPI wraps it in `detail`):

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

- `api/deps.py`: `require_owner`, `require_scope`, `check_write_scope` (the rp-sdk hooks a host builds on)
- `rp-sdk/skills/profile-agent/SKILL.md`: the agent-facing instructions
