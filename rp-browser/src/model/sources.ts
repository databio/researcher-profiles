/**
 * Source ingestion: sniff a user-supplied URL, resolve it, and load profiles
 * into the global store.
 *
 * A source URL can be:
 * - A JSON array -> profile list document
 * - An object with `cards` -> collection bundle
 * - A `schema:Person` document (has `hasPart`/`subjectOf`) -> a single profile
 */

import { fetchJson, type FetchOutcome } from "../net/fetchJson";
import { fetchBinary } from "../net/fetchBinary";
import { normalizeBase, loadManifest, clearManifestCache } from "./manifest";
import { useStore, type ProfileCard, type FailedFetch } from "../store";

/** Concurrency cap for fan-out fetches. */
const CONCURRENCY = 6;

/**
 * Ingest a source URL: sniff its kind, fetch profiles, and update the store.
 */
export async function ingestSource(sourceUrl: string): Promise<void> {
  const outcome = await fetchJson<unknown>(sourceUrl);

  if (!outcome.ok) {
    // A 401/403 means this is a private registry, not a publishing defect.
    // Give it its own status and keep it out of Diagnostics (which is the
    // "something is broken about how this was published" surface).
    if (outcome.kind === "unauthorized" || outcome.kind === "forbidden") {
      useStore.updateSource(sourceUrl, {
        status: "unauthorized",
        error: outcome.detail,
      });
      return;
    }
    useStore.updateSource(sourceUrl, {
      status: "error",
      error: outcome.detail,
    });
    useStore.addFailure({ url: sourceUrl, outcome } as FailedFetch);
    return;
  }

  const data = outcome.value;

  // Sniff the document kind
  if (Array.isArray(data)) {
    // Profile list document: array of base URLs
    useStore.updateSource(sourceUrl, { kind: "list" });
    await ingestList(sourceUrl, data as string[]);
  } else if (
    typeof data === "object" &&
    data !== null &&
    "cards" in data
  ) {
    // Collection bundle
    useStore.updateSource(sourceUrl, { kind: "registry" });
    await ingestRegistry(sourceUrl, data as CollectionBundleShape);
  } else if (
    typeof data === "object" &&
    data !== null &&
    ("hasPart" in data || "subjectOf" in data)
  ) {
    // Single profile document (schema:Person with a hasPart/subjectOf manifest)
    useStore.updateSource(sourceUrl, { kind: "profile" });
    await ingestSingleManifest(sourceUrl, data as ManifestShape);
  } else {
    useStore.updateSource(sourceUrl, {
      status: "error",
      error: "Unrecognized document format",
    });
    return;
  }
}

// ---------------------------------------------------------------------------
// Internal shapes (minimal, for sniffing)
// ---------------------------------------------------------------------------

interface CollectionBundleShape {
  cards: Array<{
    slug: string;
    rid?: string | null;
    name: string;
    level?: string;
    affiliation?: string | null;
    field?: string | null;
    paper_count?: number;
    summary_count?: number;
    fulltext_pct?: number;
    base: string;
  }>;
  backend_spec?: string | null;
  dim?: number | null;
  artifacts?: Array<{ rel: string; href: string }>;
  [key: string]: unknown;
}

interface EmbeddingIndexShape {
  backend_spec: string;
  dim: number;
  count: number;
  rows: (string | number)[];
  probe?: { text: string; vector: number[] };
}

interface ManifestShape {
  "@id"?: string;
  name: string;
  rid?: string;
  level?: string;
  hasPart?: Array<{ role?: string; contentUrl: string }>;
  subjectOf?: Array<{ role?: string; contentUrl: string }>;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Ingestion strategies
// ---------------------------------------------------------------------------

/**
 * Registry-bundle fast path: cards render immediately, manifests are fetched
 * lazily when a profile page is opened.
 */
async function ingestRegistry(
  sourceUrl: string,
  bundle: CollectionBundleShape,
): Promise<void> {
  const cards: ProfileCard[] = bundle.cards.map((c) => ({
    slug: c.slug,
    rid: c.rid ?? null,
    name: c.name,
    level: c.level ?? "full",
    affiliation: c.affiliation ?? null,
    field: c.field ?? null,
    paperCount: c.paper_count ?? 0,
    summaryCount: c.summary_count ?? 0,
    fulltextPct: c.fulltext_pct ?? 0,
    base: new URL(c.base, sourceUrl).href,
    sourceUrl,
    backendSpec: bundle.backend_spec ?? null,
  }));

  useStore.addCards(cards);
  useStore.updateSource(sourceUrl, {
    status: "ready",
    profileCount: cards.length,
    backendSpec: bundle.backend_spec,
  });

  // Load centroids via the embedding_index artifact for proper blob parsing.
  if (bundle.backend_spec && bundle.artifacts) {
    const indexArtifact = bundle.artifacts.find(
      (a) => a.rel === "embedding_index",
    );
    const centroidArtifact = bundle.artifacts.find(
      (a) => a.rel === "centroids",
    );
    if (indexArtifact && centroidArtifact) {
      const indexUrl = new URL(indexArtifact.href, sourceUrl).href;
      const centroidUrl = new URL(centroidArtifact.href, sourceUrl).href;
      const [indexOutcome, binOutcome] = await Promise.all([
        fetchJson<EmbeddingIndexShape>(indexUrl),
        fetchBinary(centroidUrl),
      ]);
      if (indexOutcome.ok && binOutcome.ok) {
        const idx = indexOutcome.value;
        try {
          const { parseCentroidBlob } = await import("../vec/blob");
          const parsed = parseCentroidBlob(binOutcome.value, idx.count, idx.dim);
          // Map rows (slugs) to card base URLs.
          const slugToBase = new Map(cards.map((c) => [c.slug, c.base]));
          const order = idx.rows.map((slug) => slugToBase.get(String(slug)) ?? String(slug));
          useStore.setCentroids(
            bundle.backend_spec!,
            parsed.vectors,
            idx.dim,
            order,
            idx.probe,
          );
        } catch {
          // Blob parse failure: centroids unavailable but cards still work.
        }
      }
    }
  }
}

/**
 * Plain-list slow path: fan out over base URLs with a concurrency cap,
 * building cards from each manifest. Render progressively.
 *
 * Before fanning out, tries `collection.jsonld` relative to sourceUrl: if the
 * site publishes a collection bundle, one fetch replaces N and the Inventory
 * columns come alive.
 */
async function ingestList(
  sourceUrl: string,
  urls: string[],
): Promise<void> {
  // Try collection bundle first: one fetch replaces N.
  try {
    const registryUrl = new URL("collection.jsonld", sourceUrl).href;
    const registryOutcome = await fetchJson<unknown>(registryUrl);
    if (
      registryOutcome.ok &&
      typeof registryOutcome.value === "object" &&
      registryOutcome.value !== null &&
      "cards" in registryOutcome.value
    ) {
      await ingestRegistry(sourceUrl, registryOutcome.value as CollectionBundleShape);
      return;
    }
  } catch {
    // 404 or other failure: fall through to per-profile fan-out.
  }

  let loaded = 0;
  let failed = 0;

  // Process in batches of CONCURRENCY
  for (let i = 0; i < urls.length; i += CONCURRENCY) {
    const batch = urls.slice(i, i + CONCURRENCY);
    const results = await Promise.allSettled(
      batch.map(async (rawUrl) => {
        try {
          const absoluteUrl = new URL(rawUrl, sourceUrl).href;
          const resolved = await loadManifest(absoluteUrl);
          const card: ProfileCard = {
            slug: resolved.manifest.rid || resolved.base,
            rid: resolved.manifest.rid || null,
            name: resolved.manifest.name,
            level: resolved.manifest.level || "full",
            affiliation: null,
            field: null,
            paperCount: 0,
            summaryCount: 0,
            fulltextPct: 0,
            base: resolved.base,
            sourceUrl,
            backendSpec: null,
          };
          useStore.addCards([card]);
          loaded++;
        } catch {
          failed++;
          const failOutcome: FetchOutcome<unknown> = {
            ok: false,
            kind: "network",
            url: rawUrl,
            detail: `Failed to load manifest from ${rawUrl}`,
          };
          useStore.addFailure({ url: rawUrl, outcome: failOutcome });
        }
      }),
    );

    // Update progress
    useStore.updateSource(sourceUrl, {
      profileCount: loaded,
      status: "loading",
    });
  }

  useStore.updateSource(sourceUrl, {
    status: failed === urls.length ? "error" : "ready",
    profileCount: loaded,
    error:
      failed > 0 ? `${failed} of ${urls.length} profiles failed to load` : undefined,
  });
}

/**
 * Single manifest: add one card.
 */
async function ingestSingleManifest(
  sourceUrl: string,
  data: ManifestShape,
): Promise<void> {
  // Derive the base from the absolute URL we actually fetched, not from the
  // manifest's `@id`: published profiles carry a relative `@id`
  // (e.g. "profiles/doe-jane/"), and `new URL()` throws on a bare
  // relative string. The list path does the same override via loadManifest.
  const base = normalizeBase(sourceUrl);
  const card: ProfileCard = {
    slug: data.rid || base,
    rid: data.rid || null,
    name: data.name,
    level: data.level || "full",
    affiliation: null,
    field: null,
    paperCount: 0,
    summaryCount: 0,
    fulltextPct: 0,
    base,
    sourceUrl,
    backendSpec: null,
  };
  useStore.addCards([card]);
  useStore.updateSource(sourceUrl, {
    status: "ready",
    profileCount: 1,
    kind: "profile",
  });
}
