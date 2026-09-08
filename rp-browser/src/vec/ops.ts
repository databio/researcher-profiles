/**
 * Pure TypeScript vector operations. No dependencies.
 *
 * cosine, rankAgainst, mmrDiversify, kmeans: mirroring the Python
 * implementations in registry.py.
 */

/**
 * Cosine similarity between two unit vectors. Since both are normalized,
 * cosine = dot product.
 */
export function cosine(a: Float32Array, b: Float32Array): number {
  if (a.length !== b.length) {
    throw new Error(
      `Vector dimension mismatch: ${a.length} vs ${b.length}. ` +
        "Vectors from different backend_spec values must never be compared.",
    );
  }
  let dot = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
  }
  return dot;
}

export interface ScoredItem {
  index: number;
  score: number;
}

/**
 * Rank all items in a group against a query vector, returning the top-k
 * by cosine similarity.
 */
export function rankAgainst(
  query: Float32Array,
  vectors: Float32Array,
  dim: number,
  k: number,
): ScoredItem[] {
  if (query.length !== dim) {
    throw new Error(
      `Query dim (${query.length}) does not match group dim (${dim}). ` +
        "Cannot compare vectors from different embedding backends.",
    );
  }

  const count = vectors.length / dim;
  const scored: ScoredItem[] = [];

  for (let i = 0; i < count; i++) {
    const row = vectors.subarray(i * dim, (i + 1) * dim);
    scored.push({ index: i, score: cosine(query, row) });
  }

  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, k);
}

/**
 * MMR (Maximal Marginal Relevance) diversification, mirroring
 * registry.diversify's MMR loop.
 *
 * @param scored - Pre-ranked items with scores
 * @param vectors - All centroid vectors (flat Float32Array)
 * @param dim - Vector dimensionality
 * @param k - Number of results to return
 * @param lambda_ - Trade-off between relevance and diversity (0=diverse, 1=relevant)
 */
export function mmrDiversify(
  scored: ScoredItem[],
  vectors: Float32Array,
  dim: number,
  k: number,
  lambda_: number = 0.5,
): ScoredItem[] {
  if (scored.length <= k) return scored;

  const selected: ScoredItem[] = [];
  const remaining = [...scored];

  // Start with the highest-scored item
  selected.push(remaining.shift()!);

  while (selected.length < k && remaining.length > 0) {
    let bestIdx = -1;
    let bestMmr = -Infinity;

    for (let i = 0; i < remaining.length; i++) {
      const candidate = remaining[i];
      const candVec = vectors.subarray(
        candidate.index * dim,
        (candidate.index + 1) * dim,
      );

      // Max similarity to any already-selected item
      let maxSim = -Infinity;
      for (const sel of selected) {
        const selVec = vectors.subarray(sel.index * dim, (sel.index + 1) * dim);
        const sim = cosine(candVec, selVec);
        if (sim > maxSim) maxSim = sim;
      }

      const mmr = lambda_ * candidate.score - (1 - lambda_) * maxSim;
      if (mmr > bestMmr) {
        bestMmr = mmr;
        bestIdx = i;
      }
    }

    if (bestIdx >= 0) {
      selected.push(remaining.splice(bestIdx, 1)[0]);
    }
  }

  return selected;
}

// ---------------------------------------------------------------------------
// k-means clustering (Lloyd's algorithm, k-means++ init)
// ---------------------------------------------------------------------------

/**
 * k-means clustering with k-means++ initialization and a fixed seed for
 * reproducibility.
 *
 * @param vectors - Flat Float32Array of all vectors
 * @param dim - Vector dimensionality
 * @param k - Number of clusters (default: max(2, round(sqrt(n))))
 * @param seed - Random seed for reproducibility
 * @param maxIter - Maximum iterations
 */
export function kmeans(
  vectors: Float32Array,
  dim: number,
  k?: number,
  seed: number = 42,
  maxIter: number = 100,
): ClusterAssignment[] {
  const n = vectors.length / dim;
  if (n === 0) return [];

  const actualK = k ?? Math.max(2, Math.round(Math.sqrt(n)));
  if (actualK >= n) {
    // Every point is its own cluster
    return Array.from({ length: n }, (_, i) => ({
      cluster: i,
      members: [i],
      exemplar: i,
    }));
  }

  // Seeded RNG (simple LCG)
  let rng = seed;
  function random(): number {
    rng = (rng * 1664525 + 1013904223) & 0x7fffffff;
    return rng / 0x7fffffff;
  }

  // k-means++ initialization
  const centers = new Float32Array(actualK * dim);
  const firstIdx = Math.floor(random() * n);
  centers.set(vectors.subarray(firstIdx * dim, (firstIdx + 1) * dim));

  for (let c = 1; c < actualK; c++) {
    const dists = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      let minDist = Infinity;
      const vec = vectors.subarray(i * dim, (i + 1) * dim);
      for (let j = 0; j < c; j++) {
        const center = centers.subarray(j * dim, (j + 1) * dim);
        let d = 0;
        for (let x = 0; x < dim; x++) {
          const diff = vec[x] - center[x];
          d += diff * diff;
        }
        if (d < minDist) minDist = d;
      }
      dists[i] = minDist;
    }

    // Weighted random selection
    let total = 0;
    for (let i = 0; i < n; i++) total += dists[i];
    let threshold = random() * total;
    let selected = 0;
    for (let i = 0; i < n; i++) {
      threshold -= dists[i];
      if (threshold <= 0) {
        selected = i;
        break;
      }
    }
    centers.set(
      vectors.subarray(selected * dim, (selected + 1) * dim),
      c * dim,
    );
  }

  // Lloyd's iterations
  const assignments = new Int32Array(n);

  for (let iter = 0; iter < maxIter; iter++) {
    let changed = false;

    // Assign each point to nearest center
    for (let i = 0; i < n; i++) {
      const vec = vectors.subarray(i * dim, (i + 1) * dim);
      let bestCluster = 0;
      let bestDist = Infinity;
      for (let c = 0; c < actualK; c++) {
        const center = centers.subarray(c * dim, (c + 1) * dim);
        let d = 0;
        for (let x = 0; x < dim; x++) {
          const diff = vec[x] - center[x];
          d += diff * diff;
        }
        if (d < bestDist) {
          bestDist = d;
          bestCluster = c;
        }
      }
      if (assignments[i] !== bestCluster) {
        assignments[i] = bestCluster;
        changed = true;
      }
    }

    if (!changed) break;

    // Recompute centers
    const counts = new Int32Array(actualK);
    centers.fill(0);
    for (let i = 0; i < n; i++) {
      const c = assignments[i];
      counts[c]++;
      const vec = vectors.subarray(i * dim, (i + 1) * dim);
      for (let x = 0; x < dim; x++) {
        centers[c * dim + x] += vec[x];
      }
    }
    for (let c = 0; c < actualK; c++) {
      if (counts[c] > 0) {
        for (let x = 0; x < dim; x++) {
          centers[c * dim + x] /= counts[c];
        }
      }
    }
  }

  // Build cluster result with exemplars (member closest to center)
  const clusters: ClusterAssignment[] = [];
  for (let c = 0; c < actualK; c++) {
    const members: number[] = [];
    for (let i = 0; i < n; i++) {
      if (assignments[i] === c) members.push(i);
    }
    if (members.length === 0) continue;

    // Find exemplar (closest to center)
    const center = centers.subarray(c * dim, (c + 1) * dim);
    let bestDist = Infinity;
    let exemplar = members[0];
    for (const m of members) {
      const vec = vectors.subarray(m * dim, (m + 1) * dim);
      let d = 0;
      for (let x = 0; x < dim; x++) {
        const diff = vec[x] - center[x];
        d += diff * diff;
      }
      if (d < bestDist) {
        bestDist = d;
        exemplar = m;
      }
    }

    clusters.push({ cluster: c, members, exemplar });
  }

  return clusters;
}

export interface ClusterAssignment {
  cluster: number;
  members: number[];
  exemplar: number;
}
