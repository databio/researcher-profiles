#!/usr/bin/env python3
"""Check every relative markdown link in every tracked `.md` file resolves.

Several documentation trees sit at different depths and reference each other
across component boundaries (`docs/` and `docs-dev/` at the repo root, plus a
README inside each component), so a link that was correct before a restructure
is silently wrong after it. This is the check that catches that. External links
(http/https, mailto) and pure in-page anchors are not checked; a link with a
`#fragment` is checked for the file only.

Usage: python scripts/check_links.py [--root PATH]
Exit code 1 if any link is broken.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

#: [text](target): the inline form. Reference-style definitions ([id]: target)
#: are matched separately below.
INLINE = re.compile(r"\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFDEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?", re.MULTILINE)

SKIP_PREFIXES = ("http://", "https://", "mailto:", "tel:", "data:", "#", "//")

#: Fenced code blocks: links inside them are illustrative, not navigable.
FENCE = re.compile(r"^\s*(```|~~~)")

#: Templates whose links are relative to where their OUTPUT lands, not to
#: themselves. Checking them in place reports false breaks; the rendered file
#: is tracked and gets checked like any other document.
SKIP_FILES = frozenset({"rp-sdk/scripts/python-api-directives.md"})


def strip_code_fences(text: str) -> str:
    out, in_fence = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def tracked_markdown(root: Path) -> list[Path]:
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [root / name for name in listing.split("\0") if name]


def broken_links(path: Path, root: Path) -> list[str]:
    text = strip_code_fences(path.read_text(encoding="utf-8"))
    targets = [m.group(1) for m in INLINE.finditer(text)]
    targets += [m.group(1) for m in REFDEF.finditer(text)]
    problems = []
    for raw in targets:
        if raw.startswith(SKIP_PREFIXES):
            continue
        target = unquote(raw.split("#", 1)[0])
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            problems.append(f"{path.relative_to(root)}: {raw}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()

    problems: list[str] = []
    for md in tracked_markdown(root):
        if md.exists() and md.relative_to(root).as_posix() not in SKIP_FILES:
            problems += broken_links(md, root)

    if problems:
        print(f"{len(problems)} broken markdown link(s):", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print("all relative markdown links resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
