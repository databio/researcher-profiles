import { describe, expect, it, beforeEach } from "vitest";
import { useStore, type ProfileCard } from "../src/store";

const BASE = "https://prosopia.databio.org/api/v1/profiles/sheffield-nathan/content/";
const HOME = "https://prosopia.databio.org/api/v1/collection.json";
const SINGLE = `${BASE}profile.jsonld`;

function card(over: Partial<ProfileCard>): ProfileCard {
  return {
    slug: "sheffield-nathan",
    rid: "0000-0001-5643-4068",
    name: "Nathan C. Sheffield",
    level: "full",
    affiliation: null,
    field: null,
    paperCount: 0,
    summaryCount: 0,
    fulltextPct: 0,
    base: BASE,
    sourceUrl: SINGLE,
    backendSpec: null,
    ...over,
  };
}

// The single-profile source builds a bare card; the collection bundle has the
// full row for the same person.
const sparse = card({ slug: "0000-0001-5643-4068", sourceUrl: SINGLE });
const full = card({
  affiliation: "University of Virginia",
  field: "Computational Biology",
  paperCount: 82,
  summaryCount: 82,
  sourceUrl: HOME,
});

describe("addCards dedup", () => {
  beforeEach(() => {
    useStore.setState({ sources: [], cards: [], failures: [], centroids: new Map() });
  });

  it("keeps the complete row when the sparse one loaded first", () => {
    useStore.addCards([sparse]);
    useStore.addCards([full]);
    const { cards } = useStore.getState();
    expect(cards).toHaveLength(1);
    expect(cards[0].affiliation).toBe("University of Virginia");
    expect(cards[0].paperCount).toBe(82);
  });

  it("keeps the complete row when it loaded first", () => {
    useStore.addCards([full]);
    useStore.addCards([sparse]);
    const { cards } = useStore.getState();
    expect(cards).toHaveLength(1);
    expect(cards[0].paperCount).toBe(82);
  });

  it("matches the same ORCID under a different base", () => {
    useStore.addCards([sparse]);
    useStore.addCards([card({ ...full, base: "https://mirror.example.org/p/sheffield/" })]);
    expect(useStore.getState().cards).toHaveLength(1);
    expect(useStore.getState().cards[0].paperCount).toBe(82);
  });

  it("matches the same base when one card has no rid", () => {
    useStore.addCards([card({ ...sparse, rid: null })]);
    useStore.addCards([full]);
    expect(useStore.getState().cards).toHaveLength(1);
  });

  it("keeps different people apart", () => {
    useStore.addCards([full]);
    useStore.addCards([card({ rid: "0000-0002-0000-0000", slug: "doe-jane", base: "https://x.org/doe/", sourceUrl: HOME })]);
    expect(useStore.getState().cards).toHaveLength(2);
  });

  it("falls back to the other source's row when one source is removed", () => {
    useStore.addCards([sparse]);
    useStore.addCards([full]);
    useStore.removeSource(HOME);
    const { cards } = useStore.getState();
    expect(cards).toHaveLength(1);
    expect(cards[0].sourceUrl).toBe(SINGLE);
  });
});
