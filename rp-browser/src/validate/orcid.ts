/**
 * ORCID round-trip verification badge.
 *
 * Runs only when the manifest asserts an orcid. Absence of an ORCID is not
 * a finding. Synthetic, historical, and third-party profiles are all valid.
 *
 * Resolution is one-directional: ORCID record -> profile URL. A profile's
 * asserted orcid is never trusted. The validator reports the round-trip as
 * a badge, never a failure.
 */

import type { CheckResult } from "./types";

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

/**
 * Normalize a URL for comparison: lowercase host, trailing slash, ignore
 * protocol differences (http vs https), ignore profile.jsonld suffix.
 */
function normalizeForComparison(url: string): string {
  try {
    const parsed = new URL(url);
    let path = parsed.pathname;
    // Remove trailing profile.jsonld
    if (path.endsWith("/profile.jsonld")) {
      path = path.slice(0, -"profile.jsonld".length);
    }
    // Force trailing slash
    if (!path.endsWith("/")) path += "/";
    return `${parsed.hostname.toLowerCase()}${path}`;
  } catch {
    return url.toLowerCase();
  }
}

/**
 * Check ORCID round-trip verification for a profile.
 *
 * @param orcid - The asserted ORCID from the manifest
 * @param profileBaseUrl - The profile's base URL
 */
export async function checkOrcidRoundTrip(
  orcid: string,
  profileBaseUrl: string,
): Promise<CheckResult> {
  // Validate checksum first
  if (!isValidOrcidChecksum(orcid)) {
    return {
      id: "orcid-roundtrip",
      title: "ORCID Round-Trip",
      severity: "badge",
      passed: false,
      message: `ORCID ${orcid} has an invalid checksum digit. Skipping network verification.`,
    };
  }

  try {
    const res = await fetch(`https://pub.orcid.org/v3.0/${orcid}/record`, {
      headers: { Accept: "application/json" },
    });

    if (!res.ok) {
      return {
        id: "orcid-roundtrip",
        title: "ORCID Round-Trip",
        severity: "badge",
        passed: "indeterminate",
        message: `ORCID API returned ${res.status}. Verification skipped.`,
      };
    }

    const record = await res.json();

    // Extract researcher URLs
    const researcherUrls: string[] = [];
    try {
      const urls =
        record?.person?.["researcher-urls"]?.["researcher-url"] || [];
      for (const entry of urls) {
        const value = entry?.url?.value;
        if (value) researcherUrls.push(value);
      }
    } catch {
      // Malformed response
    }

    const normalizedProfile = normalizeForComparison(profileBaseUrl);
    const found = researcherUrls.some(
      (u) => normalizeForComparison(u) === normalizedProfile,
    );

    if (found) {
      return {
        id: "orcid-roundtrip",
        title: "ORCID Round-Trip",
        severity: "badge",
        passed: true,
        message: "ORCID round-trip verified: the ORCID record links back to this profile.",
        evidence: `ORCID: ${orcid}`,
      };
    } else {
      return {
        id: "orcid-roundtrip",
        title: "ORCID Round-Trip",
        severity: "badge",
        passed: false,
        message:
          "This profile asserts an ORCID, but that ORCID record does not list this URL. " +
          "Resolution is one-directional. An asserted ORCID is never a credential. " +
          "Add this URL to your ORCID record's websites to earn the badge.",
        evidence: `ORCID: ${orcid}\nProfile URLs in ORCID record: ${researcherUrls.join(", ") || "(none)"}`,
        fix:
          "To add your profile URL to your ORCID record:\n" +
          "1. Log into https://orcid.org\n" +
          "2. Go to your profile page\n" +
          "3. In the 'Websites & social links' section, click 'Add link'\n" +
          `4. Add: ${profileBaseUrl}\n` +
          "5. Set visibility to 'Public'\n" +
          "6. Save",
      };
    }
  } catch {
    return {
      id: "orcid-roundtrip",
      title: "ORCID Round-Trip",
      severity: "badge",
      passed: "indeterminate",
      message:
        "Could not reach the ORCID API (CORS, rate limit, or offline). " +
        "ORCID availability must never degrade a profile's conformance verdict.",
    };
  }
}
