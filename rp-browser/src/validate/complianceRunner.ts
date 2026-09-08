/**
 * Compliance validation runner.
 *
 * Accepts a profile base URL (static API) and/or an API root (dynamic API).
 * Runs the matching check suites and streams results via onCheck.
 */

import type { CheckResult } from "./types";
import { staticApiChecks, dynamicApiChecks } from "./complianceChecks";

export type OnCheck = (check: CheckResult) => void;

export interface ComplianceRun {
  profileUrl: string | null;
  apiRoot: string | null;
  startedAt: string;
  checks: CheckResult[];
}

export async function runComplianceValidation(
  profileUrl: string | null,
  apiRoot: string | null,
  onCheck: OnCheck,
): Promise<ComplianceRun> {
  const startedAt = new Date().toISOString();
  const checks: CheckResult[] = [];

  if (profileUrl) {
    const staticChecks = await staticApiChecks(profileUrl, (c) => {
      checks.push(c);
      onCheck(c);
    });
    void staticChecks;
  }

  if (apiRoot) {
    const dynamicChecks = await dynamicApiChecks(apiRoot, (c) => {
      checks.push(c);
      onCheck(c);
    });
    void dynamicChecks;
  }

  return { profileUrl, apiRoot, startedAt, checks };
}
