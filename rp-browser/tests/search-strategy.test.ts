import { describe, it, expect, vi, beforeEach } from "vitest";
import { resolveTier } from "../src/model/strategy";
import type { ComparisonGroup } from "../src/model/groups";

vi.mock("../src/vec/probe", () => ({
  getCachedProbe: vi.fn(() => null),
}));

import { getCachedProbe } from "../src/vec/probe";

const mockedGetCachedProbe = getCachedProbe as ReturnType<typeof vi.fn>;

function makeGroup(overrides: Partial<ComparisonGroup> = {}): ComparisonGroup {
  return {
    backendSpec: "st:all-MiniLM-L6-v2",
    cards: [],
    hasCentroids: true,
    dim: 384,
    ...overrides,
  };
}

describe("resolveTier", () => {
  beforeEach(() => {
    mockedGetCachedProbe.mockReset();
    mockedGetCachedProbe.mockReturnValue(null);
  });

  it("returns tier 3 when backendSpec is null", () => {
    const result = resolveTier(makeGroup({ backendSpec: null }));
    expect(result.tier).toBe(3);
    expect(result.probeVerdict).toBeNull();
  });

  it("returns tier 3 when hasCentroids is false", () => {
    const result = resolveTier(makeGroup({ hasCentroids: false }));
    expect(result.tier).toBe(3);
  });

  it("returns tier 2 for non-local backend with centroids", () => {
    const result = resolveTier(
      makeGroup({ backendSpec: "openai:text-embedding-3-small" }),
    );
    expect(result.tier).toBe(2);
    expect(result.probeVerdict).toBeNull();
    expect(result.reason).toContain("cannot be reproduced locally");
  });

  it("returns tier 1 when local backend probe is verified", () => {
    mockedGetCachedProbe.mockReturnValue({
      verdict: "verified",
      cosine: 0.999,
      backendSpec: "st:all-MiniLM-L6-v2",
    });
    const result = resolveTier(makeGroup());
    expect(result.tier).toBe(1);
    expect(result.probeVerdict).toBe("verified");
    expect(result.reason).toContain("matches");
  });

  it("returns tier 1 when local backend probe is degraded", () => {
    mockedGetCachedProbe.mockReturnValue({
      verdict: "degraded",
      cosine: 0.95,
      backendSpec: "st:all-MiniLM-L6-v2",
    });
    const result = resolveTier(makeGroup());
    expect(result.tier).toBe(1);
    expect(result.probeVerdict).toBe("degraded");
    expect(result.reason).toContain("partially matches");
  });

  it("returns tier 2 when local backend probe is incompatible", () => {
    mockedGetCachedProbe.mockReturnValue({
      verdict: "incompatible",
      cosine: 0.4,
      backendSpec: "st:all-MiniLM-L6-v2",
    });
    const result = resolveTier(makeGroup());
    expect(result.tier).toBe(2);
    expect(result.probeVerdict).toBe("incompatible");
    expect(result.reason).toContain("Probe test failed");
  });

  it("returns tier 2 when local backend has no cached probe", () => {
    mockedGetCachedProbe.mockReturnValue(null);
    const result = resolveTier(makeGroup());
    expect(result.tier).toBe(2);
    expect(result.probeVerdict).toBeNull();
    expect(result.reason).toContain("not yet run");
  });

  it("passes backendSpec through in every result", () => {
    const spec = "openai:text-embedding-3-small";
    const result = resolveTier(makeGroup({ backendSpec: spec }));
    expect(result.backendSpec).toBe(spec);
  });
});
