"""The `rp` CLI for researcher-profiles (SDK / serving commands).

Run ``rp --help`` for the list of subcommands. Other distributions can add
verbs through the ``researcher_profiles.cli_plugins`` entry-point group
(``CLI_PLUGIN_GROUP``).

Layout
======

Every verb lives in a group module (``_corpus``, ``_store``, ``_format``, ...)
that exposes exactly two names: ``add_parsers(sub)`` and ``COMMANDS`` (verb name
to handler). This module owns the root parser, the plugin hook, and the merge
of those tables. Plugins import the shared parser helpers (``add_subcommand``,
``add_json``, ``profile_dir_for_ref``, ``resolve_profile_arg``,
``root_resolution_note``) from here, never from the private ``_shared``.

Group modules import only ``argparse``, ``json``, ``sys``, ``pathlib`` and
``_shared`` at module scope. Anything heavier is imported inside the handler,
so building the parser stays cheap (asserted in ``tests/test_guardrails.py``).

Help, JSON, and exit codes are part of the interface
====================================================

Most callers are agents that read ``--help`` once, looking for a line to copy.
So every parser carries a one-line description and two or three runnable
examples with concrete slugs and ORCIDs, not ``<placeholders>``. Every command
that answers a question carries ``--json``; commands that write files do not.
Human text is the default, and status never shares stdout with a JSON document.

Exit codes, documented in the top-level epilog: ``0`` ok, ``1`` general error,
``2`` invalid usage, ``4`` validation or contract failure.
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
