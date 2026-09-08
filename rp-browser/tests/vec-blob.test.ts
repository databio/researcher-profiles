import { describe, expect, it } from "vitest";
import { parseCentroidBlob } from "../src/vec/blob";

function makeBlob(rows: number[][], dim: number): ArrayBuffer {
  const buf = new ArrayBuffer(rows.length * dim * 4);
  const view = new Float32Array(buf);
  for (let i = 0; i < rows.length; i++) {
    for (let j = 0; j < dim; j++) {
      view[i * dim + j] = rows[i][j];
    }
  }
  return buf;
}

describe("parseCentroidBlob", () => {
  it("parses a correctly-sized blob", () => {
    const dim = 3;
    const rows = [
      [1, 0, 0],
      [0, 1, 0],
    ];
    const buf = makeBlob(rows, dim);
    const data = parseCentroidBlob(buf, 2, dim);
    expect(data.count).toBe(2);
    expect(data.dim).toBe(dim);
    expect(data.vectors).toHaveLength(6);
  });

  it("throws on byte-length mismatch", () => {
    const buf = new ArrayBuffer(12); // 3 floats
    expect(() => parseCentroidBlob(buf, 2, 3)).toThrow("byte length mismatch");
  });

  it("row accessor returns correct subarray", () => {
    const dim = 2;
    const rows = [
      [1, 0],
      [0, 1],
    ];
    const data = parseCentroidBlob(makeBlob(rows, dim), 2, dim);
    const r0 = data.row(0);
    expect(r0[0]).toBeCloseTo(1, 5);
    expect(r0[1]).toBeCloseTo(0, 5);
    const r1 = data.row(1);
    expect(r1[0]).toBeCloseTo(0, 5);
    expect(r1[1]).toBeCloseTo(1, 5);
  });

  it("row accessor throws on out-of-bounds index", () => {
    const data = parseCentroidBlob(makeBlob([[1, 0]], 2), 1, 2);
    expect(() => data.row(-1)).toThrow("out of bounds");
    expect(() => data.row(1)).toThrow("out of bounds");
  });

  it("re-normalizes an unnormalized row", () => {
    const dim = 3;
    // Not unit length, so normalization is observable: length = sqrt(4+4+4) = sqrt(12) ≈ 3.46
    const rows = [[2, 2, 2]];
    const data = parseCentroidBlob(makeBlob(rows, dim), 1, dim);
    const row = data.row(0);
    let norm = 0;
    for (let i = 0; i < dim; i++) norm += row[i] * row[i];
    expect(Math.sqrt(norm)).toBeCloseTo(1.0, 3);
  });

  it("does not re-normalize already-unit-length rows", () => {
    const dim = 3;
    const val = 1 / Math.sqrt(3);
    const rows = [[val, val, val]];
    const data = parseCentroidBlob(makeBlob(rows, dim), 1, dim);
    const row = data.row(0);
    expect(row[0]).toBeCloseTo(val, 5);
    expect(row[1]).toBeCloseTo(val, 5);
    expect(row[2]).toBeCloseTo(val, 5);
  });
});
