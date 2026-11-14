"""Heading anchors and section hashes for `expertise.md` and `SOUL.md` (#5).

The algorithm, so every validator computes the same anchor:

- Only ATX level-2 headings (a line starting `## `) outside fenced code blocks count.
- Inline Markdown markup is removed and the text is lowercased.
- Characters other than letters, digits, spaces and hyphens are removed, and
  spaces become hyphens.
- Slugs are never suffixed to make them unique: a duplicate or empty slug makes
  the file invalid.

A section runs from its heading line up to the next level-2 heading or the end
of the file. Its hash is SHA-256 over those bytes, as UTF-8 with LF line endings.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .common import sha256_bytes

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_H2 = re.compile(r"^## +(.*?)[ #]*$")


def _strip_markup(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)  # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML
    text = re.sub(r"(\*\*|__|\*|_|~~|`)", "", text)  # emphasis, strike, code
    return text


def slugify(heading_text: str) -> str:
    text = _strip_markup(heading_text).strip().lower()
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N") or ch == "-":
            out.append(ch)
        elif ch == " ":
            out.append("-")
    return "".join(out)


@dataclass(frozen=True)
class Section:
    anchor: str
    heading: str
    start: int  # byte offset of the heading line in the LF-normalized text
    end: int
    sha256: str


@dataclass(frozen=True)
class SectionScan:
    preamble_end: int
    sections: list[Section]
    duplicates: list[str]
    empty: list[str]  # headings whose slug is empty


def normalize(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def scan(text: str) -> SectionScan:
    data = normalize(text)
    lines = data.split(b"\n")
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    heads: list[tuple[int, str]] = []
    fence: str | None = None
    for i, raw in enumerate(lines):
        line = raw.decode("utf-8")
        m = _FENCE.match(line)
        if m:
            marker = m.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is None:
            h = _H2.match(line)
            if h:
                heads.append((offsets[i], h.group(1)))
    sections, seen, dups, empty = [], set(), [], []
    for j, (start, heading) in enumerate(heads):
        end = heads[j + 1][0] if j + 1 < len(heads) else len(data)
        anchor = slugify(heading)
        if not anchor:
            empty.append(heading)
        elif anchor in seen:
            dups.append(anchor)
        seen.add(anchor)
        sections.append(Section(anchor, heading, start, end, sha256_bytes(data[start:end])))
    return SectionScan(heads[0][0] if heads else len(data), sections, dups, empty)
