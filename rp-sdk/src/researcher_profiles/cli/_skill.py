"""``rp skill``: print or install the consumer skill for reading published profiles."""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from ._shared import EXIT_OK, EXIT_USAGE, add_subcommand


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register this group's verbs on the root subparser action."""
    p_skill = add_subcommand(
        sub,
        "skill",
        "Print or install the consumer skill for reading published profiles",
        "rp skill",
        "rp skill --install",
        "rp skill --install --dir ~/.claude/skills/researcher-profile",
    )
    p_skill_group = p_skill.add_mutually_exclusive_group()
    p_skill_group.add_argument(
        "--print",
        action="store_true",
        dest="skill_print",
        help="Print SKILL.md to stdout (default)",
    )
    p_skill_group.add_argument(
        "--install",
        action="store_true",
        help="Install the skill to a local directory",
    )
    p_skill.add_argument(
        "--dir",
        default=None,
        metavar="DIR",
        help="Installation directory (default: ~/.claude/skills/researcher-profile/)",
    )


def skill_resource_root():
    """Return the packaged consumer-skill tree as an ``importlib`` traversable.

    The skill ships *inside* the package (``researcher_profiles/skill/``) and is
    reached through the resource API, never through ``__file__`` arithmetic.
    Walking up from ``__file__`` would only work in a source checkout: from a
    wheel, an sdist install, or a container image the repo layout is not there.
    ``importlib.resources`` asks the loader that actually imported the package
    where its data lives, so the answer is right for every install layout.
    """
    from importlib.resources import files

    return files("researcher_profiles") / "skill"


def iter_skill_files(skill_root):
    """Yield ``(relative_posix_path, traversable)`` for every file under a tree.

    The skill has subdirectories (``examples/``, ``reference/``), and the
    resource API has no ``rglob``, so the recursion is written out here.
    """
    from pathlib import PurePosixPath

    def _walk(node, prefix: PurePosixPath):
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            rel = prefix / child.name
            if child.is_dir():
                yield from _walk(child, rel)
            else:
                yield rel, child

    yield from _walk(skill_root, PurePosixPath())


def _cmd_skill(args: argparse.Namespace) -> int:
    """Print the consumer SKILL.md, or install the whole skill tree."""
    skill_root = skill_resource_root()
    skill_md = skill_root / "SKILL.md"
    if not skill_md.is_file():
        print(f"SKILL.md not found at {skill_md}", file=sys.stderr)
        return EXIT_USAGE
    if args.install:
        dest = Path(args.dir or "~/.claude/skills/researcher-profile").expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        for rel, resource in iter_skill_files(skill_root):
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(resource.read_bytes())
            print(f"  {rel}")
        print(f"\ninstalled to {dest}")
    else:
        print(skill_md.read_text(encoding="utf-8"), end="")
    return EXIT_OK


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "skill": _cmd_skill,
}
