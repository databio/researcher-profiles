/**
 * Regression lock for an inline-style bug: an inline `style={{...}}` set a
 * `background` with no `color`, and a bare hex fallback resolved
 * differently in light vs dark mode, producing invisible text. These two
 * assertions make that class of bug impossible to reintroduce:
 *
 *  (a) no project CSS file defines a color as a bare hex literal: every
 *      color must come from a token.
 *  (b) no .tsx file sets `background` or `color` via an inline
 *      `style={{...}}` object: that path bypasses the token system and
 *      the dark-mode pass entirely.
 */
import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const HEX_COLOR = /#[0-9a-fA-F]{3,8}\b/g;
const INLINE_STYLE = /style=\{\{([\s\S]*?)\}\}/g;
const STYLE_BG_OR_COLOR = /\b(background|color)\w*\s*:/;

function walk(dir: string, matchExt: string[]): string[] {
  const out: string[] = [];
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return out;
  }
  for (const entry of entries) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      out.push(...walk(full, matchExt));
    } else if (matchExt.some((ext) => entry.endsWith(ext))) {
      out.push(full);
    }
  }
  return out;
}

describe("theme regression lock", () => {
  it("no project CSS under src/styles/ contains a hex color literal", () => {
    const files = walk(resolve(__dirname, "../src/styles"), [".css"]);
    const offenders: string[] = [];
    for (const f of files) {
      if (f.endsWith("tokens.css")) continue;
      const content = readFileSync(f, "utf8");
      if (HEX_COLOR.test(content)) offenders.push(f);
      HEX_COLOR.lastIndex = 0;
    }
    expect(offenders).toEqual([]);
  });

  it("no *.module.css under rp-ui-lib/src/lib/ contains a hex color literal", () => {
    const files = walk(resolve(__dirname, "../../rp-ui-lib/src/lib"), [".module.css"]);
    expect(files.length).toBeGreaterThan(0);
    const offenders: string[] = [];
    for (const f of files) {
      const content = readFileSync(f, "utf8");
      if (HEX_COLOR.test(content)) offenders.push(f);
      HEX_COLOR.lastIndex = 0;
    }
    expect(offenders).toEqual([]);
  });

  it("no .tsx under src/ sets background/color via an inline style object", () => {
    const files = walk(resolve(__dirname, "../src"), [".tsx"]);
    expect(files.length).toBeGreaterThan(0);
    const offenders: string[] = [];
    for (const f of files) {
      const content = readFileSync(f, "utf8");
      let m: RegExpExecArray | null;
      INLINE_STYLE.lastIndex = 0;
      while ((m = INLINE_STYLE.exec(content))) {
        if (STYLE_BG_OR_COLOR.test(m[1])) {
          offenders.push(f);
          break;
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
