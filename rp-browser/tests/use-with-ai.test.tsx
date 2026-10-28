// @vitest-environment jsdom
/**
 * The profile page's "Use with your AI" panel: the Skills page's "Talk to a
 * profile" prompt with this profile's base URL filled in, a Copy button, and
 * whatever the host adds through `aiExtra`.
 */
import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { UseWithAi } from "../src/ui/ProfilePage";
import { talkToProfilePrompt } from "../src/ui/Skills";
import { ShellSlotsProvider, type ProfileTabContext } from "../src/slots";

const BASE = "https://profiles.example.org/api/v1/profiles/doe-jane/content/";
const CTX: ProfileTabContext = { url: BASE, slug: "doe-jane", rid: null, sameOrigin: false };

function panel(aiExtra: ((ctx: ProfileTabContext) => React.ReactNode) | null = null): HTMLElement {
  const el = document.createElement("div");
  el.innerHTML = renderToStaticMarkup(
    <ShellSlotsProvider value={{ aiExtra }}>
      <UseWithAi ctx={CTX} />
    </ShellSlotsProvider>,
  );
  return el;
}

describe("Use with your AI", () => {
  it("fills the Talk to a profile prompt with the profile's base URL", () => {
    const prompt = talkToProfilePrompt(BASE);
    expect(prompt).toMatch(/^Read \S+\/skills\/researcher-profile\/SKILL\.md, then follow it/);
    expect(prompt).toContain(`read the researcher profile at ${BASE},`);
    expect(prompt).not.toContain("<");
  });

  it("is a closed panel holding the prompt and a Copy button", () => {
    const el = panel();
    const details = el.querySelector("details.ai-panel") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.querySelector("summary")?.textContent).toBe("Use with your AI");
    expect(details.querySelector("pre")?.textContent).toBe(talkToProfilePrompt(BASE));
    expect(details.querySelector("button")?.textContent).toBe("Copy");
  });

  it("adds the host's aiExtra", () => {
    const el = panel((ctx) => <p className="extra">for {ctx.slug}</p>);
    expect(el.querySelector(".ai-panel .extra")?.textContent).toBe("for doe-jane");
  });
});
