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

export interface FailedFetch {
  url: string;
  outcome: FetchOutcome<unknown>;
}

interface StoreState {
  sources: SourceEntry[];
  cards: ProfileCard[];
  failures: FailedFetch[];
  centroids: Map<string, { vectors: Float32Array; dim: number; order: string[]; probe?: { text: string; vector: number[] } }>;
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
  setAllCards(allCards.filter((c) => c.sourceUrl !== url), {
    sources: state.sources.filter((s) => s.url !== url),
    failures: state.failures.filter((f) => !f.url.startsWith(url)),
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

function setCentroids(
  backendSpec: string,
  vectors: Float32Array,
  dim: number,
  order: string[],
  probe?: { text: string; vector: number[] },
) {
  const next = new Map(state.centroids);
  next.set(backendSpec, { vectors, dim, order, probe });
  setState({ centroids: next });
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
