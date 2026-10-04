import { describe, it, expect } from "vitest";
import { resolveProfileRef } from "../src/router";
import type { ProfileCard, SourceEntry } from "../src/store";

const HOME = "https://prosopia.databio.org/api/v1/collection.json";
const BASE = "https://prosopia.databio.org/api/v1/profiles/sheffield-nathan/content/";

function card(slug: string, base: string, sourceUrl = HOME): ProfileCard {
  return {
    slug, rid: null, name: slug, level: "full", affiliation: null, field: null,
    paperCount: 0, summaryCount: 0, fulltextPct: 0, base, sourceUrl, backendSpec: null,
  };
}

function source(url: string, status: SourceEntry["status"], builtin = false): SourceEntry {
  return { url, kind: "registry", status, profileCount: 0, builtin };
}

describe("resolveProfileRef", () => {
  it("passes a full profile URL through untouched", () => {
    expect(resolveProfileRef(BASE, [], [])).toEqual({ kind: "url", url: BASE });
  });

  it("resolves a slug to its card's base", () => {
    const r = resolveProfileRef("sheffield-nathan", [card("sheffield-nathan", BASE)], [
      source(HOME, "ready", true),
    ]);
    expect(r).toEqual({ kind: "url", url: BASE });
  });

  it("prefers the home source when two sources share a slug", () => {
    const other = "https://elsewhere.org/profiles/sheffield-nathan/";
    const r = resolveProfileRef(
      "sheffield-nathan",
      [card("sheffield-nathan", other, "https://elsewhere.org/list.json"), card("sheffield-nathan", BASE)],
      [source("https://elsewhere.org/list.json", "ready"), source(HOME, "ready", true)],
    );
    expect(r).toEqual({ kind: "url", url: BASE });
  });

  it("waits while a source is still loading", () => {
    expect(resolveProfileRef("sheffield-nathan", [], [source(HOME, "loading", true)])).toEqual({
      kind: "pending",
    });
  });

  it("says plainly when nothing matches, instead of throwing", () => {
    const r = resolveProfileRef("sheffield-nathan", [], [source(HOME, "ready", true)]);
    expect(r.kind).toBe("error");
    if (r.kind === "error") expect(r.message).toContain("sheffield-nathan");
  });

  it("rejects a non-http URL with a clear message", () => {
    expect(resolveProfileRef("ftp://example.com/x", [], []).kind).toBe("error");
    expect(resolveProfileRef("", [], []).kind).toBe("error");
  });
});
