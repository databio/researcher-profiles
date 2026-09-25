/**
 * The registry-issued ORCID login proof: structure and the origin-bound badge.
 */
import { describe, expect, it } from "vitest";

import { orcidLoginChecks } from "../src/validate/orcidLogin";
import type { CheckResult } from "../src/validate/types";

const ORCID = "0000-0002-1825-0097";
const ISSUER = "https://prosopia.databio.org";

function proof(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    kind: "orcid_login",
    issuer: ISSUER,
    orcid: ORCID,
    verifiedAt: "2026-09-01T12:00:00+00:00",
    ...overrides,
  };
}

function manifest(proofs: unknown[]): Record<string, unknown> {
  return { rid: ORCID, name: "Ada", proof: proofs };
}

function byId(checks: CheckResult[], id: string): CheckResult | undefined {
  return checks.find((c) => c.id === id);
}

describe("orcidLoginChecks", () => {
  it("returns nothing when there is no orcid_login proof", () => {
    expect(orcidLoginChecks({ rid: ORCID }, `${ISSUER}/p/ada/`)).toEqual([]);
    expect(
      orcidLoginChecks(manifest([{ kind: "orcid_roundtrip", issuer: "x" }]), `${ISSUER}/`),
    ).toEqual([]);
  });

  it("passes structure and badge when served from the issuer origin", () => {
    const checks = orcidLoginChecks(
      manifest([proof()]),
      `${ISSUER}/api/v1/profiles/ada/profile.jsonld`,
    );
    expect(byId(checks, "proof-orcid-login")?.passed).toBe(true);
    const badge = byId(checks, "orcid-verified");
    expect(badge?.passed).toBe(true);
    expect(badge?.severity).toBe("badge");
    expect(badge?.message).toContain(ORCID);
  });

  it("is indeterminate when served from another origin", () => {
    const checks = orcidLoginChecks(manifest([proof()]), "https://example.org/p/");
    expect(byId(checks, "proof-orcid-login")?.passed).toBe(true);
    const badge = byId(checks, "orcid-verified");
    expect(badge?.passed).toBe("indeterminate");
    expect(badge?.message).toContain("https://example.org");
  });

  it("uses the final URL after a redirect", () => {
    // The caller passes the fetch outcome's finalUrl (on the issuer), not the
    // requested URL (elsewhere).
    const requested = "https://example.org/ada";
    const finalUrl = `${ISSUER}/api/v1/profiles/ada/profile.jsonld`;
    expect(byId(orcidLoginChecks(manifest([proof()]), finalUrl), "orcid-verified")?.passed).toBe(
      true,
    );
    expect(
      byId(orcidLoginChecks(manifest([proof()]), requested), "orcid-verified")?.passed,
    ).toBe("indeterminate");
  });

  const broken: Array<[string, unknown[]]> = [
    ["orcid does not match rid", [proof({ orcid: "0000-0001-5109-3700" })]],
    ["bad checksum", [proof({ orcid: "0000-0002-1825-0098" })]],
    ["orcid as a URL", [proof({ orcid: `https://orcid.org/${ORCID}` })]],
    ["missing verifiedAt", [proof({ verifiedAt: undefined })]],
    ["issuer without scheme", [proof({ issuer: "prosopia.databio.org" })]],
    ["two proofs", [proof(), proof()]],
  ];
  for (const [label, proofs] of broken) {
    it(`fails structure and emits no badge: ${label}`, () => {
      const checks = orcidLoginChecks(manifest(proofs), `${ISSUER}/`);
      expect(byId(checks, "proof-orcid-login")?.passed).toBe(false);
      expect(byId(checks, "proof-orcid-login")?.severity).toBe("error");
      expect(byId(checks, "orcid-verified")).toBeUndefined();
    });
  }

  it("never offers a fix or asks the user to edit their ORCID record", () => {
    const all = [
      ...orcidLoginChecks(manifest([proof()]), `${ISSUER}/`),
      ...orcidLoginChecks(manifest([proof()]), "https://example.org/"),
      ...broken.flatMap(([, p]) => orcidLoginChecks(manifest(p), `${ISSUER}/`)),
    ];
    for (const c of all) {
      expect(c.fix).toBeUndefined();
      expect(c.message).not.toMatch(/ORCID record/i);
      expect(c.message).not.toMatch(/orcid\.org/i);
    }
  });
});
