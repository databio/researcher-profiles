/**
 * The "no garbage characters" rule for paper full text and summaries.
 *
 * Mirrors `researcher_profiles.text_artifact.text_artifact_problems` in the
 * Python SDK (same rules, same thresholds). See that module for why the
 * thresholds are shares rather than "any": real PDF extraction leaves a few
 * stray characters in legitimate papers, while binary decoded as text is
 * dense with them. Greek letters, math symbols, accents, curly quotes, and
 * dashes are all fine.
 */

/** Manifest roles whose files this rule applies to. */
export const TEXT_ARTIFACT_ROLES: ReadonlySet<string> = new Set([
  "paper_fulltext",
  "paper_summary",
]);

/** Largest share of U+FFFD characters a text artifact may have. */
export const MAX_REPLACEMENT_SHARE = 0.01;

/** Largest share of control characters (other than tab, LF, CR). */
export const MAX_CONTROL_SHARE = 0.02;

const PDF_HEADER = /^﻿?\s*%PDF-\d/;

const PDF_MARKERS: Array<[string, RegExp]> = [
  ["N 0 obj", /^\d+ \d+ obj\b/m],
  ["endobj", /^endobj\b/m],
  ["endstream", /^endstream\b/m],
  ["stream", /stream\r?\n(?:x|�)/],
  ["startxref", /^startxref\b/m],
  ["%%EOF", /^%%EOF\b/m],
];

function isDisallowedControl(cp: number): boolean {
  if (cp === 0x09 || cp === 0x0a || cp === 0x0d) return false;
  return cp <= 0x1f || (cp >= 0x7f && cp <= 0x9f);
}

function pct(share: number): string {
  return `${(share * 100).toFixed(1)}%`;
}

/** Why `content` is not a clean text artifact; empty when it is. */
export function textArtifactProblems(content: ArrayBuffer | string): string[] {
  const problems: string[] = [];

  let text: string;
  if (typeof content === "string") {
    text = content;
  } else {
    try {
      text = new TextDecoder("utf-8", { fatal: true }).decode(content);
    } catch {
      problems.push("not valid UTF-8");
      text = new TextDecoder("utf-8").decode(content);
    }
  }

  let length = 0;
  let replacements = 0;
  let controls = 0;
  let nuls = 0;
  for (const ch of text) {
    length++;
    const cp = ch.codePointAt(0) ?? 0;
    if (cp === 0xfffd) replacements++;
    else if (isDisallowedControl(cp)) {
      controls++;
      if (cp === 0) nuls++;
    }
  }
  const denom = Math.max(length, 1);

  if (replacements / denom > MAX_REPLACEMENT_SHARE) {
    problems.push(
      `${replacements} U+FFFD replacement characters (${pct(replacements / denom)} of the text): binary bytes decoded as text`,
    );
  }
  if (nuls > 0) problems.push(`${nuls} NUL character(s): binary data`);
  if (controls / denom > MAX_CONTROL_SHARE) {
    problems.push(
      `${controls} control characters (${pct(controls / denom)} of the text; only tab, newline, and carriage return are normal)`,
    );
  }

  if (PDF_HEADER.test(text)) {
    problems.push("starts with a %PDF- header: raw PDF bytes saved as text");
  } else {
    const found = PDF_MARKERS.filter(([, re]) => re.test(text)).map(([name]) => name);
    if (found.length >= 2) {
      problems.push(`contains PDF file structure (${found.join(", ")}): raw PDF bytes`);
    }
  }

  return problems;
}
