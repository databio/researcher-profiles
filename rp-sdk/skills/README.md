# Agent skills

SKILL.md files that teach an LLM agent how to work with researcher profiles.
Each skill is a self-contained instruction document: point an agent at it
(by URL or local path) and it can do the task with no other context.

| Skill | What it teaches |
|---|---|
| [`src/researcher_profiles/skill/`](../src/researcher_profiles/skill/SKILL.md) | Talk to a profile: read a published profile progressively and adopt the researcher's persona, grounded in their published work |
| [`publish-profile/`](publish-profile/SKILL.md) | Publish profiles as a conformant static site |
| [`profile-agent/`](profile-agent/SKILL.md) | Act as a researcher's agent against a profile service with a scoped `rpa_` key |

The talk-to-a-profile skill lives *inside the importable package* because `rp
skill --print|--install` reads it with `importlib.resources`; the others live
here.

Only skills the public SDK can actually carry out belong in this directory;
`rp-browser/scripts/copy-skills.mjs` publishes everything it finds here to the
public site.

## Using a skill

1. Zero-install: paste a bootstrap prompt into any agent that can fetch
   URLs, e.g.:

   > Read `<skill URL>`, then follow it to read the researcher profile at
   > `<base URL>`, then answer my questions as that researcher.

2. Claude Code / Claude Desktop: save the skill as
   `~/.claude/skills/<name>/SKILL.md`. For the talk-to skill,
   `rp skill --install` does this for you.

3. Browser app: the rp-browser Skills tab lists these skills
   with links and copy-paste instructions. `rp-browser/scripts/copy-skills.mjs`
   copies the markdown into the built site so the links resolve on any deploy.
