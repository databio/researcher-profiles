/** Global pub/sub state for the explorer. Components subscribe via `useStore`. */

import type { FetchOutcome } from "./net/fetchJson";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SourceEntry {
  url: string;
  kind: "list" | "registry" | "profile" | "unknown";
  status: "loading" | "ready" | "error" | "unauthorized";
  error?: string;
  profileCount: number;
  backendSpec?: string | null;
  /**
   * A home source the host app adds itself, not the visitor. Never persisted
   * and never removable.
   */
  builtin?: boolean;
}

export interface ProfileCard {
  slug: string;
  rid: string | null;
  name: string;
  level: string;
  affiliation: string | null;
  field: string | null;
  paperCount: number;
  summaryCount: number;
  fulltextPct: number;
  base: string;
  sourceUrl: string;
  backendSpec: string | null;
}

/** One source's centroid matrix for one backend_spec: row i is `order[i]`. */
export interface CentroidData {
  vectors: Float32Array;
  dim: number;
  order: string[];
  probe?: { text: string; vector: number[] };
}

export interface FailedFetch {
  url: string;
  outcome: FetchOutcome<unknown>;
}

interface StoreState {
  sources: SourceEntry[];
  cards: ProfileCard[];
  failures: FailedFetch[];
  /**
   * Per backend_spec, one matrix merging every loaded source with that spec
   * (see `mergeCentroidSets`). Derived from `centroidSets`; read this one.
   */
  centroids: Map<string, CentroidData>;
  /**
   * Per backend_spec, each source's own centroids, keyed by source URL. Two
   * sources sharing a model must not overwrite each other.
   */
  centroidSets: Map<string, Map<string, CentroidData>>;
}

type Listener = () => void;

// ---------------------------------------------------------------------------
// Store singleton
// ---------------------------------------------------------------------------

let state: StoreState = {
  sources: [],
  cards: [],
  failures: [],
  centroids: new Map(),
  centroidSets: new Map(),
};

const listeners = new Set<Listener>();

function emit() {
  for (const fn of listeners) fn();
}

function getState(): StoreState {
  return state;
}

function setState(partial: Partial<StoreState>) {
  // Cards set directly become the full card list too, so later adds and
  // removals start from what the caller set.
  if (partial.cards) allCards = partial.cards;
  state = { ...state, ...partial };
  emit();
}

// ---------------------------------------------------------------------------
// Card dedup
// ---------------------------------------------------------------------------

/**
 * Every card from every source, duplicates included. `state.cards` is the
 * deduplicated view of this list. Keeping the full list means removing one
 * source falls back to another source's row for the same person instead of
 * dropping that person.
 */
let allCards: ProfileCard[] = [];

/** An ORCID in any of its spellings, reduced to the bare 16-digit id. */
function ridKey(rid: string | null): string | null {
  if (!rid) return null;
  const bare = rid.trim().replace(/^https?:\/\/orcid\.org\//i, "").toUpperCase();
  return bare || null;
}

/** How much of the browse row a card fills in. */
function completeness(c: ProfileCard): number {
  return (
    (c.affiliation ? 1 : 0) +
    (c.field ? 1 : 0) +
    (c.paperCount > 0 ? 1 : 0) +
    (c.summaryCount > 0 ? 1 : 0) +
    (c.fulltextPct > 0 ? 1 : 0) +
    (c.backendSpec ? 1 : 0)
  );
}

/**
 * One card per person. Two cards are the same person when they share an
 * ORCID (rid) or a normalized base URL. Of the duplicates a card from a
 * builtin (home) source wins, as in `resolveProfileRef`; among cards of equal
 * standing the most complete is kept, and on a tie the one loaded first wins.
 */
export function dedupeCards(
  cards: ProfileCard[],
  builtinUrls: ReadonlySet<string> = new Set(),
): ProfileCard[] {
  const rank = (c: ProfileCard) => (builtinUrls.has(c.sourceUrl) ? 1 : 0);
  const kept: ProfileCard[] = [];
  const byRid = new Map<string, number>();
  const byBase = new Map<string, number>();
  for (const card of cards) {
    const rid = ridKey(card.rid);
    const idx = (rid !== null ? byRid.get(rid) : undefined) ?? byBase.get(card.base);
    if (idx === undefined) {
      const at = kept.length;
      kept.push(card);
      if (rid !== null) byRid.set(rid, at);
      byBase.set(card.base, at);
      continue;
    }
    const cur = kept[idx];
    if (rank(card) > rank(cur) || (rank(card) === rank(cur) && completeness(card) > completeness(cur))) {
      kept[idx] = card;
    }
    if (rid !== null && !byRid.has(rid)) byRid.set(rid, idx);
    if (!byBase.has(card.base)) byBase.set(card.base, idx);
  }
  return kept;
}

function builtinSourceUrls(sources: SourceEntry[]): Set<string> {
  return new Set(sources.filter((s) => s.builtin).map((s) => s.url));
}

/** Replace the full card list and publish its deduplicated view. */
function setAllCards(next: ProfileCard[], extra?: Partial<StoreState>) {
  allCards = next;
  state = { ...state, ...extra, cards: dedupeCards(next, builtinSourceUrls(state.sources)) };
  emit();
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

function addSource(url: string, opts?: { builtin?: boolean }) {
  if (state.sources.some((s) => s.url === url)) return;
  setState({
    sources: [
      ...state.sources,
      {
        url,
        kind: "unknown",
        status: "loading",
        profileCount: 0,
        builtin: opts?.builtin ?? false,
      },
    ],
  });
  // A rejection must not leave the source stuck at "loading".
  import("./model/sources")
    .then((mod) => mod.ingestSource(url))
    .catch((err) => {
      updateSource(url, {
        status: "error",
        error: err instanceof Error ? err.message : String(err),
      });
    });
}

function removeSource(url: string) {
  const centroidSets = new Map<string, Map<string, CentroidData>>();
  for (const [spec, bySource] of state.centroidSets) {
    const kept = new Map([...bySource].filter(([src]) => src !== url));
    if (kept.size > 0) centroidSets.set(spec, kept);
  }
  setAllCards(allCards.filter((c) => c.sourceUrl !== url), {
    sources: state.sources.filter((s) => s.url !== url),
    failures: state.failures.filter((f) => !f.url.startsWith(url)),
    centroidSets,
    centroids: mergeCentroidSets(centroidSets),
  });
}

function updateSource(url: string, patch: Partial<SourceEntry>) {
  setState({
    sources: state.sources.map((s) =>
      s.url === url ? { ...s, ...patch } : s,
    ),
  });
}

function addCards(newCards: ProfileCard[]) {
  if (newCards.length === 0) return;
  // The same source re-ingested (same base, same source) replaces nothing.
  const seen = new Set(allCards.map((c) => `${c.sourceUrl}\n${c.base}`));
  const fresh = newCards.filter((c) => !seen.has(`${c.sourceUrl}\n${c.base}`));
  if (fresh.length === 0) return;
  setAllCards([...allCards, ...fresh]);
}

/**
 * The person's name for a source, taken from that source's own cards before
 * dedup. A source whose person is shown from another source's card still has
 * a name this way. Null when the source has no cards.
 */
function sourceName(url: string): string | null {
  return allCards.find((c) => c.sourceUrl === url)?.name ?? null;
}

function addFailure(failure: FailedFetch) {
  setState({ failures: [...state.failures, failure] });
}

function clearFailures() {
  setState({ failures: [] });
}

/**
 * One matrix per backend_spec: every source's rows concatenated, in source
 * load order, with their `order` entries concatenated alongside. A profile
 * (same `order` key) loaded from two sources keeps its first row only. Every
 * source in a spec shares the first source's dim; one that does not is left out
 * (`setCentroids` records it as a failure). The vectors are copied into a
 * fresh buffer starting at offset 0, so `vectors.buffer` row views line up.
 */
export function mergeCentroidSets(
  sets: Map<string, Map<string, CentroidData>>,
): Map<string, CentroidData> {
  const merged = new Map<string, CentroidData>();
  for (const [spec, bySource] of sets) {
    const parts = [...bySource.values()];
    if (parts.length === 0) continue;
    const dim = parts[0].dim;
    const same = parts.filter((p) => p.dim === dim);
    const rows = same.reduce((n, p) => n + p.order.length, 0);
    const buf = new Float32Array(rows * dim);
    const order: string[] = [];
    const seen = new Set<string>();
    for (const p of same) {
      p.order.forEach((key, i) => {
        if (seen.has(key)) return;
        seen.add(key);
        buf.set(p.vectors.subarray(i * dim, (i + 1) * dim), order.length * dim);
        order.push(key);
      });
    }
    const vectors = buf.slice(0, order.length * dim);
    merged.set(spec, { vectors, dim, order, probe: same.find((p) => p.probe)?.probe });
  }
  return merged;
}

function setCentroids(
  sourceUrl: string,
  backendSpec: string,
  vectors: Float32Array,
  dim: number,
  order: string[],
  probe?: { text: string; vector: number[] },
) {
  const expected = order.length * dim * 4;
  if (vectors.byteLength !== expected) {
    throw new Error(
      `Centroid byte length mismatch for ${backendSpec}: ` +
        `expected ${expected}, got ${vectors.byteLength}`,
    );
  }
  const bySource = new Map(state.centroidSets.get(backendSpec) ?? []);
  bySource.delete(sourceUrl);
  const first = bySource.values().next().value as CentroidData | undefined;
  if (first && first.dim !== dim) {
    addFailure({
      url: sourceUrl,
      outcome: {
        ok: false,
        kind: "schema",
        url: sourceUrl,
        detail:
          `centroids for ${backendSpec} have dim ${dim}, but already-loaded sources ` +
          `with that model have dim ${first.dim}; this source's centroids are not used`,
      },
    });
    return;
  }
  bySource.set(sourceUrl, { vectors, dim, order, probe });
  const centroidSets = new Map(state.centroidSets);
  centroidSets.set(backendSpec, bySource);
  setState({ centroidSets, centroids: mergeCentroidSets(centroidSets) });
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

import { useSyncExternalStore } from "react";

export const useStore = Object.assign(
  function useStoreHook(): StoreState {
    return useSyncExternalStore(
      (cb) => {
        listeners.add(cb);
        return () => listeners.delete(cb);
      },
      getState,
      getState,
    );
  },
  {
    getState,
    setState,
    addSource,
    removeSource,
    updateSource,
    addCards,
    sourceName,
    addFailure,
    clearFailures,
    setCentroids,
  },
);

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

const SOURCES_KEY = "rp-browserr-sources";

export function loadPersistedSources() {
  try {
    const raw = localStorage.getItem(SOURCES_KEY);
    if (raw) {
      const urls: string[] = JSON.parse(raw);
      for (const url of urls) addSource(url);
    }
  } catch {
    // ignore
  }
}

export function persistSources() {
  try {
    localStorage.setItem(
      SOURCES_KEY,
      JSON.stringify(state.sources.filter((s) => !s.builtin).map((s) => s.url)),
    );
  } catch {
    // ignore
  }
}
