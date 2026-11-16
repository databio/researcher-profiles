"""Level-2 sections of the prose files and their hashes (#5).

Sections are keyed by their heading text as written, with no slug:

- Only ATX level-2 headings (a line starting `## `) outside fenced code blocks count.
- The heading text is the line after `## `, with surrounding spaces and any
  closing `#`s removed.
- Two headings with the same text, or an empty heading, make the profile invalid.

A section runs from its heading line up to the next level-2 heading or the end
of the file. Its hash is SHA-256 over those bytes, as UTF-8 with LF line endings.
Text before the first heading belongs to the file's record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .common import sha256_bytes

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_H2 = re.compile(r"^## +(.*?)[ #]*$")


@dataclass(frozen=True)
class Section:
    heading: str
    start: int  # byte offset of the heading line in the LF-normalized text
    end: int
    sha256: str


@dataclass(frozen=True)
class SectionScan:
    preamble_end: int
    sections: list[Section]
    duplicates: list[str]
    empty: int  # headings with no text

    def by_heading(self) -> dict[str, Section]:
        return {s.heading: s for s in self.sections}


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
                heads.append((offsets[i], h.group(1).strip()))
    sections, seen, dups, empty = [], set(), [], 0
    for j, (start, heading) in enumerate(heads):
        end = heads[j + 1][0] if j + 1 < len(heads) else len(data)
        if not heading:
            empty += 1
        elif heading in seen:
            dups.append(heading)
        seen.add(heading)
        sections.append(Section(heading, start, end, sha256_bytes(data[start:end])))
    return SectionScan(heads[0][0] if heads else len(data), sections, dups, empty)


def section_text(text: str, section: Section) -> str:
    return normalize(text)[section.start : section.end].decode("utf-8")
