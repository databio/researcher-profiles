import { afterEach, describe, expect, it, vi } from "vitest";
import { profileBaseOf, staticApiChecks } from "../src/validate/complianceChecks";

describe("profileBaseOf", () => {
  it.each([
    ["https://h.org/profiles/x/content/", "https://h.org/profiles/x/content"],
    ["https://h.org/profiles/x/content", "https://h.org/profiles/x/content"],
    ["https://h.org/profiles/x/content/profile.jsonld", "https://h.org/profiles/x/content"],
    ["https://h.org/profiles/x/content/profile.jsonld/", "https://h.org/profiles/x/content"],
    ["  https://h.org/x/  ", "https://h.org/x"],
  ])("%s -> %s", (input, expected) => {
    expect(profileBaseOf(input)).toBe(expected);
  });
});

describe("staticApiChecks manifest URL", () => {
  const originalFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  it("does not double profile.jsonld when given the manifest URL", async () => {
    const seen: string[] = [];
    globalThis.fetch = vi.fn(async (url: string | URL | Request) => {
      seen.push(String(url));
      return new Response("{}", { status: 404, headers: { "Content-Type": "application/json" } });
    }) as typeof fetch;

    const checks = await staticApiChecks(
      "https://h.org/api/v1/profiles/x/content/profile.jsonld",
      () => {},
    );
    const manifest = checks.find((c) => c.id === "static-manifest-exists");
    expect(manifest?.evidence).toBe("https://h.org/api/v1/profiles/x/content/profile.jsonld");
    expect(seen.some((u) => u.includes("profile.jsonld/profile.jsonld"))).toBe(false);
  });
});
