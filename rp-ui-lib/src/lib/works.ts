/**
 * Maps the works graph to PaperEntry[].
 *
 * The `works` manifest role resolves to a JSON-LD graph document: a schema.org
 * `Collection` whose `hasPart` array holds one `ScholarlyArticle`-shaped record
 * per paper (`name`/title, `datePublished`/year, `isPartOf`/journal,
 * `first_author`, `full_text_link`, …). The presentational `PapersList`
 * component, however, expects a flat `PaperEntry[]` (the wire contract this
 * library owns). This module bridges the two.
 *
 * The transform is pure and total: it never throws on malformed or
 * unexpected input and it skips any graph entry that is not paper-shaped (e.g. a
 * centroid/embedding record that slipped into a graph), so an unknown role or a
 * stray node renders nothing rather than crashing the viewer.
 */

import type { PaperEntry } from "../types";

/** Coerce an unknown value to a trimmed string, or null when not stringish. */
function asString(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed.length ? trimmed : null;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  return null;
}

/** Extract a 4-digit year from a schema.org date value ("2025", "2025-05-04"). */
function asYear(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.trunc(value);
  }
  const str = asString(value);
  if (!str) return null;
  const match = str.match(/\d{4}/);
  return match ? Number.parseInt(match[0], 10) : null;
}

/**
 * Resolve the journal / container name from `isPartOf`, which may be a plain
 * string, a `{ name }` object (schema.org Periodical), or absent.
 */
function journalOf(node: Record<string, unknown>): string | null {
  const direct = asString(node.journal);
  if (direct) return direct;
  const isPartOf = node.isPartOf;
  if (typeof isPartOf === "string") return asString(isPartOf);
  if (isPartOf && typeof isPartOf === "object") {
    return asString((isPartOf as Record<string, unknown>).name);
  }
  return null;
}

/** The paper's title, tolerant of schema.org `name`/`headline` and `title`. */
function titleOf(node: Record<string, unknown>): string | null {
  return asString(node.name) ?? asString(node.headline) ?? asString(node.title);
}

/** Derive a stable paper id, falling back to the JSON-LD `@id` (e.g. `#paper/x`). */
function paperIdOf(node: Record<string, unknown>): string | null {
  const explicit = asString(node.paper_id) ?? asString(node.paperId);
  if (explicit) return explicit;
  const id = asString(node["@id"]);
  if (!id) return null;
  // Strip a leading fragment prefix like "#paper/" to recover the bare id.
  const slash = id.lastIndexOf("/");
  return slash >= 0 ? id.slice(slash + 1) : id.replace(/^#/, "");
}

/** First author, tolerant of `first_author` or a string `author`. */
function firstAuthorOf(node: Record<string, unknown>): string | null {
  const explicit = asString(node.first_author);
  if (explicit) return explicit;
  const author = node.author;
  if (typeof author === "string") return asString(author);
  if (Array.isArray(author) && author.length) {
    const head = author[0];
    if (typeof head === "string") return asString(head);
    if (head && typeof head === "object") {
      return asString((head as Record<string, unknown>).name);
    }
  }
  return null;
}

/** Map one graph node to a PaperEntry, or null when it isn't paper-shaped. */
function nodeToPaper(node: unknown): PaperEntry | null {
  if (!node || typeof node !== "object") return null;
  const record = node as Record<string, unknown>;
  const title = titleOf(record);
  // A paper must have a title; skip embedding/centroid or otherwise non-article
  // nodes so an unexpected graph member never breaks rendering.
  if (!title) return null;

  const entry: PaperEntry = { title };

  const paperId = paperIdOf(record);
  if (paperId) entry.paper_id = paperId;

  const year = asYear(record.datePublished ?? record.year);
  if (year !== null) entry.year = year;

  const journal = journalOf(record);
  if (journal) entry.journal = journal;

  const firstAuthor = firstAuthorOf(record);
  if (firstAuthor) entry.first_author = firstAuthor;

  const fullText = asString(record.full_text_link) ?? asString(record.url);
  if (fullText) entry.full_text_link = fullText;

  if (typeof record.summary_available === "boolean") {
    entry.summary_available = record.summary_available;
  }

  return entry;
}

/**
 * Map a resolved `works` graph to `PaperEntry[]`.
 *
 * Accepts any of the shapes a `works` document may take:
 *   - a bare array of paper records (or already-mapped PaperEntry objects);
 *   - a schema.org `Collection` with a `hasPart` array (the canonical form);
 *   - a JSON-LD document with a `@graph` array.
 *
 * Anything else (including `null`, a scalar, or an object without a paper
 * array) yields an empty list. Non-paper nodes within a graph are skipped.
 */
export function worksGraphToPapers(works: unknown): PaperEntry[] {
  if (works == null) return [];

  let nodes: unknown[];
  if (Array.isArray(works)) {
    nodes = works;
  } else if (typeof works === "object") {
    const doc = works as Record<string, unknown>;
    if (Array.isArray(doc.hasPart)) {
      nodes = doc.hasPart;
    } else if (Array.isArray(doc["@graph"])) {
      nodes = doc["@graph"];
    } else {
      return [];
    }
  } else {
    return [];
  }

  const papers: PaperEntry[] = [];
  for (const node of nodes) {
    const paper = nodeToPaper(node);
    if (paper) papers.push(paper);
  }
  return papers;
}
