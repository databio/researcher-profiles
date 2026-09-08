/**
 * Profile list document conformance checks.
 *
 * Entries can be:
 * - A string (profile base URL)
 * - An object with `url` (base URL) and optional enrichment fields
 * - An object with `list` (nested profile list URL)
 */

import type { CheckResult } from "./types";
import { fetchJson } from "../net/fetchJson";
import { validateAgainstSchema } from "./schemaValidation";

type Emit = (check: CheckResult) => void;

/** Extract the profile base URL from a list entry, or null for nested lists. */
function entryUrl(entry: unknown): string | null {
  if (typeof entry === "string") return entry;
  if (typeof entry === "object" && entry !== null) {
    const obj = entry as Record<string, unknown>;
    if (typeof obj.url === "string") return obj.url;
  }
  return null;
}

/** Check whether an entry is a valid list entry (string, {url}, or {list}). */
function isValidEntry(entry: unknown): boolean {
  if (typeof entry === "string") return true;
  if (typeof entry === "object" && entry !== null) {
    const obj = entry as Record<string, unknown>;
    return typeof obj.url === "string" || typeof obj.list === "string";
  }
  return false;
}

export async function listChecks(
  url: string,
  data: unknown[],
  emit: Emit,
): Promise<CheckResult[]> {
  const checks: CheckResult[] = [];

  function add(check: CheckResult) {
    checks.push(check);
    emit(check);
  }

  // JSON Schema validation against published schema
  add(validateAgainstSchema("profile_list", data, "Profile List"));

  // Entry format check: each must be a string, {url}, or {list}
  const invalidEntries = data
    .map((entry, i) => ({ entry, i }))
    .filter(({ entry }) => !isValidEntry(entry));

  add({
    id: "list-schema",
    title: "List Schema",
    severity: "error",
    passed: invalidEntries.length === 0,
    message: invalidEntries.length === 0
      ? `Valid profile list with ${data.length} entries.`
      : `${invalidEntries.length} entries are invalid (must be a URL string, {url}, or {list}).`,
    evidence: invalidEntries.length > 0
      ? invalidEntries.slice(0, 3).map(({ i }) => `[${i}]`).join(", ")
      : undefined,
  });

  if (invalidEntries.length > 0) return checks;

  // Extract profile URLs (skip nested list entries)
  const profileUrls = data.map(entryUrl).filter((u): u is string => u !== null);

  // Check all URLs are absolute and use http(s)
  const invalidUrls = profileUrls.filter((u) => {
    try {
      const parsed = new URL(u);
      return parsed.protocol !== "https:" && parsed.protocol !== "http:";
    } catch {
      return true;
    }
  });

  add({
    id: "list-urls-valid",
    title: "URL Validity",
    severity: "error",
    passed: invalidUrls.length === 0,
    message: invalidUrls.length === 0
      ? "All member URLs are valid absolute URLs."
      : `${invalidUrls.length} URLs are not valid absolute URLs.`,
    evidence: invalidUrls.length > 0 ? invalidUrls.slice(0, 5).join(", ") : undefined,
  });

  // Sample probe: first 10 plus up to 10 random
  if (profileUrls.length === 0) return checks;

  const sampleIndices = new Set<number>();
  for (let i = 0; i < Math.min(10, profileUrls.length); i++) sampleIndices.add(i);
  for (let i = 0; i < 10 && sampleIndices.size < Math.min(20, profileUrls.length); i++) {
    sampleIndices.add(Math.floor(Math.random() * profileUrls.length));
  }

  let reachable = 0;
  let corsBlocked = 0;
  let failed = 0;

  for (const idx of sampleIndices) {
    const memberUrl = profileUrls[idx];
    try {
      const base = memberUrl.endsWith("/") ? memberUrl : memberUrl + "/";
      const manifestUrl = `${base}profile.jsonld`;
      const outcome = await fetchJson<unknown>(manifestUrl);
      if (outcome.ok) {
        reachable++;
      } else if (outcome.kind === "cors") {
        corsBlocked++;
      } else {
        failed++;
      }
    } catch {
      failed++;
    }
  }

  add({
    id: "list-sample-probe",
    title: "Sample Probe",
    severity: corsBlocked > 0 || failed > sampleIndices.size / 2 ? "warn" : "info",
    passed: corsBlocked === 0 && failed <= sampleIndices.size / 4,
    message: `Sampled ${sampleIndices.size} of ${profileUrls.length} profile URLs: ${reachable} reachable, ${corsBlocked} CORS-blocked, ${failed} failed.`,
  });

  return checks;
}
