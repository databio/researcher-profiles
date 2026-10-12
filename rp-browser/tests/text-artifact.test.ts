/**
 * The explorer's copy of the "no garbage characters" rule must agree with the
 * Python SDK's (researcher_profiles.text_artifact). It runs over the same real
 * garbled downloads the SDK tests use.
 */
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { textArtifactProblems } from "../src/validate/textArtifact";

const HERE = dirname(fileURLToPath(import.meta.url));
const GARBLED_DIR = join(HERE, "..", "..", "rp-sdk", "tests", "fixtures", "text_artifacts");

function bytes(s: string): ArrayBuffer {
  const u8 = new TextEncoder().encode(s);
  return u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength) as ArrayBuffer;
}

const CLEAN: Record<string, string> = {
  greek_and_math: "The α-helix and β-sheet; ∑ᵢ xᵢ ≤ 10⁻³ and ∫ f(x) dx ≈ π/2 ± 0.1 μM.",
  accented_names: "Schrödinger, Müller, Gómez-Pérez, Łukasz, Dvořák, and Ångström.",
  typography: "“Curly quotes,” ‘single ones,’ an em-dash — and an en-dash 1–2 … done.",
  whitespace: "Line one\r\nLine two\n\tIndented with a tab.\n",
  sparse_extraction_artifacts:
    "Real prose about chromatin accessibility. ".repeat(200) + "\f\x12(a+b)\x13\n",
  one_replacement_char: "Real prose about gene regulation. ".repeat(200) + "�",
  mentions_pdf_words:
    "We parsed each file's trailer and every endobj token; the stream was then decoded.",
};

describe("textArtifactProblems", () => {
  for (const [name, text] of Object.entries(CLEAN)) {
    it(`passes clean text: ${name}`, () => {
      expect(textArtifactProblems(text)).toEqual([]);
      expect(textArtifactProblems(bytes(text))).toEqual([]);
    });
  }

  const garbled = readdirSync(GARBLED_DIR).filter((f) => f.endsWith(".md"));
  it("has the shared garbled fixtures", () => {
    expect(garbled.length).toBeGreaterThanOrEqual(3);
  });
  for (const f of garbled) {
    it(`fails real garbled download: ${f}`, () => {
      const buf = readFileSync(join(GARBLED_DIR, f));
      const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) as ArrayBuffer;
      const problems = textArtifactProblems(ab);
      expect(problems.some((p) => p.includes("U+FFFD"))).toBe(true);
    });
  }

  it.each([
    {
      name: "invalid UTF-8",
      input: new Uint8Array([0x23, 0x20, 0x41, 0xff, 0xfe, 0xc3]).buffer as ArrayBuffer | string,
      problem: "not valid UTF-8",
    },
    { name: "a NUL", input: "Real prose. ".repeat(200) + "\x00", problem: "NUL" },
    {
      name: "a PDF header",
      input: "%PDF-1.5\n1 0 obj\n<<>>\nendobj\n",
      problem: "%PDF-",
    },
    {
      name: "PDF structure without a header",
      input: "Some text\n12 0 obj\n<< /Length 5 >>\nstream\nxhello\nendstream\nendobj\n",
      problem: "PDF file structure",
    },
  ])("fails $name", ({ input, problem }) => {
    expect(textArtifactProblems(input).some((p) => p.includes(problem))).toBe(true);
  });
});
