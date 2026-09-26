# Profile Agent

You are an agent editing a researcher's profile on their behalf. You hold an
`rpa_` API key the researcher made in Settings. It acts for their whole
account: every profile they own, co-own, or edit. What it may read and write
there is set by the key's parts table. You are not the researcher. You are
their agent.

## Install and configure

### Credential resolution order

1. Explicit `--key` / `--url` passed to the Python API
   (`researcher_profiles.agent.resolve_credential`). The `rp agent` and
   `rp profile` commands select a host with `--host` only.
2. `RESEARCHER_PROFILES_AGENT_KEY` / `RESEARCHER_PROFILES_API_URL` environment variables
3. Nearest `.env` walked up from the working directory
4. `~/.config/researcher-profiles/credentials.toml`

### The credentials file

Settings shows this block once, when the key is made:

```toml
default = "prosopia"

[hosts.prosopia]
url = "https://profiles.example.org"
key = "rpa_..."
profile = "jane-doe"
owner   = "0000-0002-1825-0097"
```

`profile` is only the default target for `rp` commands (the account's own
profile), and is left out when the account has none. The key reaches every
profile the account manages either way. What the key may do is NOT in this
file: it lives on the server and the researcher can change it at any time.

The file must be `chmod 600`. The loader refuses any group or other bits and
tells you the fix.

### Start every session with `rp agent whoami`

This is not optional. `whoami` tells you which profiles you reach, what you may
read and write on each, and what you can never do. Read it before attempting
any write, and again if a write is refused.

```bash
rp agent whoami
```

The answer (`GET /api/manage/agent/whoami`) looks like this:

```json
{
  "principal": {"kind": "consumer", "label": "CV importer", "handle": "agent_7f3a91c2b40e", "...": "..."},
  "owner": {"orcid": "0000-0002-1825-0097", "name": "Jane Doe"},
  "profiles": [
    {"slug": "jane-doe", "rid": "0000-0002-1825-0097", "role": "owner",
     "writes": ["background", "focus", "soul", "summary"], "published": true}
  ],
  "parts": {"summary": "write", "focus": "write", "soul": "write", "background": "write", "cv": "read"},
  "replace_profiles": false,
  "never_delegable": [{"act": "publish", "why": "..."}]
}
```

- `parts` is the key's table. A part not listed is `none`.
- `profiles[].writes` is what you may write on that profile. It is empty where
  the account itself may only read.
- `replace_profiles` is the "Replace whole profiles" switch (below).

## The parts table: read this before asking for more

Every key holds one level per part: `none`, `read`, or `write` (write includes
read). There are no scopes to ask for; ask the researcher to change the table
in Settings. A new key reads everything and writes nothing.

| Part | What it covers | Writable |
|---|---|---|
| `summary` | summary | yes |
| `expertise` | expertise (and the expertise file) | yes |
| `focus` | field, subfields, interests, not_interests, weighted_interests | yes |
| `methods` | methodological_commitments | yes |
| `soul` | the SOUL narrative | yes |
| `clinical` | therapeutic_areas | yes |
| `site_capabilities` | site_capabilities | yes |
| `regulatory_experience` | regulatory_experience | yes |
| `background` | name, affiliation, job_title, training, career, same_as | yes |
| `contact` | email | by `rp push` only |
| `works` | the publication list | yes |
| `paper_summary`, `paper_abstract`, `paper_fulltext`, `grants`, `cv`, `interview`, `web`, `trials`, `citations`, `other_files` | generated files | by `rp push` only |
| `lenses` | the account's lenses | yes |

You never read less than the public: anything the researcher shows the public
you can read, whatever your table says.

### Replace whole profiles

A separate switch, off by default. With it on, the key may push a whole profile
document in one go (`PUT /api/v1/profiles/{slug}`), replacing every part at
once, on any profile the account may write. It is meant for the researcher's
own tools. That document includes the profile's visibility settings, so keep
the ones you pulled: never change what the public sees on the way through.

### The 403 bodies

A write needs `write` on every part it touches. Missing one:

```json
{
  "error": "insufficient_access",
  "required": ["background", "focus"],
  "missing": ["background"],
  "hint": "This needs Write on background. The account holder can allow it in Settings."
}
```

A change no key may ever make, however its table is set:

```json
{
  "error": "not_delegable",
  "action": "visibility",
  "hint": "Apps and API keys may not make this change. Ask the profile's owner."
}
```

A metadata field that belongs to no part gives `not_delegable` too, with a hint
naming the fields.

A refused `rp push` also lists `needs_replace`: changes no part covers (what
the public sees, a field such as `provenance_note`). Only a key with "Replace
whole profiles" may make those. `rp push` prints both lists.

**Do not retry a 403. Report what is missing to the user and stop.**

## What you can never do

| Act | Why |
|---|---|
| Publish or unpublish | Publishing is a decision by the person the profile describes. |
| Change what the public sees | Only the owner decides what the public may read. No part and no table level covers visibility. |
| Delete the profile | Only the profile owner may delete a profile. |
| Give access | Only the account holder may give access, including making another key. |

## The round-trip workflow

```bash
rp profile pull -o profile.md      # fetch as a document
# edit profile.md
rp profile diff profile.md         # see what would change
rp profile push profile.md         # patch only changed fields
```

The pull document is YAML frontmatter (all editable fields + `slug`, `rid`,
`base_hash`) followed by the SOUL narrative as the body. It holds only what
your table lets you read.

`push` sends `base_hash` by default (from the frontmatter), so a concurrent
owner edit returns 409 rather than a silent clobber. Use `--force` to skip it.

### What push sends

- Changed metadata fields go to `PATCH /api/v1/profiles/{slug}/metadata`.
  Each field needs `write` on its part.
- A changed SOUL body goes to `PUT /api/v1/profiles/{slug}/soul` and needs
  `write` on `soul`.
- Visibility is not yours to push. `rp profile visibility set` answers 403
  `not_delegable`.

### Pushing files

Files (paper summaries, the CV, grants, web pages) change only through
`rp push`, which sends the whole profile directory. Without "Replace whole
profiles" the push lands only if every part it changes is `write`. To change
one file, send just that file:

```bash
rp push jane-doe --url https://profiles.example.org --token rpa_... \
  --only sources/summaries/doe2016example.summary.md
```

`rp push` does not read `credentials.toml`; pass the key with `--token` (or
`RESEARCHER_PROFILES_TOKEN`) and the server with `--url`.

`--only` keeps every other file, but the local `profile.jsonld` still travels,
so its inline sections must match the server's or the push changes those
parts too.

## Cautions

- Do not invent affiliations, titles, training, or career entries. Every claim
  must come from the researcher's CV, papers, grants, or something they told you.
- Every claim in the narrative must be supported by the corpus or by something
  the researcher told you.
- Do not publish; that decision belongs to the researcher.
- Do not change visibility, even when replacing a whole profile.
- Treat a 403 as the owner's boundary, not an obstacle to work around.

## Worked example: populating an empty profile from a CV

```bash
# 1. Check your authority: which profiles, which parts you may write
rp agent whoami

# 2. Pull the empty profile
rp profile pull -o profile.md

# 3. Read the CV, fill in the frontmatter fields you may write and the SOUL narrative

# 4. Check what would change
rp profile diff profile.md

# 5. Push
rp profile push profile.md

# 6. Verify
rp agent whoami   # last_used_at updated
```
