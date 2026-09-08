// Copy agent skill markdown into public/skills/ so the built site serves
// the raw files the Skills page links to.
// Run with: npm run gen:skills (also runs automatically before dev/build).
//
// Sources (in rp-sdk):
//   src/researcher_profiles/skill/ -> public/skills/researcher-profile/   (the talk-to skill)
//   skills/<name>/                 -> public/skills/<name>/
//
// Only .md files are copied. The output directory is gitignored and rebuilt
// fresh; `--out <dir>` (relative to the working directory) points it somewhere
// else, which is how a host application building its own bundle gets the same
// skill files into ITS public/ directory.
import { cpSync, existsSync, lstatSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "..", "..");
const sdk = resolve(repo, "rp-sdk");
const outArg = process.argv.indexOf("--out");
const outRoot =
  outArg > -1
    ? resolve(process.cwd(), process.argv[outArg + 1])
    : resolve(here, "..", "public", "skills");

const jobs = [
  {
    src: resolve(sdk, "src", "researcher_profiles", "skill"),
    dest: resolve(outRoot, "researcher-profile"),
  },
];
const skillsDir = resolve(sdk, "skills");
if (existsSync(skillsDir)) {
  for (const entry of readdirSync(skillsDir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      jobs.push({ src: resolve(skillsDir, entry.name), dest: resolve(outRoot, entry.name) });
    }
  }
}

rmSync(outRoot, { recursive: true, force: true });
mkdirSync(outRoot, { recursive: true });

for (const { src, dest } of jobs) {
  if (!existsSync(src)) {
    console.warn(`[copy-skills] missing source, skipping: ${src}`);
    continue;
  }
  cpSync(src, dest, {
    recursive: true,
    filter: (s) => lstatSync(s).isDirectory() || s.endsWith(".md"),
  });
  console.log(`[copy-skills] ${src} -> ${dest}`);
}
