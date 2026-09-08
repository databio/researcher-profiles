import { describe, it, expect } from "vitest";
import {
  cosine,
  rankAgainst,
  mmrDiversify,
  kmeans,
  type ScoredItem,
} from "../src/vec/ops";

// Helper: build a unit vector along axis `i` in `dim` dimensions.
function basisVec(dim: number, i: number): Float32Array {
  const v = new Float32Array(dim);
  v[i] = 1;
  return v;
}

// Helper: normalize a Float32Array in place.
function normalize(v: Float32Array): Float32Array {
  let norm = 0;
  for (let i = 0; i < v.length; i++) norm += v[i] * v[i];
  norm = Math.sqrt(norm);
  for (let i = 0; i < v.length; i++) v[i] /= norm;
  return v;
}

describe("cosine", () => {
  it("returns 1.0 for identical unit vectors", () => {
    const v = basisVec(4, 0);
    expect(cosine(v, v)).toBeCloseTo(1.0, 6);
  });

  it("returns 0.0 for orthogonal unit vectors", () => {
    const a = basisVec(4, 0);
    const b = basisVec(4, 1);
    expect(cosine(a, b)).toBeCloseTo(0.0, 6);
  });

  it("returns intermediate value for angled vectors", () => {
    const a = normalize(new Float32Array([1, 1, 0]));
    const b = normalize(new Float32Array([1, 0, 0]));
    // cos(45°) ≈ 0.7071
    expect(cosine(a, b)).toBeCloseTo(Math.SQRT1_2, 4);
  });

  it("throws on dimension mismatch", () => {
    const a = new Float32Array([1, 0, 0]);
    const b = new Float32Array([1, 0]);
    expect(() => cosine(a, b)).toThrow("dimension mismatch");
  });
});

describe("rankAgainst", () => {
  const dim = 3;
  // 4 unit vectors: [1,0,0], [0,1,0], [0,0,1], normalized [1,1,0]
  const row0 = basisVec(dim, 0);                           // index 0
  const row1 = basisVec(dim, 1);                           // index 1
  const row2 = basisVec(dim, 2);                           // index 2
  const row3 = normalize(new Float32Array([1, 1, 0]));     // index 3

  const matrix = new Float32Array([...row0, ...row1, ...row2, ...row3]);

  it("returns items sorted descending by score", () => {
    const query = basisVec(dim, 0); // closest to row0, then row3
    const results = rankAgainst(query, matrix, dim, 4);
    for (let i = 1; i < results.length; i++) {
      expect(results[i - 1].score).toBeGreaterThanOrEqual(results[i].score);
    }
    expect(results[0].index).toBe(0); // exact match
  });

  it("returns at most k items", () => {
    const query = basisVec(dim, 0);
    const results = rankAgainst(query, matrix, dim, 2);
    expect(results).toHaveLength(2);
  });

  it("throws when query dim differs from group dim", () => {
    const wrongQuery = new Float32Array([1, 0]);
    expect(() => rankAgainst(wrongQuery, matrix, dim, 2)).toThrow(
      "does not match group dim",
    );
  });
});

describe("mmrDiversify", () => {
  const dim = 3;
  const row0 = basisVec(dim, 0);
  const row1 = normalize(new Float32Array([1, 0.1, 0])); // very similar to row0
  const row2 = basisVec(dim, 1);                          // orthogonal to row0
  const row3 = basisVec(dim, 2);                          // orthogonal to both

  const matrix = new Float32Array([...row0, ...row1, ...row2, ...row3]);

  const scored: ScoredItem[] = [
    { index: 0, score: 1.0 },
    { index: 1, score: 0.99 },
    { index: 2, score: 0.5 },
    { index: 3, score: 0.3 },
  ];

  it("returns fewer items than input when k < input.length", () => {
    const result = mmrDiversify(scored, matrix, dim, 2, 0.5);
    expect(result).toHaveLength(2);
  });

  it("returns input unchanged when k >= input.length", () => {
    const result = mmrDiversify(scored, matrix, dim, 10, 0.5);
    expect(result).toHaveLength(scored.length);
  });

  it("first item is always the highest scored", () => {
    const result = mmrDiversify(scored, matrix, dim, 3, 0.5);
    expect(result[0].index).toBe(0);
  });

  it("with low lambda, promotes diverse items over similar ones", () => {
    // lambda=0 → pure diversity; row1 is very similar to row0, so row2 or row3
    // should be picked before row1
    const result = mmrDiversify(scored, matrix, dim, 2, 0.0);
    expect(result[0].index).toBe(0);
    expect(result[1].index).not.toBe(1); // row1 is too similar to row0
  });
});

describe("kmeans", () => {
  it("assigns clearly separated clusters correctly with k=2", () => {
    const dim = 2;
    // Cluster A: points near [10, 0]
    // Cluster B: points near [0, 10]
    const vecs = new Float32Array([
      10, 0.1,
      10, -0.1,
      9.9, 0,
      0.1, 10,
      -0.1, 10,
      0, 9.9,
    ]);

    const clusters = kmeans(vecs, dim, 2);
    expect(clusters.length).toBe(2);

    // Each cluster should have exactly 3 members
    const sizes = clusters.map((c) => c.members.length).sort();
    expect(sizes).toEqual([3, 3]);

    // Members 0-2 should be in one cluster, 3-5 in another
    const clusterOfFirst = clusters.find((c) => c.members.includes(0))!;
    expect(clusterOfFirst.members).toContain(1);
    expect(clusterOfFirst.members).toContain(2);

    const clusterOfLast = clusters.find((c) => c.members.includes(3))!;
    expect(clusterOfLast.members).toContain(4);
    expect(clusterOfLast.members).toContain(5);
  });

  it("handles k >= n by assigning each point its own cluster", () => {
    const dim = 2;
    const vecs = new Float32Array([1, 0, 0, 1]);
    const clusters = kmeans(vecs, dim, 5);
    expect(clusters).toHaveLength(2);
    expect(clusters[0].members).toEqual([0]);
    expect(clusters[1].members).toEqual([1]);
  });

  it("returns empty array for empty input", () => {
    const clusters = kmeans(new Float32Array(0), 3, 2);
    expect(clusters).toEqual([]);
  });

  it("every index appears in exactly one cluster", () => {
    const dim = 2;
    const n = 20;
    const vecs = new Float32Array(n * dim);
    for (let i = 0; i < n * dim; i++) vecs[i] = i;

    const clusters = kmeans(vecs, dim, 4);
    const allMembers = clusters.flatMap((c) => c.members).sort((a, b) => a - b);
    expect(allMembers).toEqual(Array.from({ length: n }, (_, i) => i));
  });

  it("exemplar is always a member of its cluster", () => {
    const dim = 2;
    const vecs = new Float32Array([1, 0, 0, 1, 1, 1, 0.5, 0.5]);
    const clusters = kmeans(vecs, dim, 2);
    for (const c of clusters) {
      expect(c.members).toContain(c.exemplar);
    }
  });
});
