/**
 * Typed research interests (`rp:researchInterests`): parsing, precedence and
 * grouping, ported from rp-sdk's `schema/_parts.py` so a viewer and an editor
 * agree with the backend about which entry counts and where it belongs.
 * Pure and total: never throws on malformed input.
 */

import type { InterestConcept, ResearchInterest } from "../types";

/** What a human reads: the label of a text-only concept, else `display`. */
export const conceptText = (c: InterestConcept): string =>
  (c.unmapped ? c.label : c.display) ?? "";

/** Identity for precedence: `system|code`, or the lowercased label. */
export const conceptKey = (c: InterestConcept): string =>
  c.unmapped ? "text|" + (c.label ?? "").toLowerCase() : `${c.system}|${c.code}`;

/** The vocabulary a concept comes from, for a chip; `null` for a text-only one. */
export function vocabularyOf(c: InterestConcept): string | null {
  if (c.unmapped) return null;
  const s = c.system ?? "";
  if (s.includes("openalex")) return "OpenAlex";
  if (s.includes("mesh")) return "MeSH";
  return s || null;
}

/** Epoch ms of `assertedAt`; a value with no zone is UTC, as in the backend. */
function assertedMs(e: ResearchInterest): number {
  const at = e.assertedAt ?? "";
  const zoned = /([zZ]|[+-]\d\d:?\d\d)$/.test(at) ? at : at + "Z";
  const ms = Date.parse(zoned);
  return Number.isNaN(ms) ? 0 : ms;
}

/** Whether `a` outranks `b`: declared beats the rest, then newer beats older. */
function outranks(a: ResearchInterest, b: ResearchInterest): boolean {
  const da = a.method === "declared" ? 1 : 0;
  const db = b.method === "declared" ? 1 : 0;
  if (da !== db) return da > db;
  return assertedMs(a) > assertedMs(b);
}

/**
 * The one entry per concept that counts, in first-appearance order.
 * Declared beats inferred or imported; among equals the newest `assertedAt` wins.
 */
export function effectiveInterests(es: ResearchInterest[]): ResearchInterest[] {
  const best = new Map<string, ResearchInterest>();
  for (const e of es) {
    const k = conceptKey(e.concept);
    const cur = best.get(k);
    if (cur === undefined || outranks(e, cur)) best.set(k, e);
  }
  return [...best.values()];
}

export type InterestBand = "core" | "interested" | "neutral" | "lower" | "never" | "unrated";

/** Where a weight sits on the scale. No weight is unknown, never neutral. */
export function bandOf(w: number | null | undefined): InterestBand {
  if (w == null) return "unrated";
  if (w === 1) return "core";
  if (w > 0) return "interested";
  if (w === 0) return "neutral";
  if (w === -1) return "never";
  return "lower";
}

/** The groups in display order, with their headings. */
export const INTEREST_BANDS: readonly { band: InterestBand; title: string }[] = [
  { band: "core", title: "Core" },
  { band: "interested", title: "Interested" },
  { band: "neutral", title: "Neutral" },
  { band: "lower", title: "Rank lower" },
  { band: "never", title: "Never" },
  { band: "unrated", title: "Unrated (suggested from papers)" },
];

/**
 * Effective entries grouped by band, in `INTEREST_BANDS` order, empty groups
 * dropped. Unrated entries are sorted by the share of papers carrying them.
 */
export function groupInterests(
  entries: ResearchInterest[],
): { band: InterestBand; title: string; entries: ResearchInterest[] }[] {
  const by = new Map<InterestBand, ResearchInterest[]>();
  for (const e of effectiveInterests(entries)) {
    const b = bandOf(e.weight);
    by.set(b, [...(by.get(b) ?? []), e]);
  }
  const unrated = by.get("unrated");
  if (unrated) {
    unrated.sort((a, b) => (b.evidence?.share ?? -1) - (a.evidence?.share ?? -1));
  }
  return INTEREST_BANDS.filter(({ band }) => by.has(band)).map(({ band, title }) => ({
    band,
    title,
    entries: by.get(band)!,
  }));
}

/** Who said so, in plain words. */
export function sourceOf(e: ResearchInterest): string {
  if (e.method === "declared") return "you said";
  if (e.method === "imported") return "imported";
  if (e.generator.startsWith("openalex-topics")) return "from papers";
  if (e.generator.startsWith("llm")) return "AI guess";
  return "inferred";
}

function isConcept(v: unknown): v is InterestConcept {
  if (!v || typeof v !== "object") return false;
  const c = v as InterestConcept;
  return c.unmapped
    ? typeof c.label === "string" && c.label.trim() !== ""
    : typeof c.system === "string" && typeof c.code === "string" && typeof c.display === "string";
}

/** Well-formed typed entries from an untrusted value; malformed ones are dropped. */
export function parseResearchInterests(v: unknown): ResearchInterest[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is ResearchInterest => {
    if (!x || typeof x !== "object") return false;
    const e = x as ResearchInterest;
    return (
      isConcept(e.concept) &&
      (e.weight == null || (typeof e.weight === "number" && e.weight >= -1 && e.weight <= 1)) &&
      (e.method === "declared" || e.method === "inferred" || e.method === "imported") &&
      typeof e.generator === "string" &&
      typeof e.assertedAt === "string"
    );
  });
}
