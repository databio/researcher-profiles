/**
 * Comparison groups: partition every loaded profile by its backend_spec.
 *
 * Every vector operation in the app takes a group, never the whole corpus.
 * There must be no code path that can compare two vectors from different
 * backend_spec values or different dim.
 */

import { useStore, type ProfileCard } from "../store";

export interface ComparisonGroup {
  backendSpec: string | null;
  cards: ProfileCard[];
  /** True when centroids are loaded for this group. */
  hasCentroids: boolean;
  dim: number | null;
  probe?: { text: string; vector: number[] };
}

/**
 * Partition all loaded cards by backend_spec. Profiles with no embeddings
 * land in a null group.
 */
export function getGroups(): ComparisonGroup[] {
  const state = useStore.getState();
  const bySpec = new Map<string | null, ProfileCard[]>();

  for (const card of state.cards) {
    const spec = card.backendSpec;
    const existing = bySpec.get(spec) || [];
    existing.push(card);
    bySpec.set(spec, existing);
  }

  const groups: ComparisonGroup[] = [];
  for (const [spec, cards] of bySpec) {
    const centroidData = spec ? state.centroids.get(spec) : undefined;
    groups.push({
      backendSpec: spec,
      cards,
      hasCentroids: !!centroidData,
      dim: centroidData?.dim ?? null,
      probe: centroidData?.probe,
    });
  }

  return groups;
}

/**
 * Get centroids for a specific backend_spec group. Asserts matching
 * backend_spec and dim.
 */
export function getCentroidsForGroup(
  backendSpec: string,
): { vectors: Float32Array; dim: number; order: string[] } | null {
  const state = useStore.getState();
  const data = state.centroids.get(backendSpec);
  if (!data) return null;

  // Assert consistency
  const expectedBytes = data.order.length * data.dim * 4;
  if (data.vectors.byteLength !== expectedBytes) {
    throw new Error(
      `Centroid byte length mismatch for ${backendSpec}: ` +
        `expected ${expectedBytes}, got ${data.vectors.byteLength}`,
    );
  }

  return data;
}
