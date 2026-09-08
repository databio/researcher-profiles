"""The `rp` CLI for researcher-profiles (SDK / serving commands).

Run ``rp --help`` for the current list of subcommands; it is generated from the
group modules below and cannot go stale the way a hand-written list here does.

Out-of-tree distributions can add their own verbs through the
``researcher_profiles.cli_plugins`` entry-point group (see
``CLI_PLUGIN_GROUP``), so the set of verbs a given install offers depends on
what else is installed.

How the CLI package is laid out
===============================

Every verb lives in a group module (``_corpus``, ``_store``, ``_format``,
``_publish``, ``_registry``, ``_auth``, ``_skill``, ``_sign``, ``_agent``), and
each of those exposes exactly two names: ``add_parsers(sub)``, which registers
its parsers, and ``COMMANDS``, which maps each of its verb names to the handler
that runs it. This module owns only the root parser, the plugin hook, and the
merge of those tables. ``_shared`` holds the exit codes and the parser helpers
every group reuses. The ones a plugin may reuse too are re-exported from this
module, so a plugin imports them from ``researcher_profiles.cli`` and never
from the private ``_shared``: ``add_subcommand``
(one subcommand with a summary and worked examples), ``add_json`` (the one
``--json`` flag), ``profile_dir_for_ref`` (slug or rid to a profile directory),
``resolve_profile_arg`` (a ``profile`` positional -- a path, a rid or a slug --
to a directory, reporting the miss itself) and ``root_resolution_note`` (the
sentence explaining which root was searched).

Group modules import ``argparse``, ``json``, ``sys``, ``pathlib`` and
``_shared`` at module scope and nothing else: everything heavier is imported
inside the handler that needs it, so building the parser stays cheap. See
``tests/test_guardrails.py``, which asserts that in a subprocess.

Help, JSON, and exit codes are part of the interface
====================================================

Most callers of this CLI are agents, and an agent reads ``--help`` the way a
person reads a man page: once, quickly, looking for a line it can copy. So every
parser here carries a one-line description and two or three *runnable* examples
with concrete slugs and ORCIDs in them. A `<placeholder>` teaches an agent
nothing it did not already know from the metavar. Every command that answers a
question (``search``, ``where``, ``list``, ``listr``, ``seek``, ``manifest``,
``validate``, ``export``, ``rank-works``, ``whoami``, ``login``, ``push``,
``agent whoami``, ``agent scopes``, ``graph build``, and the ``rp db``
subcommands) carries ``--json``; the commands that write files (``index``,
``render``, ``site``, ``sign``, ``schema``, ...) do not. Human text stays the
default, and status never shares stdout with a JSON document.

Exit codes follow one table across the whole tool: ``0`` ok, ``1`` general
error, ``2`` invalid usage, ``4`` validation or contract failure. They are
documented in the top-level epilog because an agent checks
``$?`` and needs to know whether a non-zero means "you typed it wrong" or "the
profile really does not pass".
"""

from ._root import _COMMANDS as _COMMANDS
from ._root import CLI_PLUGIN_GROUP, build_parser, main
from ._shared import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    add_json,
    add_subcommand,
    profile_dir_for_ref,
    resolve_profile_arg,
    root_resolution_note,
)
from ._skill import iter_skill_files, skill_resource_root

__all__ = [
    "CLI_PLUGIN_GROUP",
    "EXIT_ERROR",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_VALIDATION",
    "add_json",
    "add_subcommand",
    "build_parser",
    "iter_skill_files",
    "main",
    "profile_dir_for_ref",
    "resolve_profile_arg",
    "root_resolution_note",
    "skill_resource_root",
]
