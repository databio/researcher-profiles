/**
 * Global application state for the explorer.
 *
 * Uses a simple pub/sub store pattern. Components subscribe via useStore hook.
 */

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
   * True for a home source a host application adds on its own
   * (`/api/v1/collection.json`). Never persisted to localStorage and never
   * shown with a remove button. It isn't a source the visitor added.
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
  state = { ...state, ...partial };
  emit();
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

function addSource(url: string, opts?: { builtin?: boolean }) {
  // Deduplicate
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
  // Trigger ingestion asynchronously. Never let a rejection hang the source
  // at "loading" forever: surface it as an error on the entry instead.
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
  setState({
    sources: state.sources.filter((s) => s.url !== url),
    cards: state.cards.filter((c) => c.sourceUrl !== url),
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
  // Deduplicate by normalized base URL
  const existing = new Set(state.cards.map((c) => c.base));
  const unique = newCards.filter((c) => !existing.has(c.base));
  if (unique.length > 0) {
    setState({ cards: [...state.cards, ...unique] });
  }
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
