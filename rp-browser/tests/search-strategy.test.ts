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

  const LOCAL = "st:all-MiniLM-L6-v2";
  const probe = (verdict: string, cosine: number) => ({ verdict, cosine, backendSpec: LOCAL });

  it.each([
    {
      name: "tier 3 when backendSpec is null",
      group: { backendSpec: null },
      probe: null,
      tier: 3,
      verdict: null,
    },
    {
      name: "tier 3 when hasCentroids is false",
      group: { hasCentroids: false },
      probe: null,
      tier: 3,
      verdict: null,
    },
    {
      name: "tier 2 for non-local backend with centroids",
      group: { backendSpec: "openai:text-embedding-3-small" },
      probe: null,
      tier: 2,
      verdict: null,
      reasonContains: "cannot be reproduced locally",
    },
    {
      name: "tier 1 when local backend probe is verified",
      group: {},
      probe: probe("verified", 0.999),
      tier: 1,
      verdict: "verified",
      reasonContains: "matches",
    },
    {
      name: "tier 1 when local backend probe is degraded",
      group: {},
      probe: probe("degraded", 0.95),
      tier: 1,
      verdict: "degraded",
      reasonContains: "partially matches",
    },
    {
      name: "tier 2 when local backend probe is incompatible",
      group: {},
      probe: probe("incompatible", 0.4),
      tier: 2,
      verdict: "incompatible",
      reasonContains: "Probe test failed",
    },
    {
      name: "tier 2 when local backend has no cached probe",
      group: {},
      probe: null,
      tier: 2,
      verdict: null,
      reasonContains: "not yet run",
    },
  ] as {
    name: string;
    group: Partial<ComparisonGroup>;
    probe: ReturnType<typeof probe> | null;
    tier: number;
    verdict: string | null;
    reasonContains?: string;
  }[])("returns $name", ({ group, probe, tier, verdict, reasonContains }) => {
    mockedGetCachedProbe.mockReturnValue(probe);
    const result = resolveTier(makeGroup(group));
    expect(result.tier).toBe(tier);
    expect(result.probeVerdict).toBe(verdict);
    if (reasonContains) expect(result.reason).toContain(reasonContains);
  });

  it("passes backendSpec through in every result", () => {
    const spec = "openai:text-embedding-3-small";
    const result = resolveTier(makeGroup({ backendSpec: spec }));
    expect(result.backendSpec).toBe(spec);
  });
});
