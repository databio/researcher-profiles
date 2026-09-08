// Regenerate src/types.ts from the researcher-profiles HTTP wire contract.
//
// Pipeline:
//   1. Ask the researcher_profiles package to emit the combined wire JSON
//      Schema to schemas/wire.schema.json (authoritative pydantic source in
//      src/researcher_profiles/api_models.py).
//   2. Compile that schema to TypeScript with json-schema-to-typescript.
//
// Only src/types.ts is committed. schemas/wire.schema.json is a throwaway
// intermediate this script writes and immediately reads back, so it is
// gitignored and recreated on every run. Re-run `npm run gen:types` after any
// change to api_models.py so the viewer's types cannot drift from the wire
// contract.
//
// Python discovery: set RP_PYTHON to a python that can `import
// researcher_profiles`; otherwise rp-sdk/.venv and then the repo-root .venv
// are tried, then `python3`.
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { compile } from "json-schema-to-typescript";

const here = dirname(fileURLToPath(import.meta.url));
const viewerRoot = resolve(here, "..");
const repoRoot = resolve(viewerRoot, "..");
const schemaPath = resolve(viewerRoot, "schemas", "wire.schema.json");
const typesPath = resolve(viewerRoot, "src", "types.ts");

function pickPython() {
  if (process.env.RP_PYTHON) return process.env.RP_PYTHON;
  for (const venv of [
    resolve(repoRoot, "rp-sdk", ".venv", "bin", "python"),
    resolve(repoRoot, ".venv", "bin", "python"),
  ]) {
    if (existsSync(venv)) return venv;
  }
  return "python3";
}

const python = pickPython();
console.log(`[gen-types] exporting wire schema via ${python}`);
// The schema directory is not committed, so a fresh clone has no schemas/.
mkdirSync(dirname(schemaPath), { recursive: true });
execFileSync(
  python,
  ["-m", "researcher_profiles.cli", "schema", "export-wire", schemaPath],
  { cwd: repoRoot, stdio: "inherit" },
);

const schema = JSON.parse(readFileSync(schemaPath, "utf-8"));
const banner =
  "/**\n" +
  " * Wire contract for the researcher-profiles HTTP API.\n" +
  " *\n" +
  " * GENERATED FILE: do not edit by hand.\n" +
  " * Regenerate with `npm run gen:types` (source: src/researcher_profiles/api_models.py).\n" +
  " */";

const ts = await compile(schema, "ResearcherProfileWireContract", {
  bannerComment: banner,
  additionalProperties: true,
  style: { singleQuote: false },
});

writeFileSync(typesPath, ts, "utf-8");
console.log(`[gen-types] wrote ${typesPath}`);
