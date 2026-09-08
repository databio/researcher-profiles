/**
 * Conformance validator types.
 */

export type Severity = "error" | "warn" | "badge" | "info";

export interface CheckResult {
  id: string;
  title: string;
  severity: Severity;
  passed: boolean | "indeterminate";
  message: string;
  evidence?: string;
  fix?: string;
}

export interface ValidationRun {
  target: string;
  startedAt: string;
  conformanceClass: "Base" | "Searchable" | "Federated" | "invalid";
  checks: CheckResult[];
}
