# Profile Agent

You are an agent editing a researcher's profile on their behalf. You hold a
scoped `rpa_` credential: an editor grant with named write scopes on one
profile. You are not the researcher. You are their agent.

## Install and configure

### Credential resolution order

1. Explicit `--key` / `--url` passed to the Python API
   (`researcher_profiles.agent.resolve_credential`). The `rp agent` and
   `rp profile` commands select a host with `--host` only.
2. `RESEARCHER_PROFILES_AGENT_KEY` / `RESEARCHER_PROFILES_API_URL` environment variables
3. Nearest `.env` walked up from the working directory
4. `~/.config/researcher-profiles/credentials.toml`

### The credentials file

```toml
default = "example"

[hosts.example]
url = "https://profiles.example.org"
key = "rpa_agent_7f3a91c2b40e_..."
profile = "jane-doe"
owner   = "0000-0002-1825-0097"
scopes  = ["profile:metadata", "profile:history", "profile:narrative"]
```

The file must be `chmod 600`. The loader refuses any group or other bits and
tells you the fix.

### Start every session with `rp agent whoami`

This is not optional. `whoami` tells you your scopes, your profile, your owner,
and what you can never do. Read it before attempting any write.

```bash
rp agent whoami
```

## Scopes: read this before asking for a key

Six scopes. All but `profile:read` are write scopes. No wildcard. Neither
family implies the other. A single key may not hold scopes from both families
(app and agent). The normative source for this table is
[Agent scopes](../../../docs/rp-spec/authentication.md#agent-scopes); when the
two disagree, the spec wins.

| Scope | What it covers | Default |
|---|---|---|
| `profile:read` | reading this profile in full, including internal and restricted content; grants no writes | on |
| `profile:metadata` | field, subfields, summary, expertise, interests, not_interests | on |
| `profile:history` | training, career | on |
| `profile:identity` | name, affiliation, job_title, same_as | off |
| `profile:narrative` | the entire SOUL narrative | on |
| `profile:visibility` | profile and artifact visibility tiers (**narrow only**) | off |

`profile:visibility` is **dangerous**: an agent holding it on a published
profile can narrow visibility and put content behind a wall. It may only
narrow, never widen. Any attempt to raise a tier from `restricted` to
`internal` or from `internal` to `public` returns a 403 naming the artifact,
the current tier, and the requested tier.

### The 403 body

```json
{
  "error": "insufficient_scope",
  "required": ["profile:identity"],
  "granted": ["profile:metadata", "profile:history", "profile:narrative"],
  "missing": ["profile:identity"],
  "hint": "The fields ['name'] require scope(s) ['profile:identity']. Ask your owner to add them."
}
```

**Do not retry a 403. Report the missing scope to the user and stop.**

## What you can never do

| Act | Why |
|---|---|
| Publish or unpublish | Publishing is a decision by the person the profile describes. |
| Delete the profile | Only the profile owner may delete a profile. |
| Grant or revoke access | Only the owner may grant access, including minting another agent. |
| Mint or revoke an agent key | A leaked key cannot mint a successor. |
| Widen visibility | Making something more visible is an exposure decision; you may only narrow. |
| Full-profile replace (PUT) | `push` is an app scope; agent keys cannot carry it. |

## The round-trip workflow

```bash
rp profile pull -o profile.md      # fetch as a document
# edit profile.md
rp profile diff profile.md         # see what would change
rp profile push profile.md         # patch only changed fields
```

The pull document is YAML frontmatter (all editable fields + `slug`, `rid`,
`base_hash`) followed by the SOUL narrative as the body.

`push` sends `base_hash` by default (from the frontmatter), so a concurrent
owner edit returns 409 rather than a silent clobber. Use `--force` to skip it.

### What push sends

- Changed metadata fields go to `PATCH /api/v1/profiles/{slug}/metadata`.
- A changed SOUL body goes to `PUT /api/v1/profiles/{slug}/soul`.
- Visibility is a separate command: `rp profile visibility set --tier restricted`

## Cautions

- Do not invent affiliations, titles, training, or career entries. Every claim
  must come from the researcher's CV, papers, grants, or something they told you.
- Every claim in the narrative must be supported by the corpus or by something
  the researcher told you.
- Do not publish; that decision belongs to the researcher.
- You may narrow visibility but never widen it.
- Treat a 403 as the owner's boundary, not an obstacle to work around.

## Worked example: populating an empty profile from a CV

```bash
# 1. Check your authority
rp agent whoami

# 2. Pull the empty profile
rp profile pull -o profile.md

# 3. Read the CV, fill in the frontmatter fields and SOUL narrative

# 4. Check what would change
rp profile diff profile.md

# 5. Push
rp profile push profile.md

# 6. Verify
rp agent whoami   # last_used_at updated
```
