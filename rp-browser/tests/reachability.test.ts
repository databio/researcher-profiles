/**
 * Reachability guardrail: every src/**\/*.ts(x) file must be reachable from
 * the app entry point src/main.tsx (the only <script> in index.html).
 *
 * Tests are deliberately NOT roots: a module that only a test imports is still
 * dead to users. Anything legitimately loaded another way goes in ALLOWLIST
 * with a reason.
 *
 * This catches orphaned modules: code that was written but never wired up.
 * A month of format churn skipped the search modules because nothing imported
 * them, so nothing broke and no signal fired. This test IS the signal.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync, existsSync } from "fs";
import { resolve, relative, extname } from "path";

const PROJECT_ROOT = resolve(__dirname, "..");
const SRC_DIR = resolve(PROJECT_ROOT, "src");

// Real app entry points. index.html loads only src/main.tsx; vite.config.ts
// declares no extra inputs or workers. Add one here if that changes.
const ENTRY_POINTS = ["src/main.tsx"];

// src/ files the app loads some way other than an import from main.tsx, or
// that are intentionally not loaded at runtime. Every entry needs a reason.
// Do NOT add a module here just because only a test imports it: wire it up
// or delete it.
const ALLOWLIST: Record<string, string> = {
  "src/vite-env.d.ts": "ambient type declarations, read by tsc only",
};

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

function parseImports(content: string): string[] {
  const imports: string[] = [];
  // Static imports and re-exports: import ... from "..." / export ... from "..."
  for (const m of content.matchAll(/from\s+["']([^"']+)["']/g)) {
    imports.push(m[1]);
  }
  // Side-effect imports: import "./x";
  for (const m of content.matchAll(/^\s*import\s+["']([^"']+)["']/gm)) {
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

function extractImports(filePath: string): string[] {
  return parseImports(readFileSync(filePath, "utf-8"));
}

function isFile(p: string): boolean {
  try {
    return statSync(p).isFile();
  } catch {
    return false;
  }
}

function resolveImport(from: string, spec: string): string | null {
  // Non-relative specs are packages or aliases. Safe to skip: every alias in
  // vite.config.ts / tsconfig.json points outside src/ (@rp/ui-lib ->
  // ../rp-ui-lib, @rp/schemas -> ../rp-sdk/schemas). If an alias into src/ is
  // ever added, it must be resolved here.
  if (!spec.startsWith(".")) return null;

  const dir = resolve(from, "..");
  const candidates = [
    resolve(dir, spec),
    resolve(dir, spec + ".ts"),
    resolve(dir, spec + ".tsx"),
    resolve(dir, spec + "/index.ts"),
    resolve(dir, spec + "/index.tsx"),
  ];

  for (const c of candidates) {
    if (isFile(c)) return c;
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

const entryPaths = ENTRY_POINTS.map((e) => resolve(PROJECT_ROOT, e));

describe("reachability", () => {
  it("every src/ file is reachable from the app entry point", () => {
    const srcFiles = collectFiles(SRC_DIR, [".ts", ".tsx"]);
    const reached = walkReachable(entryPaths);

    const unreached = srcFiles
      .map((f) => relative(PROJECT_ROOT, f))
      .filter((rel) => !(rel in ALLOWLIST))
      .filter((rel) => !reached.has(resolve(PROJECT_ROOT, rel)));

    expect(unreached, `Orphaned source files found. Wire them up or add to ALLOWLIST with a reason:\n${unreached.join("\n")}`).toEqual([]);
  });

  it("every ALLOWLIST entry still exists on disk", () => {
    const stale = Object.keys(ALLOWLIST).filter(
      (rel) => !existsSync(resolve(PROJECT_ROOT, rel)),
    );
    expect(stale, `Stale ALLOWLIST entries, remove them:\n${stale.join("\n")}`).toEqual([]);
  });
});

describe("reachability helpers", () => {
  it("parseImports handles every import form", () => {
    const content = [
      'import x from "./a";',
      'import { y } from "./b";',
      'import type { T } from "./c";',
      'export { z } from "./d";',
      'export * from "./e";',
      'import "./f";',
      'const g = import("./g");',
      'const h = import(/* @vite-ignore */ "./h");',
    ].join("\n");
    expect(parseImports(content).sort()).toEqual(
      ["./a", "./b", "./c", "./d", "./e", "./f", "./g", "./h"],
    );
  });

  it("walk from entry points reaches more than the entry itself", () => {
    const reached = walkReachable(entryPaths);
    expect(reached.has(resolve(PROJECT_ROOT, "src/mount.tsx"))).toBe(true);
    expect(reached.has(resolve(PROJECT_ROOT, "src/routes.tsx"))).toBe(true);
  });
});
