/** Manifest-driven data layer: every file is reached by manifest `role`. */

import type { ProfileDetail, PaperEntry, ProfileMetadataPayload } from "@rp/ui-lib/types";
import { worksGraphToPapers } from "@rp/ui-lib";
import { linkFor, summaryUrl, type ResolvedProfile } from "./manifest";
import { fetchJson } from "../net/fetchJson";
import { fetchBinary } from "../net/fetchBinary";

/** Per-profile embedding index (embeddings/index.json). */
export interface EmbeddingIndex {
  backend_spec: string;
  dim: number;
  count: number;
  dtype?: string;
  byte_order?: string;
  normalized?: boolean;
  metric?: string;
  row_key?: string;
  [k: string]: unknown;
}

/** One published chunk vector's metadata (no vector, no text). */
export interface EmbeddingChunk {
  source_type: string;
  source_id: string;
  chunk_index: number;
  section?: string | null;
  char_count?: number | null;
}

export interface ProfileEmbeddings {
  index: EmbeddingIndex;
  chunks: EmbeddingChunk[];
}

/** Fetch a text file (markdown/plain) through the CORS-aware binary path. */
async function fetchText(url: string): Promise<string> {
  const outcome = await fetchBinary(url);
  if (!outcome.ok) {
    throw new Error(`Failed to load ${url}: ${outcome.detail}`);
  }
  return new TextDecoder().decode(outcome.value);
}

/** Assemble profile detail: the profile document plus the `expertise` and `soul` files. */
export async function getProfileDetail(
  resolved: ResolvedProfile,
): Promise<ProfileDetail> {
  const m = resolved.manifest;
  const metadata = m as unknown as ProfileMetadataPayload;

  const expertiseLink = linkFor(m, "expertise");
  const soulLink = linkFor(m, "soul");

  const [expertise, soul] = await Promise.all([
    expertiseLink ? fetchText(expertiseLink.href).catch(() => "") : Promise.resolve(""),
    soulLink ? fetchText(soulLink.href).catch(() => "") : Promise.resolve(""),
  ]);

  return {
    metadata,
    expertise,
    soul,
    slug: m.rid ?? resolved.base,
    rid: m.rid ?? null,
  };
}

/**
 * Fetch the paper corpus (manifest role `works`). A works graph of a shape
 * `worksGraphToPapers` cannot read yields an empty list.
 */
export async function getProfilePapers(
  resolved: ResolvedProfile,
): Promise<PaperEntry[]> {
  const worksLink = linkFor(resolved.manifest, "works");
  if (!worksLink) {
    return []; // Works are optional for lite profiles
  }

  const outcome = await fetchJson<unknown>(worksLink.href);
  if (!outcome.ok) {
    throw new Error(
      `Failed to load works from ${worksLink.href}: ${outcome.detail}`,
    );
  }

  // Only the manifest says which papers have a summary.
  return worksGraphToPapers(outcome.value).map((p) =>
    p.summary_available === undefined && p.paper_id
      ? { ...p, summary_available: summaryUrl(resolved.manifest, p.paper_id) !== null }
      : p,
  );
}

/**
 * Fetch the per-profile embedding metadata (index + chunk manifest), or null
 * when the manifest has no `embedding_index` entry.
 */
export async function getProfileEmbeddings(
  resolved: ResolvedProfile,
): Promise<ProfileEmbeddings | null> {
  const indexLink = linkFor(resolved.manifest, "embedding_index");
  if (!indexLink) return null;

  const indexOutcome = await fetchJson<EmbeddingIndex>(indexLink.href);
  if (!indexOutcome.ok) {
    throw new Error(
      `Failed to load embedding index from ${indexLink.href}: ${indexOutcome.detail}`,
    );
  }

  let chunks: EmbeddingChunk[] = [];
  const chunksLink = linkFor(resolved.manifest, "embedding_chunks");
  if (chunksLink) {
    const chunksOutcome = await fetchJson<EmbeddingChunk[]>(chunksLink.href);
    if (chunksOutcome.ok) chunks = chunksOutcome.value;
  }

  return { index: indexOutcome.value, chunks };
}

/** Fetch one paper's summary markdown via its `paper_summary` manifest entry. */
export async function getPaperSummary(
  resolved: ResolvedProfile,
  paperId: string,
): Promise<string> {
  const url = summaryUrl(resolved.manifest, paperId);
  if (!url) {
    throw new Error(`No paper_summary entry for "${paperId}" in manifest`);
  }
  return fetchText(url);
}
