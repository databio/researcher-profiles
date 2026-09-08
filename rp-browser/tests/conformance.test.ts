/**
 * Run the explorer validator over the shared conformance corpus.
 *
 * The corpus (`spec/conformance/`) is the single set of fixtures that BOTH
 * validators run over: this file drives the explorer's `runValidation`, and
 * `tests/test_validate.py::TestConformanceCorpus` drives the Python CLI `validate_profile_dir`
 * over the identical directories. Expected verdicts live in `cases.json`.
 *
 * The explorer's checks are pure functions of `fetch` outcomes, so we run them
 * headlessly by pointing a stub `fetch` at the fixture files on disk. A change
 * that makes the explorer disagree with the corpus (accept an invalid profile
 * or reject a valid one) fails here. That keeps the two
 * validators from drifting apart silently.
 */
import { readFileSync, existsSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, extname } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { runValidation } from "../src/validate/runner";

const HERE = dirname(fileURLToPath(import.meta.url));
const CORPUS = join(HERE, "..", "..", "spec", "conformance");

interface CaseExpectation {
  valid: boolean;
  expect_fail?: string[];
}
interface Case {
  dir: string;
  description: string;
  cli: CaseExpectation;
  explorer: CaseExpectation;
}

const cases: Case[] = JSON.parse(
  readFileSync(join(CORPUS, "cases.json"), "utf-8"),
).cases;

// Fixtures are served under this synthetic base. localhost is required because
// normalizeBase() blocks non-localhost http:// URLs.
const BASE = "http://localhost/conformance/";

const CONTENT_TYPES: Record<string, string> = {
  ".jsonld": "application/ld+json",
  ".json": "application/json",
  ".md": "text/markdown",
  ".html": "text/html",
  ".bin": "application/octet-stream",
};

/** Map a fixture URL to its file on disk; 404 when absent or a directory. */
function fixtureFetch(url: string): Response {
  if (!url.startsWith(BASE)) {
    return new Response("not found", { status: 404, statusText: "Not Found" });
  }
  const rel = decodeURIComponent(url.slice(BASE.length));
  const path = join(CORPUS, rel);
  if (!existsSync(path) || statSync(path).isDirectory()) {
    return new Response("not found", { status: 404, statusText: "Not Found" });
  }
  const body = readFileSync(path);
  const ct = CONTENT_TYPES[extname(path)] ?? "application/octet-stream";
  return new Response(body, {
    status: 200,
    statusText: "OK",
    headers: { "Content-Type": ct },
  });
}

const realFetch = globalThis.fetch;

beforeEach(() => {
  globalThis.fetch = ((input: RequestInfo | URL) =>
    Promise.resolve(fixtureFetch(String(input)))) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("conformance corpus (explorer validator)", () => {
  it("has at least one valid and one invalid explorer case", () => {
    expect(cases.some((c) => c.explorer.valid)).toBe(true);
    expect(cases.some((c) => !c.explorer.valid)).toBe(true);
  });

  for (const c of cases) {
    it(`${c.dir} -> explorer ${c.explorer.valid ? "valid" : "invalid"}`, async () => {
      const target = `${BASE}${c.dir}/`;
      const checks: Array<{ id: string; passed: boolean; severity: string }> = [];
      const run = await runValidation(target, (check) =>
        checks.push({ id: check.id, passed: check.passed, severity: check.severity }),
      );

      const isValid = run.conformanceClass !== "invalid";
      expect(
        isValid,
        `${c.dir}: explorer verdict ${run.conformanceClass} (valid=${isValid}), ` +
          `expected valid=${c.explorer.valid}. Failing checks: ` +
          JSON.stringify(checks.filter((x) => !x.passed && x.severity === "error")),
      ).toBe(c.explorer.valid);

      for (const id of c.explorer.expect_fail ?? []) {
        const failed = checks.find((x) => x.id === id && !x.passed);
        expect(failed, `${c.dir}: expected check "${id}" to fail`).toBeTruthy();
      }
    });
  }
});
