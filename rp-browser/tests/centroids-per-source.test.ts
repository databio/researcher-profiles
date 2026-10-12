import { describe, expect, it, beforeEach } from "vitest";
import { useStore } from "../src/store";
import { getCentroidsForGroup } from "../src/model/groups";

const SPEC = "st:all-MiniLM-L6-v2";
const A = "https://a.example.org/api/v1/collection.json";
const B = "https://b.example.org/api/v1/collection.json";

function rows(...vs: number[][]): Float32Array {
  return new Float32Array(vs.flat());
}

describe("centroids from two sources with the same model", () => {
  beforeEach(() => {
    useStore.setState({
      sources: [],
      cards: [],
      failures: [],
      centroids: new Map(),
      centroidSets: new Map(),
    });
  });

  it("keeps both sources' rows in one group", () => {
    useStore.setCentroids(A, SPEC, rows([1, 0], [0, 1]), 2, ["a1", "a2"]);
    useStore.setCentroids(B, SPEC, rows([1, 1]), 2, ["b1"]);
    const data = getCentroidsForGroup(SPEC)!;
    expect(data.order).toEqual(["a1", "a2", "b1"]);
    expect(Array.from(data.vectors)).toEqual([1, 0, 0, 1, 1, 1]);
    // Row views off the buffer (as Similar Researchers does) line up.
    const b1 = new Float32Array(data.vectors.buffer, 2 * 2 * 4, 2);
    expect(Array.from(b1)).toEqual([1, 1]);
  });

  it("removing one source removes only its rows", () => {
    useStore.setCentroids(A, SPEC, rows([1, 0], [0, 1]), 2, ["a1", "a2"]);
    useStore.setCentroids(B, SPEC, rows([1, 1]), 2, ["b1"]);
    useStore.removeSource(A);
    expect(getCentroidsForGroup(SPEC)!.order).toEqual(["b1"]);
    useStore.removeSource(B);
    expect(getCentroidsForGroup(SPEC)).toBeNull();
  });

  it("keeps the first row of a profile loaded from both sources", () => {
    useStore.setCentroids(A, SPEC, rows([1, 0]), 2, ["same"]);
    useStore.setCentroids(B, SPEC, rows([0, 1], [1, 1]), 2, ["same", "b2"]);
    const data = getCentroidsForGroup(SPEC)!;
    expect(data.order).toEqual(["same", "b2"]);
    expect(Array.from(data.vectors)).toEqual([1, 0, 1, 1]);
  });

  it("skips a source whose dim differs and records a failure", () => {
    useStore.setCentroids(A, SPEC, rows([1, 0]), 2, ["a1"]);
    useStore.setCentroids(B, SPEC, rows([1, 0, 0]), 3, ["b1"]);
    expect(getCentroidsForGroup(SPEC)!.order).toEqual(["a1"]);
    expect(useStore.getState().failures.map((f) => f.url)).toEqual([B]);
  });
});
