/**
 * The registry-issued ORCID login proof (`orcid_login`) and its badge.
 *
 * A registry that signs people in with ORCID attaches this proof when it
 * serves a profile: "an owner of this profile signed in here with ORCID iD
 * `orcid`". It is computed per request and never stored.
 *
 * Why the origin comparison is the trust rule: the proof is plain JSON, so
 * anyone can paste it into a self-hosted document. Only the issuer's own
 * response counts. The badge passes only when `issuer` has the same origin as
 * the URL that actually served the document (after redirects); a proof served
 * from anywhere else is reported as indeterminate, never as verified.
 *
 * `orcid_roundtrip` (the ORCID record's website list points back) is the
 * self-hosted route and is not checked here.
 *
 * Pure: no network.
 */

import type { CheckResult } from "./types";

const ORCID_RE = /^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$/;

/**
 * Validate an ORCID checksum digit (ISO 7064 Mod 11,2).
 */
export function isValidOrcidChecksum(orcid: string): boolean {
  const digits = orcid.replace(/-/g, "");
  if (digits.length !== 16) return false;

  let total = 0;
  for (let i = 0; i < 15; i++) {
    const digit = parseInt(digits[i], 10);
    if (isNaN(digit)) return false;
    total = (total + digit) * 2;
  }
  const remainder = total % 11;
  const checkDigit = (12 - remainder) % 11;
  const expected = checkDigit === 10 ? "X" : String(checkDigit);
  return digits[15] === expected;
}

function httpOrigin(url: unknown): string | null {
  if (typeof url !== "string") return null;
  try {
    const u = new URL(url);
    if ((u.protocol !== "http:" && u.protocol !== "https:") || !u.host) return null;
    return u.origin;
  } catch {
    return null;
  }
}

/** The first problem with the `orcid_login` proofs, or null when well formed. */
function structuralProblem(
  proofs: Record<string, unknown>[],
  rid: unknown,
): string | null {
  if (proofs.length > 1) {
    return "The profile carries more than one ORCID login proof; a document is served by one registry, so it can carry at most one.";
  }
  const p = proofs[0];
  const orcid = p.orcid;
  if (typeof orcid !== "string" || !orcid) {
    return "The ORCID login proof has no orcid member.";
  }
  if (!ORCID_RE.test(orcid)) {
    return "The ORCID login proof's orcid is not a bare ORCID iD in 0000-0000-0000-0000 form.";
  }
  if (!isValidOrcidChecksum(orcid)) {
    return `The ORCID login proof's orcid (${orcid}) has an invalid check digit.`;
  }
  if (orcid !== rid) {
    return `The ORCID login proof names ORCID iD ${orcid}, but this profile's rid is ${String(rid)}.`;
  }
  if (httpOrigin(p.issuer) === null) {
    return "The ORCID login proof's issuer is not an absolute http(s) URL.";
  }
  const when = p.verifiedAt;
  if (typeof when !== "string" || !when || isNaN(Date.parse(when))) {
    return "The ORCID login proof's verifiedAt is missing or is not a date.";
  }
  return null;
}

/**
 * Checks for the `orcid_login` proof in `manifest`, served from `servedFrom`.
 *
 * No proof returns `[]`: nothing is shown. Otherwise a structural check
 * (`proof-orcid-login`, an error when it fails) and, when that passes, the
 * `orcid-verified` badge.
 */
export function orcidLoginChecks(
  manifest: Record<string, unknown>,
  servedFrom: string,
): CheckResult[] {
  const all = Array.isArray(manifest.proof) ? (manifest.proof as unknown[]) : [];
  const proofs = all.filter(
    (p): p is Record<string, unknown> =>
      typeof p === "object" && p !== null && (p as { kind?: unknown }).kind === "orcid_login",
  );
  if (proofs.length === 0) return [];

  const problem = structuralProblem(proofs, manifest.rid);
  const structural: CheckResult = {
    id: "proof-orcid-login",
    title: "ORCID login proof",
    severity: "error",
    passed: problem === null,
    message: problem ?? "The ORCID login proof is well formed.",
  };
  if (problem !== null) return [structural];

  const p = proofs[0];
  const issuer = String(p.issuer);
  const orcid = String(p.orcid);
  const issuerOrigin = httpOrigin(issuer) as string;
  const servedOrigin = httpOrigin(servedFrom);
  const evidence = `issuer: ${issuer}, orcid: ${orcid}, verifiedAt: ${String(p.verifiedAt)}`;

  const badge: CheckResult =
    servedOrigin !== null && issuerOrigin === servedOrigin
      ? {
          id: "orcid-verified",
          title: "ORCID verified",
          severity: "badge",
          passed: true,
          message: `The registry serving this profile (${servedOrigin}) confirmed that an owner of this profile signed in with ORCID iD ${orcid}.`,
          evidence,
        }
      : {
          id: "orcid-verified",
          title: "ORCID verified",
          severity: "badge",
          passed: "indeterminate",
          message: `This profile carries an ORCID login proof from ${issuerOrigin}, but it was served by ${servedOrigin ?? servedFrom}. Only the registry that serves a profile can vouch for it.`,
          evidence,
        };
  return [structural, badge];
}
