"""Bounded reads of long text: sections and pages.

A paper's full text has a 14K-token median and a quarter run past 25K, which
is more than many clients accept in one response. A read here returns the
whole text up to :data:`~._limits.TEXT_CHUNK` characters, and above that the
first chunk with ``has_more`` and the offset to continue from. ``section=``
reads one heading's span without offset arithmetic.

Offsets are Python string indices into the stored text. The server always
computes ``next_offset``, so a caller never has to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..models.api import TextPage, TextSection
from ._limits import TEXT_CHUNK, TEXT_MAX_CHARS_CAP, clamp

#: Markdown ATX headings, levels 1 to 3, at the start of a line.
_HEADING = re.compile(r"^(#{1,3})[ \t]+(.+?)[ \t#]*$", re.MULTILINE)

#: How far back from a cut a chunk may end early to land on a blank line.
_PARAGRAPH_WINDOW = 2_000


@dataclass
class Section:
    """One heading's span: its name, where it starts, and how long it is."""

    name: str
    offset: int
    chars: int


def sections_of(md: str) -> list[Section]:
    """Markdown headings (``#`` to ``###``) with char offsets.

    A section runs from its heading to the next heading of any level. Text
    before the first heading is a ``body`` section; a document with no
    heading at all is one ``body`` section. A repeated heading name gets a
    numeric suffix (``Methods (2)``) so every name is a valid ``section=``.
    """
    heads = [(m.start(), m.group(2).strip()) for m in _HEADING.finditer(md)]
    if not heads:
        return [Section("body", 0, len(md))] if md else []
    out: list[Section] = []
    if heads[0][0] > 0 and md[: heads[0][0]].strip():
        out.append(Section("body", 0, heads[0][0]))
    seen: dict[str, int] = {}
    for i, (start, name) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(md)
        n = seen.get(name, 0) + 1
        seen[name] = n
        out.append(Section(name if n == 1 else f"{name} ({n})", start, end - start))
    return out


def section_at(sections: list[Section], offset: int) -> Optional[str]:
    """The name of the section holding ``offset``, or ``None``."""
    name = None
    for s in sections:
        if s.offset <= offset:
            name = s.name
        else:
            break
    return name


def page_text(
    text: str, *, section: Optional[str], offset: int, max_chars: Optional[int]
) -> TextPage:
    """One page of ``text``, or of one section of it.

    ``offset`` is always an index into the whole text, so an offset from a
    passage or a ``next_offset`` works with or without ``section``. With a
    section, the page starts at ``max(offset, section start)`` and never runs
    past the section's end. The page is at most
    ``clamp(max_chars, TEXT_CHUNK, TEXT_MAX_CHARS_CAP)`` chars; a page that is
    cut ends at the last blank line in its final 2,000 chars, so it does not
    stop mid-paragraph when it can help it. ``total_chars`` is the length of
    the section, or of the whole text.

    An unknown section raises :class:`ValueError` whose ``args[1]`` lists the
    valid names; the route maps it to a 400 ``unknown_section``.
    """
    sections = sections_of(text)
    start_bound, end_bound = 0, len(text)
    if section is not None:
        match = next((s for s in sections if s.name == section), None)
        if match is None:
            # Case-insensitive second chance: "methods" for "Methods".
            folded = [s for s in sections if s.name.lower() == section.lower()]
            match = folded[0] if len(folded) == 1 else None
        if match is None:
            raise ValueError(f"unknown section {section!r}", [s.name for s in sections])
        section = match.name
        start_bound, end_bound = match.offset, match.offset + match.chars
    start = min(max(offset, start_bound), end_bound)
    size = clamp(max_chars, TEXT_CHUNK, TEXT_MAX_CHARS_CAP)
    end = min(end_bound, start + size)
    if end < end_bound:
        brk = text.rfind("\n\n", max(start + 1, end - _PARAGRAPH_WINDOW), end)
        if brk != -1:
            end = brk + 2
    more = end < end_bound
    body = text[start:end]
    return TextPage(
        section=section,
        offset=start,
        returned_chars=len(body),
        total_chars=end_bound - start_bound,
        has_more=more,
        next_offset=end if more else None,
        sections=[TextSection(name=s.name, offset=s.offset, chars=s.chars) for s in sections],
        text=body,
    )


__all__ = ["Section", "page_text", "section_at", "sections_of"]
