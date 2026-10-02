"""The character rule for a profile's text artifacts (paper full text, summaries).

One rule, used in two places: the builder calls :func:`text_artifact_problems`
right after it downloads a paper or accepts a summary (so a garbled file is
never written as good), and ``rp validate`` calls it on every
``sources/papers/*.md`` and ``sources/summaries/*.summary.md`` (so one that got
through anyway fails the profile).

The rule is "no garbage", not "ASCII only". Greek letters, math symbols,
accented names, curly quotes, and em-dashes are all fine. What fails:

- bytes that are not valid UTF-8;
- any NUL character;
- U+FFFD replacement characters above :data:`MAX_REPLACEMENT_SHARE` of the
  text (the mark left when binary bytes are decoded with ``errors="replace"``);
- control characters other than tab, newline, and carriage return above
  :data:`MAX_CONTROL_SHARE` of the text;
- a raw PDF: the ``%PDF-`` header at the start, or two or more distinct PDF
  structure markers (``N 0 obj``, ``endobj``, ``endstream``, ``stream`` followed
  by binary, ``startxref``, ``%%EOF``). One marker alone never fails, so a paper
  that mentions ``endobj`` in prose is safe.

Why shares and not "any": real PDF text extraction leaves a few stray
characters behind. Math fonts map big brackets to U+0010-U+001B, pdftotext
writes a form feed at each page break, and an unmappable glyph becomes one
U+FFFD. Measured over ~7,400 paper and summary files on disk, legitimate
papers top out at 0.09% U+FFFD and 1.3% control characters, while every
binary-as-text file sits at 4.6% or more U+FFFD and 4.9% or more control
characters. The thresholds sit in that gap.

Stdlib only, so the builder can import it without pulling in anything else.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "ALLOWED_CONTROL_CHARS",
    "MAX_CONTROL_SHARE",
    "MAX_REPLACEMENT_SHARE",
    "TEXT_ARTIFACT_ROLES",
    "text_artifact_problems",
]

#: Manifest roles whose files are human-readable text this rule applies to.
TEXT_ARTIFACT_ROLES: tuple[str, ...] = ("paper_fulltext", "paper_summary")

#: Control characters that are always fine.
ALLOWED_CONTROL_CHARS = frozenset("\t\n\r")

#: Largest share of U+FFFD characters a text artifact may have.
MAX_REPLACEMENT_SHARE = 0.01

#: Largest share of other control characters a text artifact may have.
MAX_CONTROL_SHARE = 0.02

_REPLACEMENT_CHAR = "�"

#: The PDF file header, after an optional BOM and whitespace.
_PDF_HEADER = re.compile(r"\A﻿?\s*%PDF-\d")

#: Distinct PDF structure markers. Two or more of these is a raw PDF body.
_PDF_MARKERS: dict[str, re.Pattern[str]] = {
    "N 0 obj": re.compile(r"(?m)^\d+ \d+ obj\b"),
    "endobj": re.compile(r"(?m)^endobj\b"),
    "endstream": re.compile(r"(?m)^endstream\b"),
    "stream": re.compile(r"stream\r?\n(?:x|�)"),
    "startxref": re.compile(r"(?m)^startxref\b"),
    "%%EOF": re.compile(r"(?m)^%%EOF\b"),
}

#: How many offending characters to name in a problem message.
_MAX_EXAMPLES = 5


def _describe(ch: str) -> str:
    name = unicodedata.name(ch, "")
    return f"U+{ord(ch):04X}{f' {name}' if name else ''}"


def text_artifact_problems(content: bytes | str, *, min_chars: int = 0) -> list[str]:
    """Return why ``content`` is not a clean text artifact; empty when it is.

    Args:
        content: The file's raw bytes (preferred: invalid UTF-8 is only
            detectable on bytes) or already-decoded text.
        min_chars: When positive, also fail text with fewer than this many
            characters after trimming whitespace. The builder passes a floor
            for full text; the validator does not (size is not a format rule).

    Returns:
        One human-readable sentence per failed rule, in a stable order.
    """
    problems: list[str] = []

    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append(f"not valid UTF-8 (first bad byte at offset {exc.start})")
            text = content.decode("utf-8", errors="replace")
    else:
        text = content

    length = max(len(text), 1)

    n_replacement = text.count(_REPLACEMENT_CHAR)
    if n_replacement / length > MAX_REPLACEMENT_SHARE:
        problems.append(
            f"{n_replacement} U+FFFD replacement characters ({n_replacement / length:.1%} "
            f"of the text, limit {MAX_REPLACEMENT_SHARE:.0%}): binary bytes decoded as text"
        )

    bad_controls: dict[str, int] = {}
    for ch in text:
        if ch not in ALLOWED_CONTROL_CHARS and unicodedata.category(ch) == "Cc":
            bad_controls[ch] = bad_controls.get(ch, 0) + 1
    n_controls = sum(bad_controls.values())
    n_nul = bad_controls.get("\x00", 0)
    if n_nul:
        problems.append(f"{n_nul} NUL character(s): binary data")
    if n_controls / length > MAX_CONTROL_SHARE:
        examples = ", ".join(_describe(c) for c in list(bad_controls)[:_MAX_EXAMPLES])
        problems.append(
            f"{n_controls} control characters ({n_controls / length:.1%} of the text, "
            f"limit {MAX_CONTROL_SHARE:.0%}; only tab, newline, and carriage return are "
            f"normal): {examples}"
        )

    if _PDF_HEADER.match(text):
        problems.append("starts with a %PDF- header: raw PDF bytes saved as text")
    else:
        found = [name for name, pat in _PDF_MARKERS.items() if pat.search(text)]
        if len(found) >= 2:
            problems.append(f"contains PDF file structure ({', '.join(found)}): raw PDF bytes")

    if min_chars > 0:
        n = len(text.strip())
        if n < min_chars:
            problems.append(f"too short to be full text ({n} chars, minimum {min_chars})")

    return problems
