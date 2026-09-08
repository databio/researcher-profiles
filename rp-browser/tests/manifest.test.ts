import { describe, it, expect } from "vitest";
import { normalizeBase, linkFor, summaryUrl, profileFiles } from "../src/model/manifest";
import type { Manifest, ResolvedProfile } from "../src/model/manifest";

describe("normalizeBase", () => {
  it("adds trailing slash", () => {
    expect(normalizeBase("https://example.com/profiles/alice")).toBe(
      "https://example.com/profiles/alice/"
    );
  });

  it("strips trailing profile.jsonld", () => {
    expect(
      normalizeBase("https://example.com/profiles/alice/profile.jsonld")
    ).toBe("https://example.com/profiles/alice/");
  });

  it("preserves already-normalized URL", () => {
    expect(normalizeBase("https://example.com/profiles/alice/")).toBe(
      "https://example.com/profiles/alice/"
    );
  });

  it("rejects non-http URLs", () => {
    expect(() => normalizeBase("ftp://example.com/profiles/alice")).toThrow(
      "Only http(s)"
    );
  });
});

describe("linkFor", () => {
  const manifest: Manifest = {
    "@context": "https://profiles.databio.org/context/v1.jsonld",
    "@id": "https://example.com/profiles/alice/",
    "@type": "Person",
    name: "Alice",
    rid: "0000-0000-0000-0001",
    level: "full",
    conformsTo: ["https://profiles.databio.org/context/v1.jsonld"],
    hasPart: [
      { role: "works", contentUrl: "sources/papers.jsonld", encodingFormat: "application/ld+json" },
      { role: "grants", contentUrl: "sources/grants.jsonld", encodingFormat: "application/ld+json" },
    ],
    subjectOf: [
      { role: "expertise", contentUrl: "personality/expertise.md", encodingFormat: "text/markdown" },
      { role: "soul", contentUrl: "personality/SOUL.md", encodingFormat: "text/markdown" },
    ],
  };

  it("resolves a hasPart entry's contentUrl against @id base", () => {
    const result = linkFor(manifest, "works");
    expect(result).not.toBeNull();
    expect(result!.href).toBe(
      "https://example.com/profiles/alice/sources/papers.jsonld"
    );
  });

  it("resolves a subjectOf entry by role", () => {
    const result = linkFor(manifest, "soul");
    expect(result!.href).toBe(
      "https://example.com/profiles/alice/personality/SOUL.md"
    );
  });

  it("returns null for unknown role", () => {
    expect(linkFor(manifest, "nonexistent")).toBeNull();
  });

  it("does not double the path when @id matches the profile directory", () => {
    const result = linkFor(manifest, "works");
    expect(result!.href).not.toContain("profiles/alice/profiles/alice");
  });
});

describe("summaryUrl", () => {
  const manifest: Manifest = {
    "@context": "https://profiles.databio.org/context/v1.jsonld",
    "@id": "https://example.com/profiles/alice/",
    name: "Alice",
    rid: "0000-0000-0000-0001",
    level: "full",
    hasPart: [
      {
        role: "paper_summary",
        contentUrl: "sources/summaries/smith2024foo.summary.md",
        encodingFormat: "text/markdown",
        paperId: "smith2024foo",
      },
      {
        role: "paper_summary",
        contentUrl: "sources/summaries/name-with-slashes.summary.md",
        encodingFormat: "text/markdown",
        paperId: "name/with/slashes",
      },
    ],
  };

  it("resolves the paper_summary entry for a paperId", () => {
    const url = summaryUrl(manifest, "smith2024foo");
    expect(url).toBe(
      "https://example.com/profiles/alice/sources/summaries/smith2024foo.summary.md"
    );
  });

  it("matches on the exact paperId", () => {
    const url = summaryUrl(manifest, "name/with/slashes");
    expect(url).toBe(
      "https://example.com/profiles/alice/sources/summaries/name-with-slashes.summary.md"
    );
  });

  it("returns null when no paper_summary entry matches", () => {
    expect(summaryUrl(manifest, "anything")).toBeNull();
  });
});

describe("URL resolution with relative @id (the doubled-path bug)", () => {
  it("linkFor with absolute @id produces correct single path", () => {
    const manifest: Manifest = {
      "@context": "https://profiles.databio.org/context/v1.jsonld",
      "@id": "https://profiles.example.org/profiles/doe-jane/",
      name: "Jane Doe",
      rid: "0000-0002-1825-0097",
      level: "full",
      hasPart: [
        {
          role: "works",
          contentUrl: "sources/papers.jsonld",
          encodingFormat: "application/ld+json",
        },
      ],
    };

    const result = linkFor(manifest, "works");
    expect(result!.href).toBe(
      "https://profiles.example.org/profiles/doe-jane/sources/papers.jsonld"
    );
  });
});

describe("profileFiles carries the declared tier", () => {
  const manifest: Manifest = {
    "@context": "https://profiles.databio.org/context/v1.jsonld",
    "@id": "https://example.com/profiles/alice/",
    "@type": "Person",
    name: "Alice",
    rid: "0000-0000-0000-0001",
    level: "full",
    hasPart: [
      { contentUrl: "sources/cv.md", role: "cv", visibility: "restricted" },
      { contentUrl: "sources/papers.jsonld", role: "works", visibility: "public" },
      { contentUrl: "sources/notes.md", role: "notes" },
    ],
  };

  it("keeps each entry's visibility instead of dropping it on the floor", () => {
    const files = profileFiles({ manifest, base: manifest["@id"] } as ResolvedProfile);
    const byRole = Object.fromEntries(files.map((f) => [f.role, f.visibility]));
    expect(byRole.cv).toBe("restricted");
    expect(byRole.works).toBe("public");
  });

  it("reports null, not a guess, when an entry declares no tier", () => {
    const files = profileFiles({ manifest, base: manifest["@id"] } as ResolvedProfile);
    const notes = files.find((f) => f.role === "notes");
    expect(notes?.visibility).toBeNull();
  });
});
