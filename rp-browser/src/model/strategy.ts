/**
 * Resolve which search capability tier is available per comparison group.
 *
 * Tier 1 (local): backend_spec === "st:all-MiniLM-L6-v2" and probe verified.
 *   Full free-text search in the page, no server.
 *
 * Tier 2 (vector-only): embeddings present but backend is not client-
 *   reproducible. Free-text unavailable; "similar to this profile", topic
 *   browse, and cluster browse still work.
 *
 * Tier 3 (no embeddings): Base-only sources. Inventory, filter, sort, topic
 *   browse. Text filtering falls back to substring match.
 */

import type { ComparisonGroup } from "../model/groups";
import { getCachedProbe, type ProbeVerdict } from "../vec/probe";

export type SearchTier = 1 | 2 | 3;

export interface TierInfo {
  tier: SearchTier;
  reason: string;
  backendSpec: string | null;
  probeVerdict: ProbeVerdict | null;
}

/** The one backend_spec the local model can reproduce. */
const LOCAL_BACKEND = "st:all-MiniLM-L6-v2";

/**
 * Determine the search capability tier for a comparison group.
 */
export function resolveTier(group: ComparisonGroup): TierInfo {
  if (!group.backendSpec || !group.hasCentroids) {
    return {
      tier: 3,
      reason: "No embeddings available for this group.",
      backendSpec: group.backendSpec,
      probeVerdict: null,
    };
  }

  if (group.backendSpec === LOCAL_BACKEND) {
    const probe = getCachedProbe(group.backendSpec);
    if (probe?.verdict === "verified" || probe?.verdict === "degraded") {
      return {
        tier: 1,
        reason:
          probe.verdict === "verified"
            ? "Local model matches publisher's embeddings."
            : `Local model partially matches (cosine ${probe.cosine?.toFixed(3)}). Results may be less accurate.`,
        backendSpec: group.backendSpec,
        probeVerdict: probe.verdict,
      };
    }
    if (probe?.verdict === "incompatible") {
      return {
        tier: 2,
        reason: `Probe test failed for ${group.backendSpec}. Free-text search disabled; similarity browsing still works.`,
        backendSpec: group.backendSpec,
        probeVerdict: probe.verdict,
      };
    }
    // Probe not yet run: report as pending, don't assume compatibility.
    return {
      tier: 2,
      reason: "Probe test not yet run. Run a search to verify local model compatibility.",
      backendSpec: group.backendSpec,
      probeVerdict: null,
    };
  }

  // Non-local backend: can use vectors for similarity but not free-text
  return {
    tier: 2,
    reason: `Backend ${group.backendSpec} cannot be reproduced locally. Free-text search unavailable; "similar to this profile" and browsing features work.`,
    backendSpec: group.backendSpec,
    probeVerdict: null,
  };
}
