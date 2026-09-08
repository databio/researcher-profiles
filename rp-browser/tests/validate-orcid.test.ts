import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { isValidOrcidChecksum, checkOrcidRoundTrip } from "../src/validate/orcid";

describe("isValidOrcidChecksum", () => {
  it.each([
    ["0000-0002-1825-0097", true, "valid ORCID with numeric check digit"],
    ["0000-0000-0000-001X", true, "valid ORCID with X check digit"],
    ["0000000218250097", true, "valid ORCID without dashes"],
    ["0000-0002-1825-0098", false, "wrong check digit"],
    ["0000-0002-1825", false, "too short"],
    ["0000-0002-1825-00971", false, "too long"],
    ["000A-0002-1825-0097", false, "non-digit character in the body"],
  ])("%s -> %s (%s)", (orcid, expected) => {
    expect(isValidOrcidChecksum(orcid)).toBe(expected);
  });
});

describe("checkOrcidRoundTrip", () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("returns passed:false for invalid checksum without making a network call", async () => {
    const fetchSpy = vi.fn();
    globalThis.fetch = fetchSpy;

    const result = await checkOrcidRoundTrip("0000-0000-0000-0000", "https://example.com/profile/");
    expect(result.passed).toBe(false);
    expect(result.severity).toBe("badge");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("returns indeterminate when ORCID API returns non-200", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
    });

    const result = await checkOrcidRoundTrip("0000-0002-1825-0097", "https://example.com/profile/");
    expect(result.passed).toBe("indeterminate");
    expect(result.severity).toBe("badge");
  });

  it("returns passed:true when ORCID record contains matching URL", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        person: {
          "researcher-urls": {
            "researcher-url": [
              { url: { value: "https://example.com/profile/" } },
            ],
          },
        },
      }),
    });

    const result = await checkOrcidRoundTrip("0000-0002-1825-0097", "https://example.com/profile/");
    expect(result.passed).toBe(true);
    expect(result.severity).toBe("badge");
  });

  it("matches URLs ignoring trailing profile.jsonld", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        person: {
          "researcher-urls": {
            "researcher-url": [
              { url: { value: "https://example.com/profiles/alice/" } },
            ],
          },
        },
      }),
    });

    const result = await checkOrcidRoundTrip(
      "0000-0002-1825-0097",
      "https://example.com/profiles/alice/profile.jsonld",
    );
    expect(result.passed).toBe(true);
    expect(result.severity).toBe("badge");
  });

  it("returns passed:false when ORCID record has no matching URL", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        person: {
          "researcher-urls": {
            "researcher-url": [
              { url: { value: "https://other.example.org/someone/" } },
            ],
          },
        },
      }),
    });

    const result = await checkOrcidRoundTrip("0000-0002-1825-0097", "https://example.com/profile/");
    expect(result.passed).toBe(false);
    expect(result.severity).toBe("badge");
    expect(result.fix).toBeDefined();
  });

  it("returns indeterminate on network error", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("Network failure"));

    const result = await checkOrcidRoundTrip("0000-0002-1825-0097", "https://example.com/profile/");
    expect(result.passed).toBe("indeterminate");
    expect(result.severity).toBe("badge");
  });

  it("handles empty researcher-urls gracefully", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        person: {
          "researcher-urls": {
            "researcher-url": [],
          },
        },
      }),
    });

    const result = await checkOrcidRoundTrip("0000-0002-1825-0097", "https://example.com/profile/");
    expect(result.passed).toBe(false);
    expect(result.severity).toBe("badge");
  });
});
