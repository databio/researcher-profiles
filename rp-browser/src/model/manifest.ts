/**
 * Manifest loading for published researcher profiles.
 *
 * A published profile is a SINGLE JSON-LD document at <base>profile.jsonld, a
 * `schema:Person` record (the ProfileDocument shape) that is authored and
 * published identically. That document is the only path a client may hard-code.
 *
 * Its file manifest is the `hasPart` + `subjectOf` arrays. Each entry is a
 * `ArtifactRef`: a typed link (`role`) to a file inside the profile whose location
 * is a RELATIVE `contentUrl`. A client never guesses a path. It resolves a
 * `contentUrl` against the profile base.
 */

import { fetchJson, type FetchOutcome } from "../net/fetchJson";

// ---------------------------------------------------------------------------
// Types (minimal; the authoritative shape is the profile_jsonld schema)
// ---------------------------------------------------------------------------

/**
 * One manifest entry: a typed link to one artifact (file) inside the profile.
 *
 * `contentUrl` is always a relative path, so a profile stays portable across
 * servers: copying the directory to a different host cannot break a link.
 */
export interface ArtifactRef {
  "@type"?: string;
  /** Machine token identifying the file's purpose. */
  role?: string | null;
  /** Human-facing label. */
  name?: string | null;
  /** MIME type. */
  encodingFormat?: string | null;
  /** Relative path to the file. */
  contentUrl: string;
  /** For per-paper entries (e.g. role `paper_summary`), the paper it belongs to. */
  paperId?: string | null;
  /** Publication scope of the file. */
  visibility?: "public" | "internal" | "restricted" | string | null;
  derivedFrom?: string | null;
  bytes?: number | null;
  sha256?: string | null;
  [k: string]: unknown;
}

/**
 * A published profile document: a `schema:Person` record. Besides the identity
 * and manifest fields listed here it carries arbitrary profile metadata
 * (affiliation, career, interests, …), hence the index signature.
 */
export interface Manifest {
  "@context"?: string;
  "@id": string;
  "@type"?: string;
  name: string;
  rid: string;
  orcid?: string | null;
  level?: string;
  conformsTo?: string[];
  /** Manifest of profile files. Persona docs live in `subjectOf`. */
  hasPart?: ArtifactRef[];
  subjectOf?: ArtifactRef[];
  [k: string]: unknown;
}

export interface ResolvedProfile {
  base: string;
  manifest: Manifest;
  outcome: FetchOutcome<Manifest>;
}

/** Every manifest entry, across both `hasPart` and `subjectOf`. */
export function manifestEntries(manifest: Manifest): ArtifactRef[] {
  return [...(manifest.hasPart ?? []), ...(manifest.subjectOf ?? [])];
}

// ---------------------------------------------------------------------------
// URL normalization
// ---------------------------------------------------------------------------

/**
 * Normalize a profile base URL: force trailing slash, strip a pasted
 * profile.jsonld suffix, reject non-http(s).
 */
export function normalizeBase(url: string): string {
  let u = url.trim();

  // Strip trailing profile.jsonld
  if (u.endsWith("/profile.jsonld")) {
    u = u.slice(0, -"profile.jsonld".length);
  } else if (u.endsWith("/profile.jsonld/")) {
    u = u.slice(0, -"profile.jsonld/".length);
  }

  // Force trailing slash
  if (!u.endsWith("/")) u += "/";

  // Validate protocol
  const parsed = new URL(u);
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new Error(`Only http(s) URLs are supported, got ${parsed.protocol}`);
  }

  // Block http in production (allow localhost in dev)
  if (
    parsed.protocol === "http:" &&
    parsed.hostname !== "localhost" &&
    parsed.hostname !== "127.0.0.1"
  ) {
    // Allow in dev, warn but don't block for now
    console.warn(`Non-HTTPS URL: ${u}. HTTPS is recommended for production.`);
  }

  return u;
}

// ---------------------------------------------------------------------------
// Link resolution
// ---------------------------------------------------------------------------

/**
 * Resolve a typed manifest entry (by `role`) to an absolute URL, its relative
 * `contentUrl` resolved against the manifest's @id (the base URL). No file path
 * is ever constructed by string concatenation outside this function.
 */
export function linkFor(
  manifest: Manifest,
  role: string,
): { href: string; link: ArtifactRef } | null {
  const link = manifestEntries(manifest).find((e) => e.role === role);
  if (!link) return null;
  const resolved = new URL(link.contentUrl, manifest["@id"]).href;
  return { href: resolved, link };
}

/** One single-copy downloadable file of a profile, with a resolved URL. */
export interface ProfileFile {
  role: string;
  href: string;
  mediaType: string;
  bytes?: number | null;
  /**
   * The tier declared on the manifest entry, when it declares one.
   *
   * Carried through so the ordinary browse path can show it: the screen that
   * lists every file a profile contains can then say which of them are
   * public. This is the declared tier, not the effective one: the manifest
   * is tier-invariant (spec section 6) and does not carry the derivation
   * rule's answer. The owner's Publication panel reads the server's
   * `effective` for that; nothing recomputes it here.
   */
  visibility?: string | null;
}

/**
 * Enumerate the single-copy files a profile publishes: the profile document
 * itself (profile.jsonld) plus every manifest entry with a concrete
 * `contentUrl`, resolved against the profile base. Templated entries (a
 * `contentUrl` containing `{…}`) are excluded. They don't resolve to one file.
 */
export function profileFiles(resolved: ResolvedProfile): ProfileFile[] {
  const base = resolved.manifest["@id"];
  const files: ProfileFile[] = [
    {
      role: "profile",
      href: new URL("profile.jsonld", base).href,
      mediaType: "application/ld+json",
    },
  ];
  for (const e of manifestEntries(resolved.manifest)) {
    if (!e.contentUrl || e.contentUrl.includes("{")) continue; // templated / empty
    files.push({
      role: e.role ?? "part",
      href: new URL(e.contentUrl, base).href,
      mediaType: e.encodingFormat ?? "application/octet-stream",
      bytes: e.bytes,
      visibility: e.visibility ?? null,
    });
  }
  // The JSON-LD @context this profile references (an absolute URL). It defines
  // the vocabulary every file is written against, so surface it too.
  const ctx = resolved.manifest["@context"];
  if (ctx) {
    files.push({
      role: "context",
      href: new URL(ctx, base).href,
      mediaType: "application/ld+json",
    });
  }
  return files;
}

/**
 * Resolve the summary file for a paper. Per-paper summaries are individual
 * manifest entries (role `paper_summary`) keyed by `paperId`.
 */
export function summaryUrl(
  manifest: Manifest,
  paperId: string,
): string | null {
  const link = manifestEntries(manifest).find(
    (e) => e.role === "paper_summary" && e.paperId === paperId,
  );
  if (!link) return null;
  return new URL(link.contentUrl, manifest["@id"]).href;
}

// ---------------------------------------------------------------------------
// Manifest loading
// ---------------------------------------------------------------------------

const manifestCache = new Map<string, ResolvedProfile>();

/**
 * Load a profile document from the given base URL. Returns a ResolvedProfile
 * with the parsed document and fetch outcome. Caches by normalized base URL.
 */
export async function loadManifest(
  rawUrl: string,
): Promise<ResolvedProfile> {
  const base = normalizeBase(rawUrl);

  const cached = manifestCache.get(base);
  if (cached) return cached;

  const url = `${base}profile.jsonld`;
  const outcome = await fetchJson<Manifest>(url);

  if (!outcome.ok) {
    throw new Error(`Failed to load manifest from ${url}: ${outcome.detail}`);
  }

  const manifest = outcome.value;
  // @id must be the absolute base URL for contentUrl resolution
  manifest["@id"] = base;

  const resolved: ResolvedProfile = {
    base,
    manifest,
    outcome,
  };

  manifestCache.set(base, resolved);
  return resolved;
}

/** Clear the manifest cache (used by refresh). */
export function clearManifestCache(): void {
  manifestCache.clear();
}
