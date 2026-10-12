/**
 * Conformance validation runner. Accepts a profile base, profile.jsonld, list
 * document, or registry URL and runs the matching check suite.
 */

import type { CheckResult, ValidationRun } from "./types";
import { fetchJson, type FetchOutcome } from "../net/fetchJson";
import { fetchBinary } from "../net/fetchBinary";
import { normalizeBase } from "../model/manifest";
import { profileChecks } from "./profileChecks";
import { listChecks } from "./listChecks";
import { validateAgainstSchema } from "./schemaValidation";

export type OnCheck = (check: CheckResult) => void;

/** Run validation against a URL, streaming each check to `onCheck`. */
export async function runValidation(
  rawUrl: string,
  onCheck: OnCheck,
): Promise<ValidationRun> {
  const startedAt = new Date().toISOString();
  const checks: CheckResult[] = [];

  function emit(check: CheckResult) {
    checks.push(check);
    onCheck(check);
  }

  const outcome = await fetchJson<unknown>(rawUrl);

  if (!outcome.ok) {
    try {
      const base = normalizeBase(rawUrl);
      const manifestUrl = `${base}profile.jsonld`;
      const manifestOutcome = await fetchJson<unknown>(manifestUrl);

      if (!manifestOutcome.ok) {
        emit({
          id: "reachable",
          title: "Reachable",
          severity: "error",
          passed: false,
          message: `Could not fetch ${rawUrl}: ${outcome.detail}`,
          evidence: `Also tried ${manifestUrl}: ${manifestOutcome.detail}`,
        });
        return { target: rawUrl, startedAt, conformanceClass: "invalid", checks };
      }

      return await runProfileChecks(base, manifestOutcome, emit, startedAt);
    } catch {
      emit({
        id: "reachable",
        title: "Reachable",
        severity: "error",
        passed: false,
        message: `Could not fetch ${rawUrl}: ${outcome.detail}`,
      });
      return { target: rawUrl, startedAt, conformanceClass: "invalid", checks };
    }
  }

  const data = outcome.value;

  if (Array.isArray(data)) {
    // Bare-array profile list
    return await runListChecks(rawUrl, data, outcome, emit, startedAt);
  } else if (typeof data === "object" && data !== null) {
    if ("rp:profileList" in data) {
      // rp:profileList envelope
      const rec = data as Record<string, unknown>;
      const profiles = Array.isArray(rec.profiles) ? rec.profiles : [];
      return await runListChecks(rawUrl, profiles, outcome, emit, startedAt);
    }
    if ("cards" in data) {
      // Collection bundle
      return await runRegistryChecks(rawUrl, data as Record<string, unknown>, outcome, emit, startedAt);
    }
    if ("hasPart" in data || "subjectOf" in data) {
      // Single profile document. Base comes from the fetched URL, not the
      // relative `@id` (see ingestSingleManifest).
      const base = normalizeBase(rawUrl);
      return await runProfileChecks(base, outcome, emit, startedAt);
    }
  }

  try {
    const base = normalizeBase(rawUrl);
    const manifestUrl = `${base}profile.jsonld`;
    const manifestOutcome = await fetchJson<unknown>(manifestUrl);
    if (manifestOutcome.ok) {
      return await runProfileChecks(base, manifestOutcome, emit, startedAt);
    }
  } catch {
    // fall through
  }

  emit({
    id: "format",
    title: "Document Format",
    severity: "error",
    passed: false,
    message: "Unrecognized document format. Expected a profile manifest, a profile list (JSON array), or a collection bundle.",
  });

  return { target: rawUrl, startedAt, conformanceClass: "invalid", checks };
}

// ---------------------------------------------------------------------------
// Profile validation
// ---------------------------------------------------------------------------

async function runProfileChecks(
  base: string,
  manifestOutcome: FetchOutcome<unknown>,
  emit: OnCheck,
  startedAt: string,
): Promise<ValidationRun> {
  const checks = await profileChecks(base, manifestOutcome, emit);
  const hasErrors = checks.some((c) => c.severity === "error" && !c.passed);
  const manifest = manifestOutcome.ok ? (manifestOutcome.value as Record<string, unknown>) : null;

  let conformanceClass: ValidationRun["conformanceClass"] = "invalid";
  if (!hasErrors && manifest) {
    conformanceClass = "Base";
    // Check if Searchable
    const entries = [
      ...((manifest.hasPart as Array<{ role?: string }>) || []),
      ...((manifest.subjectOf as Array<{ role?: string }>) || []),
    ];
    const hasEmbeddings = entries.some((e) => e.role === "embedding_index");
    if (hasEmbeddings) {
      conformanceClass = "Searchable";
    }
  }

  return { target: base, startedAt, conformanceClass, checks };
}

// ---------------------------------------------------------------------------
// List validation
// ---------------------------------------------------------------------------

async function runListChecks(
  url: string,
  data: unknown[],
  outcome: FetchOutcome<unknown>,
  emit: OnCheck,
  startedAt: string,
): Promise<ValidationRun> {
  const checks = await listChecks(url, data, emit);
  const hasErrors = checks.some((c) => c.severity === "error" && !c.passed);
  return {
    target: url,
    startedAt,
    conformanceClass: hasErrors ? "invalid" : "Federated",
    checks,
  };
}

// ---------------------------------------------------------------------------
// Registry validation
// ---------------------------------------------------------------------------

async function runRegistryChecks(
  url: string,
  data: Record<string, unknown>,
  outcome: FetchOutcome<unknown>,
  emit: OnCheck,
  startedAt: string,
): Promise<ValidationRun> {
  const checks: CheckResult[] = [];

  const ajvCheck = validateAgainstSchema("collection", data, "Collection Bundle");
  checks.push(ajvCheck);
  emit(ajvCheck);

  const schemaCheck: CheckResult = {
    id: "registry-schema",
    title: "Registry Bundle Schema",
    severity: "error",
    passed: true,
    message: "Collection bundle has required fields.",
  };

  if (!data.cards || !Array.isArray(data.cards)) {
    schemaCheck.passed = false;
    schemaCheck.message = "Collection bundle missing required 'cards' array.";
  }
  if (typeof data.count !== "number") {
    schemaCheck.passed = false;
    schemaCheck.message += " Missing required 'count' field.";
  }

  checks.push(schemaCheck);
  emit(schemaCheck);

  if (Array.isArray(data.cards) && typeof data.count === "number") {
    const countCheck: CheckResult = {
      id: "registry-count",
      title: "Card Count",
      severity: "error",
      passed: data.cards.length === data.count,
      message: data.cards.length === data.count
        ? `Card count matches: ${data.count}`
        : `cards.length (${data.cards.length}) does not match count (${data.count})`,
    };
    checks.push(countCheck);
    emit(countCheck);
  }

  if (data.dim && data.count && data.artifacts) {
    const artifacts = data.artifacts as Array<{ rel: string; href: string }>;
    const centroidArtifact = artifacts.find((a) => a.rel === "centroids");
    if (centroidArtifact) {
      const centroidUrl = new URL(centroidArtifact.href, url).href;
      const binOutcome = await fetchBinary(centroidUrl);
      const expectedBytes = (data.count as number) * (data.dim as number) * 4;
      const centroidCheck: CheckResult = {
        id: "registry-centroids",
        title: "Centroids Blob",
        severity: "error",
        passed: binOutcome.ok && binOutcome.value.byteLength === expectedBytes,
        message: binOutcome.ok
          ? binOutcome.value.byteLength === expectedBytes
            ? `Centroids blob is correct size: ${expectedBytes} bytes (${data.count} x ${data.dim} x 4)`
            : `Centroids blob size mismatch: expected ${expectedBytes}, got ${binOutcome.value.byteLength}`
          : `Failed to fetch centroids: ${binOutcome.detail}`,
        evidence: centroidUrl,
      };
      checks.push(centroidCheck);
      emit(centroidCheck);
    }
  }

  try {
    const topicsUrl = new URL("collection/topics.json", url).href;
    const topicsOutcome = await fetchJson<unknown>(topicsUrl);
    if (topicsOutcome.ok) {
      const topicsCheck = validateAgainstSchema("topic_index", topicsOutcome.value, "Topic Index");
      checks.push(topicsCheck);
      emit(topicsCheck);
    }
  } catch {
    // topics.json is optional.
  }

  // Sample member URLs
  if (Array.isArray(data.cards)) {
    const cards = data.cards as Array<{ base?: string; name?: string }>;
    const sample = cards.slice(0, 3);
    for (const card of sample) {
      if (card.base) {
        const memberCheck: CheckResult = {
          id: `registry-member-${card.base}`,
          title: `Member: ${card.name || card.base}`,
          severity: "warn",
          passed: true,
          message: `Member URL is absolute and uses HTTPS.`,
        };

        try {
          const parsed = new URL(card.base);
          if (parsed.protocol !== "https:") {
            memberCheck.passed = false;
            memberCheck.message = `Member URL uses ${parsed.protocol} instead of https:`;
          }
        } catch {
          memberCheck.passed = false;
          memberCheck.message = `Member URL is not a valid URL: ${card.base}`;
        }

        checks.push(memberCheck);
        emit(memberCheck);
      }
    }
  }

  const hasErrors = checks.some((c) => c.severity === "error" && !c.passed);
  return {
    target: url,
    startedAt,
    conformanceClass: hasErrors ? "invalid" : "Federated",
    checks,
  };
}
