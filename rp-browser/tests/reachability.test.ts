/**
 * Reachability guardrail: every src/**\/*.ts(x) file must be reachable from
 * the app entry point (src/main.tsx) or from a test file.
 *
 * This catches orphaned modules: code that was written but never wired up.
 * A month of format churn skipped the search modules because nothing imported
 * them, so nothing broke and no signal fired. This test IS the signal.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "fs";
import { resolve, relative, extname } from "path";

const SRC_DIR = resolve(__dirname, "../src");
const TESTS_DIR = resolve(__dirname, "../tests");

// Files that are genuine entry points with no importer inside src/
const ALLOWLIST = new Set([
  "src/vite-env.d.ts",
]);

function collectFiles(dir: string, ext: string[]): string[] {
  const results: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = resolve(dir, entry.name);
    if (entry.isDirectory()) {
      results.push(...collectFiles(full, ext));
    } else if (ext.includes(extname(entry.name))) {
      results.push(full);
    }
  }
  return results;
}

function extractImports(filePath: string): string[] {
  const content = readFileSync(filePath, "utf-8");
  const imports: string[] = [];
  // Static imports: import ... from "..."
  for (const m of content.matchAll(/from\s+["']([^"']+)["']/g)) {
    imports.push(m[1]);
  }
  // Dynamic imports: import("...")
  for (const m of content.matchAll(/import\(\s*["']([^"']+)["']\s*\)/g)) {
    imports.push(m[1]);
  }
  // Dynamic imports with vite-ignore: import(/* @vite-ignore */ "...")
  for (const m of content.matchAll(/import\(\s*\/\*[^*]*\*\/\s*["']([^"']+)["']\s*\)/g)) {
    imports.push(m[1]);
  }
  return imports;
}

function resolveImport(from: string, spec: string): string | null {
  if (!spec.startsWith(".") && !spec.startsWith("/")) return null; // package or aliased import (e.g. @rp/ui-lib, @rp/schemas)

  const dir = resolve(from, "..");
  const candidates = [
    resolve(dir, spec),
    resolve(dir, spec + ".ts"),
    resolve(dir, spec + ".tsx"),
    resolve(dir, spec + "/index.ts"),
    resolve(dir, spec + "/index.tsx"),
  ];

  for (const c of candidates) {
    try {
      statSync(c);
      return c;
    } catch {
      // not found
    }
  }
  return null;
}

function walkReachable(entryPoints: string[]): Set<string> {
  const visited = new Set<string>();
  const queue = [...entryPoints];

  while (queue.length > 0) {
    const file = queue.pop()!;
    if (visited.has(file)) continue;
    visited.add(file);

    let imports: string[];
    try {
      imports = extractImports(file);
    } catch {
      continue;
    }

    for (const spec of imports) {
      const resolved = resolveImport(file, spec);
      if (resolved && !visited.has(resolved)) {
        queue.push(resolved);
      }
    }
  }

  return visited;
}

describe("reachability", () => {
  it("every src/ file is reachable from main.tsx or tests", () => {
    const srcFiles = collectFiles(SRC_DIR, [".ts", ".tsx"]);
    const testFiles = collectFiles(TESTS_DIR, [".ts", ".tsx"]);

    const entryPoints = [
      resolve(SRC_DIR, "main.tsx"),
      ...testFiles,
    ];

    const reached = walkReachable(entryPoints);
    const projectRoot = resolve(__dirname, "..");

    const unreached = srcFiles
      .map((f) => relative(projectRoot, f))
      .filter((rel) => !ALLOWLIST.has(rel))
      .filter((rel) => !reached.has(resolve(projectRoot, rel)));

    expect(unreached, `Orphaned source files found. Wire them up or add to ALLOWLIST:\n${unreached.join("\n")}`).toEqual([]);
  });
});
