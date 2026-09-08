"""Minimal markdown to HTML converter for a fixed subset of the syntax.

Handles: headings, paragraphs, bold, italic, links, lists (unordered and
ordered), code spans, blockquotes, and code blocks.  Anything else degrades
gracefully. The ``.md`` and JSON-LD carry the authoritative content, so
perfect fidelity is not required.

No external dependencies.
"""

import html
import re


def md_to_html(text: str) -> str:
    """Convert a markdown string to an HTML fragment."""
    if not text:
        return ""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Each block handler consumes the lines it owns and returns the
        # rendered fragment (None when nothing is emitted) plus the next index.
        if line.strip().startswith("```"):
            block, i = _fenced_code(lines, i)
        elif line.startswith("> ") or line == ">":
            block, i = _blockquote(lines, i)
        elif re.match(r"^(#{1,6})\s+(.+)$", line):
            block, i = _heading(line), i + 1
        elif re.match(r"^[\-\*]\s", line):
            block, i = _unordered_list(lines, i)
        elif re.match(r"^\d+\.\s", line):
            block, i = _ordered_list(lines, i)
        elif not line.strip():
            block, i = None, i + 1
        else:
            block, i = _paragraph(lines, i)

        if block is not None:
            out.append(block)

    return "\n".join(out)


def _fenced_code(lines: list[str], i: int) -> tuple[str, int]:
    """Render a fenced code block starting at ``lines[i]``."""
    lang = lines[i].strip()[3:].strip()
    code_lines: list[str] = []
    i += 1
    while i < len(lines) and not lines[i].strip().startswith("```"):
        code_lines.append(lines[i])
        i += 1
    i += 1  # skip closing ```
    code = html.escape("\n".join(code_lines))
    cls = f' class="language-{html.escape(lang)}"' if lang else ""
    return f"<pre><code{cls}>{code}</code></pre>", i


def _blockquote(lines: list[str], i: int) -> tuple[str, int]:
    """Render consecutive ``> `` lines, recursing on the stripped body."""
    bq_lines: list[str] = []
    while i < len(lines) and (lines[i].startswith("> ") or lines[i] == ">"):
        bq_lines.append(lines[i][2:] if lines[i].startswith("> ") else "")
        i += 1
    inner = md_to_html("\n".join(bq_lines))
    return f"<blockquote>{inner}</blockquote>", i


def _heading(line: str) -> str:
    """Render one ATX heading line (``#`` through ``######``)."""
    m = re.match(r"^(#{1,6})\s+(.+)$", line)
    level = len(m.group(1))
    content = _inline(m.group(2))
    return f"<h{level}>{content}</h{level}>"


def _unordered_list(lines: list[str], i: int) -> tuple[str, int]:
    """Render consecutive ``- `` / ``* `` items as one ``<ul>``."""
    items: list[str] = []
    while i < len(lines) and re.match(r"^[\-\*]\s", lines[i]):
        items.append(_inline(lines[i][2:]))
        i += 1
    return "<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>", i


def _ordered_list(lines: list[str], i: int) -> tuple[str, int]:
    """Render consecutive ``1. `` items as one ``<ol>``."""
    items: list[str] = []
    while i < len(lines) and re.match(r"^\d+\.\s", lines[i]):
        items.append(_inline(re.sub(r"^\d+\.\s", "", lines[i])))
        i += 1
    return "<ol>" + "".join(f"<li>{it}</li>" for it in items) + "</ol>", i


def _paragraph(lines: list[str], i: int) -> tuple[str | None, int]:
    """Collect consecutive non-blank, non-special lines into one ``<p>``.

    Returns ``None`` when the line at ``i`` is a block start that no other
    handler claimed (e.g. a bare ``# ``), so it is skipped rather than rendered.
    """
    para_lines: list[str] = []
    while i < len(lines) and lines[i].strip() and not _is_block_start(lines[i]):
        para_lines.append(lines[i])
        i += 1
    if not para_lines:
        return None, i + 1
    content = _inline(" ".join(para_lines))
    return f"<p>{content}</p>", i


def _is_block_start(line: str) -> bool:
    """Check if a line starts a new block element."""
    if re.match(r"^#{1,6}\s", line):
        return True
    if line.strip().startswith("```"):
        return True
    if line.startswith("> "):
        return True
    if re.match(r"^[\-\*]\s", line):
        return True
    if re.match(r"^\d+\.\s", line):
        return True
    return False


def _inline(text: str) -> str:
    """Process inline markdown: bold, italic, code, links.

    The whole run is HTML-escaped first, so a literal ``<``, ``>`` or ``&`` in
    prose reaches the page as text. The markdown markers survive escaping, and
    the substitutions below emit the only tags in the output.
    """
    text = html.escape(text, quote=False)
    # Code spans first (so bold/italic inside code are not processed)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Links [text](url)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{_attr(m.group(2))}">{m.group(1)}</a>',
        text,
    )
    # Bold **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
    # Italic *text* or _text_
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", text)
    return text


def _attr(escaped: str) -> str:
    """Make an already-escaped text run safe inside a double-quoted attribute."""
    return escaped.replace('"', "&quot;")
