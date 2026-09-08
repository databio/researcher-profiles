/**
 * Manifest-driven data layer.
 *
 * A published profile is a single `schema:Person` document (profile.jsonld).
 * The document itself carries the profile metadata; every other file (works,
 * persona docs, per-paper summaries, embeddings) is reached through a typed
 * manifest entry resolved by `role` to a relative `contentUrl`, never a
 * guessed path.
 */

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

/**
 * Assemble the full profile detail (metadata + expertise + soul) for a resolved
 * profile. The metadata is the profile.jsonld document itself; the persona documents
 * (`expertise`, `soul`) are separate files listed in `subjectOf`.
 */
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
 * Fetch the paper corpus for a resolved profile (manifest role `works`).
 *
 * The `works` file is a JSON-LD graph (a `Collection` with a `hasPart` array of
 * ScholarlyArticle nodes). `worksGraphToPapers` maps that graph to PaperEntry
 * rows; a shape it cannot read yields an empty list rather than a crash.
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

  return worksGraphToPapers(outcome.value);
}

/**
 * Fetch the per-profile embedding metadata (index + chunk manifest), or null
 * when the profile publishes no embeddings.
 *
 * Reached through the typed manifest entry (`embedding_index`), never a
 * guessed path. A profile without an `embedding_index` entry has no published
 * vectors.
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

/**
 * Fetch a single paper summary by resolving its `paper_summary` manifest entry.
 * Summaries are markdown files, so the raw text is returned.
 */
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
