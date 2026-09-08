import { describe, expect, it, beforeEach } from "vitest";
import { useStore } from "../src/store";
import { getGroups, getCentroidsForGroup } from "../src/model/groups";

beforeEach(() => {
  useStore.setState({
    sources: [],
    cards: [],
    failures: [],
    centroids: new Map(),
  });
});

describe("getGroups", () => {
  it("returns empty array when no cards", () => {
    expect(getGroups()).toHaveLength(0);
  });

  it("partitions cards by backendSpec", () => {
    useStore.addCards([
      card("a", "st:all-MiniLM-L6-v2"),
      card("b", "st:all-MiniLM-L6-v2"),
      card("c", "openai:text-embedding-3-small"),
      card("d", null),
    ]);

    const groups = getGroups();
    expect(groups).toHaveLength(3);

    const specs = groups.map((g) => g.backendSpec).sort();
    expect(specs).toEqual([null, "openai:text-embedding-3-small", "st:all-MiniLM-L6-v2"]);

    const miniLM = groups.find((g) => g.backendSpec === "st:all-MiniLM-L6-v2")!;
    expect(miniLM.cards).toHaveLength(2);

    const nullGroup = groups.find((g) => g.backendSpec === null)!;
    expect(nullGroup.cards).toHaveLength(1);
    expect(nullGroup.hasCentroids).toBe(false);
  });

  it("reports hasCentroids when centroids are set", () => {
    useStore.addCards([card("a", "st:all-MiniLM-L6-v2")]);
    useStore.setCentroids(
      "st:all-MiniLM-L6-v2",
      new Float32Array([1, 0, 0]),
      3,
      ["a"],
    );

    const groups = getGroups();
    const g = groups.find((g) => g.backendSpec === "st:all-MiniLM-L6-v2")!;
    expect(g.hasCentroids).toBe(true);
    expect(g.dim).toBe(3);
  });

  it("passes probe from centroids to group", () => {
    useStore.addCards([card("a", "st:all-MiniLM-L6-v2")]);
    const probe = { text: "test", vector: [0.1, 0.2, 0.3] };
    useStore.setCentroids(
      "st:all-MiniLM-L6-v2",
      new Float32Array([1, 0, 0]),
      3,
      ["a"],
      probe,
    );

    const g = getGroups().find((g) => g.backendSpec === "st:all-MiniLM-L6-v2")!;
    expect(g.probe).toEqual(probe);
  });
});

describe("getCentroidsForGroup", () => {
  it("returns null when no centroids for spec", () => {
    expect(getCentroidsForGroup("nonexistent")).toBeNull();
  });

  it("returns centroid data when available", () => {
    const vectors = new Float32Array([1, 0, 0, 0, 1, 0]);
    useStore.setCentroids("st:all-MiniLM-L6-v2", vectors, 3, ["a", "b"]);
    const data = getCentroidsForGroup("st:all-MiniLM-L6-v2");
    expect(data).not.toBeNull();
    expect(data!.dim).toBe(3);
    expect(data!.order).toEqual(["a", "b"]);
  });

  it("throws on byte length mismatch", () => {
    // 3 floats but claiming 2 rows of dim 3 (needs 6 floats)
    const vectors = new Float32Array([1, 0, 0]);
    useStore.setCentroids("bad", vectors, 3, ["a", "b"]);
    expect(() => getCentroidsForGroup("bad")).toThrow("byte length mismatch");
  });
});

function card(slug: string, backendSpec: string | null) {
  return {
    slug,
    rid: null,
    name: slug,
    level: "full",
    affiliation: null,
    field: null,
    paperCount: 0,
    summaryCount: 0,
    fulltextPct: 0,
    base: `http://example.com/${slug}/`,
    sourceUrl: "http://example.com/list.json",
    backendSpec,
  };
}
