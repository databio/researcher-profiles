import { useState } from "react";

const REPO_URL = "https://github.com/databio/researcher-profiles";

type SkillDef = {
  slug: string;
  title: string;
  blurb: string;
  prompt: string;
  extras?: { label: string; path: string }[];
  install?: string;
};

const SKILLS: SkillDef[] = [
  {
    slug: "researcher-profile",
    title: "Talk to a profile",
    blurb:
      "Adopt a researcher's persona and chat with them. Interview them, consult them, or ask questions grounded in their published work. Teaches an agent to read a published profile progressively (the profile.jsonld document first, then its persona documents, then individual paper summaries on demand), answer in the researcher's voice, cite papers inline, and say where the profile came from.",
    prompt:
      "Read <SKILL_URL>, then follow it to read the researcher profile at <profile base URL>, then answer my questions as that researcher.",
    extras: [
      { label: "Read order", path: "researcher-profile/reference/read-order.md" },
      { label: "Failure modes", path: "researcher-profile/reference/failure-modes.md" },
      { label: "Worked example", path: "researcher-profile/examples/walkthrough.md" },
    ],
    install: "rp skill --install",
  },
  {
    slug: "profile-agent",
    title: "Edit as an agent",
    blurb:
      "Act as a scoped editor for a researcher's profile. You hold an agent key with named write scopes on one profile: edit metadata, update the expertise narrative, or manage publications on the owner's behalf through the registry API.",
    prompt:
      "Read <SKILL_URL>, then follow it to edit the researcher profile using my agent key.",
  },
  {
    slug: "publish-profile",
    title: "Publish a profile",
    blurb:
      "Turn built profiles into a conformant static site with rp render and rp site, then host it on Cloudflare, S3, or your own server. Covers the CORS and Content-Type requirements that make a profile readable by browsers, crawlers, and agents, and how to verify a deployment.",
    prompt:
      "Read <SKILL_URL>, then follow it to publish the profiles at <path> to <host>.",
  },
];

function skillUrl(path: string): string {
  return new URL(`skills/${path}`, document.baseURI).href;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn btn--ghost text-sm"
      onClick={() => {
        navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

export function Skills() {
  return (
    <div className="content-page">
      <h1 className="content-page__title">Skills</h1>

      <section className="content-page__section">
        <p>
          A <strong>skill</strong> is a self-contained instruction document
          (SKILL.md) that teaches an AI agent how to work with researcher
          profiles. Point any agent that can fetch URLs at a skill and it can
          do the task with no other context: no plugin, no special tooling.
        </p>
        <p>To use one, either:</p>
        <ul className="content-page__list">
          <li>
            Paste a prompt: copy the bootstrap prompt from a
            card below into any chat agent, filling in the bracketed blanks.
          </li>
          <li>
            Install locally: save the SKILL.md under{" "}
            <code>~/.claude/skills/&lt;name&gt;/SKILL.md</code> (Claude Code
            loads it automatically).
          </li>
        </ul>
      </section>

      {SKILLS.map((s) => {
        const url = skillUrl(`${s.slug}/SKILL.md`);
        const prompt = s.prompt.replace("<SKILL_URL>", url);
        return (
          <section key={s.slug} className="card mb-8">
            <div className="card__head">
              <h2 className="card__title">{s.title}</h2>
              <a
                className="card__link"
                href={url}
                target="_blank"
                rel="noopener noreferrer"
              >
                SKILL.md →
              </a>
            </div>
            <p className="text-subtle mb-2">{s.blurb}</p>
            <div className="skills-prompt-row">
              <pre className="skills-prompt-box">{prompt}</pre>
              <CopyButton text={prompt} />
            </div>
            {s.install && (
              <p className="skills-extras">
                Local install (requires the SDK already installed):{" "}
                <code>{s.install}</code>
              </p>
            )}
            {s.extras && (
              <p className="skills-extras">
                Companion documents:{" "}
                {s.extras.map((e, i) => (
                  <span key={e.path}>
                    {i > 0 && " · "}
                    <a
                      href={skillUrl(e.path)}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {e.label}
                    </a>
                  </span>
                ))}
              </p>
            )}
          </section>
        );
      })}

      <section className="content-page__section">
        <p>
          Skill sources live in the{" "}
          <a href={REPO_URL} target="_blank" rel="noopener noreferrer">
            researcher-profiles repository
          </a>{" "}
          (<code>rp-sdk/src/researcher_profiles/skill/</code> and{" "}
          <code>rp-sdk/skills/</code>).
        </p>
      </section>
    </div>
  );
}
