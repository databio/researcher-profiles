// @vitest-environment jsdom
/**
 * Typed interest helpers (`@rp/ui-lib` interests.ts), which must agree with
 * rp-sdk's `effective_interests` and `project_interests`, and the Interests
 * tab that groups by them.
 */
import { describe, it, expect } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { ResearchInterest } from "@rp/ui-lib/types";
import { bandOf, effectiveInterests, groupInterests, parseResearchInterests } from "@rp/ui-lib";
import { InterestsPanel } from "../src/ui/InterestsPanel";
import { interestsPrivate, researchInterests } from "../src/model/dataLayer";
import type { Manifest } from "../src/model/manifest";
import examples from "./fixtures/research-interests.json";

const text = (label: string, over: Partial<ResearchInterest>): ResearchInterest => ({
  concept: { label, unmapped: true },
  method: "inferred",
  generator: "llm",
  assertedAt: "2026-09-25T12:00:00Z",
  ...over,
});

describe("bandOf", () => {
  it.each([
    [null, "unrated"],
    [undefined, "unrated"],
    [0, "neutral"],
    [1, "core"],
    [0.5, "interested"],
    [-0.5, "lower"],
    [-1, "never"],
  ] as const)("%s is %s", (w, band) => {
    expect(bandOf(w)).toBe(band);
  });
});

describe("effectiveInterests", () => {
  it("declared beats a newer inferred entry", () => {
    const declared = text("x", { method: "declared", weight: 1, assertedAt: "2026-01-01T00:00:00Z" });
    const inferred = text("X", { weight: -0.5, assertedAt: "2026-09-01T00:00:00Z" });
    expect(effectiveInterests([inferred, declared])).toEqual([declared]);
  });

  it("the newest wins among equals, keeping first-appearance order", () => {
    const old = text("a", { method: "declared", weight: 0.5, assertedAt: "2026-01-01T00:00:00Z" });
    const other = text("b", { weight: 0.5 });
    const fresh = text("A", { method: "declared", weight: -1, assertedAt: "2026-02-01T00:00:00Z" });
    expect(effectiveInterests([old, other, fresh])).toEqual([fresh, other]);
  });

  it("reads a zoneless assertedAt as UTC", () => {
    const naive = text("a", { assertedAt: "2026-01-01T00:30:00" });
    const zoned = text("a", { assertedAt: "2026-01-01T00:00:00Z" });
    expect(effectiveInterests([zoned, naive])).toEqual([naive]);
  });
});

describe("parseResearchInterests", () => {
  it("drops malformed entries", () => {
    const ok = text("a", { weight: 0.5 });
    const got = parseResearchInterests([
      ok,
      null,
      { ...ok, weight: 2 },
      { ...ok, method: "guess" },
      { ...ok, concept: { system: "s", code: "c" } },
    ]);
    expect(got).toEqual([ok]);
    expect(parseResearchInterests("nope")).toEqual([]);
  });
});

describe("InterestsPanel", () => {
  const entries = examples as ResearchInterest[];

  it("groups the note's examples by band, unweighted in Unrated not Neutral", () => {
    const groups = groupInterests(entries);
    expect(groups.map((g) => [g.band, g.entries.map((e) => e.concept.display ?? e.concept.label)])).toEqual([
      ["interested", ["Epigenomics", "genomic interval data standards"]],
      ["neutral", ["Machine Learning"]],
      ["never", ["Chromatin Remodeling and Cancer"]],
      ["unrated", ["Genomics and Chromatin Dynamics"]],
    ]);
  });

  it("renders groups, chips, sources and paper share", () => {
    const html = renderToStaticMarkup(createElement(InterestsPanel, { entries }));
    expect(html).toContain('data-band="unrated"');
    expect(html).toContain("Unrated (suggested from papers)");
    expect(html).toContain('href="https://openalex.org/T10222"');
    expect(html).toContain("in 45% of papers");
    expect(html).toContain("you said");
    expect(html).toContain("AI guess");
    expect(html).not.toContain("Rank lower");
  });

  it("says private rather than empty when the section was withheld", () => {
    expect(renderToStaticMarkup(createElement(InterestsPanel, { entries: [] }))).toContain(
      "No interests recorded yet.",
    );
    expect(
      renderToStaticMarkup(createElement(InterestsPanel, { entries: [], withheld: true })),
    ).toContain("Interests are private on this profile.");
  });
});

describe("dataLayer interest readers", () => {
  const base = { "@id": "x", name: "A", rid: "r" } as Manifest;

  it("reads rp:researchInterests and nothing else", () => {
    expect(researchInterests({ ...base, "rp:researchInterests": examples }).length).toBe(5);
    expect(researchInterests({ ...base, research_interests: examples })).toEqual([]);
  });

  it("flags a focus section declared above public", () => {
    expect(interestsPrivate(base)).toBe(false);
    expect(
      interestsPrivate({
        ...base,
        "rp:sectionVisibility": [{ section: "focus", visibility: "private" }],
      }),
    ).toBe(true);
  });
});
