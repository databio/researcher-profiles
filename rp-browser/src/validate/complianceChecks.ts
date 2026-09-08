/**
 * API compliance checks: client-side HTTP probes against the RP static and
 * dynamic API specs. Mirrors the Python conformance suite in spec/conformance/.
 */

import type { CheckResult } from "./types";
import { fetchJson } from "../net/fetchJson";

type Emit = (check: CheckResult) => void;

// ---------------------------------------------------------------------------
// Static API checks (given a profile base URL)
// ---------------------------------------------------------------------------

export async function staticApiChecks(
  profileUrl: string,
  emit: Emit,
): Promise<CheckResult[]> {
  const base = profileUrl.replace(/\/+$/, "");
  const checks: CheckResult[] = [];

  function add(c: CheckResult) {
    checks.push(c);
    emit(c);
  }

  // manifest exists
  const manifestUrl = `${base}/profile.jsonld`;
  const outcome = await fetchJson<Record<string, unknown>>(manifestUrl);
  add({
    id: "static-manifest-exists",
    title: "Manifest Exists",
    severity: "error",
    passed: outcome.ok,
    message: outcome.ok
      ? "profile.jsonld returned 200."
      : `profile.jsonld not reachable: ${outcome.ok ? "" : outcome.detail}`,
    evidence: manifestUrl,
  });
  if (!outcome.ok) return checks;

  // valid JSON
  const data = outcome.value;
  add({
    id: "static-manifest-json",
    title: "Valid JSON",
    severity: "error",
    passed: typeof data === "object" && data !== null,
    message:
      typeof data === "object" && data !== null
        ? "profile.jsonld is a valid JSON object."
        : "profile.jsonld root is not a JSON object.",
  });

  // content-type
  const ct = outcome.contentType?.split(";")[0].trim().toLowerCase() ?? "";
  add({
    id: "static-content-type",
    title: "Content-Type",
    severity: "warn",
    passed: ct === "application/ld+json" || ct === "application/json",
    message:
      ct === "application/ld+json"
        ? "Content-Type is application/ld+json."
        : ct === "application/json"
          ? "Content-Type is application/json (acceptable; application/ld+json preferred)."
          : `Content-Type is "${outcome.contentType || "missing"}".`,
  });

  // CORS (if we got here, fetch succeeded, so CORS passed)
  add({
    id: "static-cors",
    title: "CORS",
    severity: "error",
    passed: true,
    message: "Cross-origin access permitted (fetch succeeded).",
  });

  // HEAD method
  let headOk = false;
  try {
    const res = await fetch(manifestUrl, { method: "HEAD", mode: "cors" });
    headOk = res.ok;
  } catch {
    // CORS or network
  }
  add({
    id: "static-head",
    title: "HEAD Method",
    severity: "error",
    passed: headOk,
    message: headOk
      ? "HEAD request on profile.jsonld succeeded."
      : "HEAD request on profile.jsonld failed.",
  });

  // ETag: we can't read ETag cross-origin unless exposed, so this is
  // best-effort; note as indeterminate if unreadable.
  // Note: ETag is not a CORS-safelisted header, so cross-origin reads may
  // fail. We check but don't hard-fail.
  let hasEtag: boolean | "indeterminate" = "indeterminate";
  try {
    const res = await fetch(manifestUrl, { mode: "cors" });
    const etag = res.headers.get("ETag");
    hasEtag = etag !== null;
  } catch {
    // can't check
  }
  add({
    id: "static-etag",
    title: "ETag Header",
    severity: "warn",
    passed: hasEtag === true ? true : hasEtag === false ? false : "indeterminate",
    message:
      hasEtag === true
        ? "ETag header is present."
        : hasEtag === false
          ? "ETag header is missing (SHOULD be present per spec)."
          : "ETag header could not be read (may require Access-Control-Expose-Headers).",
  });

  // Last-Modified: same cross-origin caveat
  let hasLastMod: boolean | "indeterminate" = "indeterminate";
  try {
    const res = await fetch(manifestUrl, { mode: "cors" });
    const lm = res.headers.get("Last-Modified");
    hasLastMod = lm !== null;
  } catch {
    // can't check
  }
  add({
    id: "static-last-modified",
    title: "Last-Modified Header",
    severity: "warn",
    passed: hasLastMod === true ? true : hasLastMod === false ? false : "indeterminate",
    message:
      hasLastMod === true
        ? "Last-Modified header is present."
        : hasLastMod === false
          ? "Last-Modified header is missing (SHOULD be present per spec)."
          : "Last-Modified header could not be read (may require Access-Control-Expose-Headers).",
  });

  // 404 for nonexistent artifact
  const bogusUrl = `${base}/nonexistent-compliance-probe.jsonld`;
  const bogusOutcome = await fetchJson<unknown>(bogusUrl);
  const got404 = !bogusOutcome.ok && bogusOutcome.status === 404;
  const gotHtml200 =
    bogusOutcome.ok ||
    (!bogusOutcome.ok &&
      bogusOutcome.status === 200 &&
      bogusOutcome.contentType?.includes("text/html"));
  add({
    id: "static-404",
    title: "404 for Missing Artifact",
    severity: "error",
    passed: got404,
    message: got404
      ? "Nonexistent artifact correctly returns 404."
      : gotHtml200
        ? "Nonexistent artifact returned 200 (likely an SPA catch-all). This is a conformance violation."
        : `Nonexistent artifact returned ${bogusOutcome.ok ? "200" : String((bogusOutcome as { status?: number }).status ?? "unknown")}.`,
    evidence: bogusUrl,
  });

  // HTML landing page
  const indexUrl = `${base}/index.html`;
  const indexOutcome = await fetchJson<unknown>(indexUrl);
  if (indexOutcome.ok && typeof indexOutcome.value === "string") {
    // It returned JSON-parseable string? That's odd. Skip.
  } else {
    // Try fetching as text to parse HTML
    try {
      const res = await fetch(indexUrl, { mode: "cors" });
      if (res.ok) {
        const html = await res.text();
        const hasAlternate = /<link[^>]*rel=["']alternate["'][^>]*type=["']application\/ld\+json["']/i.test(html);
        const hasJsonLdScript = /<script[^>]*type=["']application\/ld\+json["']/i.test(html);
        const hasCanonical = /<link[^>]*rel=["']canonical["']/i.test(html);

        add({
          id: "static-html-alternate",
          title: "HTML <link alternate>",
          severity: "error",
          passed: hasAlternate,
          message: hasAlternate
            ? 'index.html has <link rel="alternate" type="application/ld+json">.'
            : 'index.html missing <link rel="alternate" type="application/ld+json">.',
        });
        add({
          id: "static-html-jsonld",
          title: "HTML JSON-LD Script",
          severity: "error",
          passed: hasJsonLdScript,
          message: hasJsonLdScript
            ? 'index.html has <script type="application/ld+json">.'
            : 'index.html missing <script type="application/ld+json">.',
        });
        add({
          id: "static-html-canonical",
          title: "HTML Canonical Link",
          severity: "error",
          passed: hasCanonical,
          message: hasCanonical
            ? 'index.html has <link rel="canonical">.'
            : 'index.html missing <link rel="canonical">.',
        });
      }
      // If 404, landing page is optional: skip silently
    } catch {
      // Can't fetch, skip
    }
  }

  return checks;
}

// ---------------------------------------------------------------------------
// Dynamic API checks (given an api_root URL)
// ---------------------------------------------------------------------------

export async function dynamicApiChecks(
  apiRoot: string,
  emit: Emit,
): Promise<CheckResult[]> {
  const base = apiRoot.replace(/\/+$/, "");
  const checks: CheckResult[] = [];

  function add(c: CheckResult) {
    checks.push(c);
    emit(c);
  }

  // Health: lives at server root, strip /api/vN suffix
  const serverRoot = base.replace(/\/api\/v\d+$/, "");
  const healthOutcome = await fetchJson<Record<string, unknown>>(
    `${serverRoot}/health`,
  );
  add({
    id: "dynamic-health",
    title: "Health Endpoint",
    severity: "error",
    passed:
      healthOutcome.ok &&
      healthOutcome.value.status === "ok" &&
      "store" in healthOutcome.value &&
      "profile_count" in healthOutcome.value,
    message: healthOutcome.ok
      ? healthOutcome.value.status === "ok"
        ? `GET /health returns status "ok", ${healthOutcome.value.profile_count} profiles.`
        : `GET /health status is "${healthOutcome.value.status}", expected "ok".`
      : `GET /health failed: ${healthOutcome.ok ? "" : healthOutcome.detail}`,
  });

  // GET /profiles: rp:profileList format
  const profilesOutcome = await fetchJson<Record<string, unknown>>(
    `${base}/profiles`,
  );
  const hasList =
    profilesOutcome.ok && "rp:profileList" in profilesOutcome.value;
  const hasProfiles =
    profilesOutcome.ok &&
    Array.isArray(profilesOutcome.value.profiles);
  add({
    id: "dynamic-profiles-list",
    title: "Profile Listing Format",
    severity: "error",
    passed: hasList && hasProfiles,
    message: hasList
      ? `GET /profiles returns rp:profileList v${profilesOutcome.ok ? profilesOutcome.value["rp:profileList"] : "?"}.`
      : profilesOutcome.ok
        ? "GET /profiles is missing rp:profileList envelope (got bare data)."
        : `GET /profiles failed: ${profilesOutcome.ok ? "" : profilesOutcome.detail}`,
  });

  // CORS on /profiles
  add({
    id: "dynamic-profiles-cors",
    title: "CORS on /profiles",
    severity: "error",
    passed: profilesOutcome.ok,
    message: profilesOutcome.ok
      ? "Cross-origin access to GET /profiles succeeded."
      : profilesOutcome.kind === "cors"
        ? "GET /profiles blocked by CORS."
        : `GET /profiles failed: ${profilesOutcome.detail}`,
  });

  // Profile list entry validation
  if (profilesOutcome.ok && Array.isArray(profilesOutcome.value.profiles)) {
    const entries = profilesOutcome.value.profiles as unknown[];
    const sample = entries.slice(0, 10);
    const badEntries = sample.filter((e, i) => {
      if (typeof e === "string") return false;
      if (typeof e === "object" && e !== null) {
        return !("url" in e) && !("list" in e);
      }
      return true;
    });
    add({
      id: "dynamic-profiles-entries",
      title: "Profile List Entries",
      severity: "error",
      passed: badEntries.length === 0,
      message:
        badEntries.length === 0
          ? `Sampled ${sample.length} entries. All have url or list field.`
          : `${badEntries.length} of ${sample.length} sampled entries lack url or list field.`,
    });
  }

  // Discover a slug for further checks
  let slug: string | null = null;
  if (profilesOutcome.ok) {
    const profiles =
      Array.isArray(profilesOutcome.value.profiles)
        ? (profilesOutcome.value.profiles as unknown[])
        : Array.isArray(profilesOutcome.value)
          ? (profilesOutcome.value as unknown[])
          : [];
    for (const entry of profiles) {
      if (typeof entry === "object" && entry !== null && "slug" in entry) {
        slug = (entry as Record<string, unknown>).slug as string;
        break;
      }
    }
  }

  if (slug) {
    // Profile detail
    const detailOutcome = await fetchJson<Record<string, unknown>>(
      `${base}/profiles/${slug}`,
    );
    add({
      id: "dynamic-profile-detail",
      title: "Profile Detail",
      severity: "error",
      passed:
        detailOutcome.ok &&
        "slug" in detailOutcome.value &&
        "metadata" in detailOutcome.value,
      message: detailOutcome.ok
        ? "slug" in detailOutcome.value && "metadata" in detailOutcome.value
          ? `GET /profiles/${slug} returns slug and metadata.`
          : `GET /profiles/${slug} missing required fields.`
        : `GET /profiles/${slug} failed: ${detailOutcome.ok ? "" : detailOutcome.detail}`,
    });

    // Papers endpoint
    const papersOutcome = await fetchJson<unknown>(
      `${base}/profiles/${slug}/papers`,
    );
    add({
      id: "dynamic-papers",
      title: "Papers Endpoint",
      severity: "error",
      passed: papersOutcome.ok && Array.isArray(papersOutcome.value),
      message: papersOutcome.ok
        ? Array.isArray(papersOutcome.value)
          ? `GET /profiles/${slug}/papers returns array with ${(papersOutcome.value as unknown[]).length} entries.`
          : "Papers endpoint did not return an array."
        : `GET /profiles/${slug}/papers failed: ${papersOutcome.ok ? "" : papersOutcome.detail}`,
    });

    // Content endpoint: manifest
    const contentOutcome = await fetchJson<Record<string, unknown>>(
      `${base}/profiles/${slug}/content/profile.jsonld`,
    );
    const contentCt =
      contentOutcome.ok
        ? contentOutcome.contentType?.split(";")[0].trim().toLowerCase()
        : null;
    add({
      id: "dynamic-content-manifest",
      title: "Content Endpoint",
      severity: "error",
      passed:
        contentOutcome.ok &&
        (contentCt === "application/ld+json" || contentCt === "application/json"),
      message: contentOutcome.ok
        ? `Content endpoint serves profile.jsonld with ${contentCt}.`
        : `Content endpoint failed: ${contentOutcome.ok ? "" : contentOutcome.detail}`,
    });

    // Content 404
    const content404 = await fetchJson<unknown>(
      `${base}/profiles/${slug}/content/nonexistent-file.jsonld`,
    );
    add({
      id: "dynamic-content-404",
      title: "Content 404",
      severity: "error",
      passed: !content404.ok && content404.status === 404,
      message:
        !content404.ok && content404.status === 404
          ? "Content endpoint returns 404 for nonexistent artifact."
          : `Content endpoint returned ${content404.ok ? "200" : String((content404 as { status?: number }).status ?? "?")} for nonexistent artifact.`,
    });
  } else {
    add({
      id: "dynamic-no-profiles",
      title: "Profile Probes Skipped",
      severity: "info",
      passed: "indeterminate",
      message:
        "No visible profiles with a slug, skipping detail, papers, and content checks.",
    });
  }

  // Error format
  const errOutcome = await fetchJson<Record<string, unknown>>(
    `${base}/profiles/nonexistent-slug-compliance-probe`,
  );
  add({
    id: "dynamic-error-format",
    title: "Error Format",
    severity: "error",
    passed:
      !errOutcome.ok &&
      errOutcome.status === 404,
    message:
      !errOutcome.ok && errOutcome.status === 404
        ? "Nonexistent profile returns 404."
        : `Nonexistent profile returned ${errOutcome.ok ? "200" : String(errOutcome.status ?? "?")}.`,
  });

  // collection.json
  const regOutcome = await fetchJson<Record<string, unknown>>(
    `${base}/collection.json`,
  );
  add({
    id: "dynamic-collection",
    title: "Collection Bundle",
    severity: "warn",
    passed:
      regOutcome.ok &&
      "cards" in regOutcome.value &&
      "count" in regOutcome.value,
    message: regOutcome.ok
      ? "cards" in regOutcome.value
        ? `GET /collection.json returns ${regOutcome.value.count} cards.`
        : "GET /collection.json missing cards field."
      : `GET /collection.json failed: ${regOutcome.ok ? "" : regOutcome.detail}`,
  });

  return checks;
}
