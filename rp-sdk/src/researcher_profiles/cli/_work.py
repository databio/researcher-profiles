"""``rp work``: the field-level verbs for one record in the works corpus.

``rp profile`` edits the profile document and the SOUL; this is its counterpart
one level down, for the bibliographic record of a single paper. A wrong DOI on
one work used to need ``rp push``, which replaces the whole profile directory
to fix one string.

Like ``rp profile``, everything here talks to a remote server, so every import
of the client machinery is deferred into the handler that needs it.
"""

import argparse
import json
import sys
from collections.abc import Callable

from ._shared import EXIT_ERROR, EXIT_OK, EXIT_USAGE, _add_host, add_json, add_subcommand


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register this group's verbs on the root subparser action."""
    p_work = add_subcommand(
        sub,
        "work",
        "Agent editing of one work in the profile's corpus (show/set/rm)",
        "rp work show smith2023protein",
        "rp work set smith2023protein doi=10.1038/s41586-023-06000-1",
        "rp work rm smith2023protein",
    )
    work_sub = p_work.add_subparsers(dest="work_cmd", metavar="<subcommand>")

    p_show = add_subcommand(
        work_sub,
        "show",
        "Print one work's record",
        "rp work show smith2023protein",
        "rp work show smith2023protein --json",
    )
    p_show.add_argument("paper_id", help="The work's paper_id")
    add_json(p_show, "JSON output")
    _add_host(p_show)

    p_set = add_subcommand(
        work_sub,
        "set",
        "Set fields on one work",
        "rp work set smith2023protein doi=10.1038/s41586-023-06000-1",
        "rp work set smith2023protein datePublished=2023 access=open --dry-run",
    )
    p_set.add_argument("paper_id", help="The work's paper_id")
    p_set.add_argument("assignment", nargs="+", metavar="key=value", help="Fields to set")
    p_set.add_argument("--if-match", help="Base hash for conflict detection")
    p_set.add_argument("--force", action="store_true", help="Skip base_hash check")
    p_set.add_argument(
        "--dry-run", action="store_true", help="Show what would change without writing"
    )
    add_json(p_set, "JSON output")
    _add_host(p_set)

    p_rm = add_subcommand(
        work_sub,
        "rm",
        "Remove one work from the corpus",
        "rp work rm smith2023protein",
    )
    p_rm.add_argument("paper_id", help="The work's paper_id")
    _add_host(p_rm)


def _parse_assignments(assignments: list[str]) -> dict:
    """``key=value`` pairs to a patch dict, JSON-typed where the value parses.

    ``is_corresponding=true`` and ``datePublished=2023`` have to arrive as a
    bool and an int or the record fails re-validation on the server, and
    quoting every string would be the worse trade: a title with a colon in it
    is not JSON and must stay the string the user typed.
    """
    patch: dict = {}
    for item in assignments:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise ValueError(f"not a key=value assignment: {item!r}")
        try:
            patch[key] = json.loads(raw)
        except ValueError:
            patch[key] = raw
    return patch


def _work_show(client, slug: str, args: argparse.Namespace) -> int:
    """Print one work's remote record."""
    from .auth.agent import AgentAPIError

    try:
        record = client.profile.get_work(slug, args.paper_id)
    except AgentAPIError as e:
        print(f"Error: {e.detail}", file=sys.stderr)
        return EXIT_ERROR
    if args.as_json:
        print(json.dumps(record, indent=2))
        return EXIT_OK
    for key in sorted(record):
        print(f"{key}: {record[key]}")
    return EXIT_OK


def _work_set(client, slug: str, args: argparse.Namespace) -> int:
    """Patch the named fields on one work."""
    from .auth.agent import AgentAPIError

    try:
        patch = _parse_assignments(args.assignment)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return EXIT_USAGE

    if args.dry_run:
        # The current remote values too: "would set doi=X" is only useful next
        # to what doi already is.
        try:
            record = client.profile.get_work(slug, args.paper_id)
        except AgentAPIError as e:
            print(f"Error: {e.detail}", file=sys.stderr)
            return EXIT_ERROR
        for key in sorted(patch):
            print(f"{key}: {record.get(key)!r} -> {patch[key]!r}")
        return EXIT_OK

    base_hash = None if args.force else args.if_match
    try:
        result = client.profile.patch_work(slug, args.paper_id, patch, base_hash=base_hash)
    except AgentAPIError as e:
        print(f"Error: {e.detail}", file=sys.stderr)
        return EXIT_ERROR
    if args.as_json:
        print(json.dumps(result, indent=2))
        return EXIT_OK
    print(f"{args.paper_id}: updated {', '.join(result.get('updated', []))}")
    return EXIT_OK


def _work_rm(client, slug: str, args: argparse.Namespace) -> int:
    """Remove one work from the corpus."""
    from .auth.agent import AgentAPIError

    try:
        client.profile.delete_work(slug, args.paper_id)
    except AgentAPIError as e:
        print(f"Error: {e.detail}", file=sys.stderr)
        return EXIT_ERROR
    print(f"{args.paper_id}: removed")
    return EXIT_OK


_WORK_HANDLERS: dict[str, Callable[..., int]] = {
    "show": _work_show,
    "set": _work_set,
    "rm": _work_rm,
}


def _cmd_work(args: argparse.Namespace) -> int:
    """Handle the ``work`` verb group (agent-side editing of one work)."""
    from .auth.agent import CredentialError, ManagementClient, resolve_credential

    if not args.work_cmd:
        print("rp work: subcommand required (show, set, rm)", file=sys.stderr)
        return EXIT_USAGE

    try:
        cred = resolve_credential(host=args.host)
    except CredentialError as e:
        print(str(e), file=sys.stderr)
        return EXIT_ERROR

    slug = cred.profile
    if not slug:
        print("No profile configured. Set 'profile' in credentials.toml.", file=sys.stderr)
        return EXIT_ERROR

    handler = _WORK_HANDLERS.get(args.work_cmd)
    if handler is None:
        print(f"rp work: unknown subcommand {args.work_cmd!r}", file=sys.stderr)
        return EXIT_USAGE
    return handler(ManagementClient(cred), slug, args)


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "work": _cmd_work,
}
