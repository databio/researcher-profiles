"""``rp graph`` and ``rp db``: the derived graph and the SQL profile store."""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from ..store.config import DATABASE_URL_ENV_VAR, PROFILES_ROOT_ENV_VAR
from ._shared import (
    _EG_SLUG,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    _add_root,
    add_json,
    add_subcommand,
    profile_dir_for_ref,
    root_resolution_note,
)


def _db_common(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """The two flags every ``rp db`` subcommand carries."""
    parser.add_argument(
        "--database-url",
        default=None,
        dest="database_url",
        help=f"Store URL (default: ${DATABASE_URL_ENV_VAR})",
    )
    add_json(parser, "Emit the result as JSON")
    return parser


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register this group's verbs on the root subparser action."""
    p_graph = add_subcommand(
        sub,
        "graph",
        "Build/inspect the derived profile graph (coauthor / COI / advising edges)",
        "rp graph build",
        "rp graph build --root ~/researcher-profiles --json",
    )
    graph_sub = p_graph.add_subparsers(dest="graph_cmd", required=True, metavar="<subcommand>")
    g_build = add_subcommand(
        graph_sub,
        "build",
        "Derive the graph from every profile in a store and write graph.sqlite",
        "rp graph build",
        "rp graph build --root ~/researcher-profiles",
    )
    _add_root(
        g_build,
        f"Profiles root to build over (default: {PROFILES_ROOT_ENV_VAR}, else ~/researcher-profiles)",
    )
    add_json(g_build, "Emit the build summary as JSON")

    p_db = add_subcommand(
        sub,
        "db",
        "Push, pull and inspect profiles in a SQL profile store",
        "rp db init",
        "rp db push --all",
        f"rp db pull {_EG_SLUG} --to ./pulled",
        "rp db list --json",
        extra=(
            "The store is a peer backend, not a projection: a profile in it is a\n"
            "profile. Pick one backend per deployment; nothing should read the\n"
            "same profile from a directory and from the store at once.\n"
            "\n"
            "Database URL, highest precedence first:\n"
            "  1. --database-url URL\n"
            f"  2. ${DATABASE_URL_ENV_VAR}\n"
            "There is no built-in default: a guessed database is a store written\n"
            "somewhere nobody meant.\n"
        ),
    )
    db_sub = p_db.add_subparsers(dest="db_cmd", required=True, metavar="<subcommand>")

    _db_common(add_subcommand(db_sub, "init", "Create the rp_* tables on the store", "rp db init"))

    d_push = _db_common(
        add_subcommand(
            db_sub,
            "push",
            "Load profile directories into the store",
            f"rp db push {_EG_SLUG}",
            "rp db push --all",
        )
    )
    d_push.add_argument("ref", nargs="*", help="Profile directories, slugs, or rids")
    d_push.add_argument(
        "--all", action="store_true", dest="all_profiles", help="Push every profile in the root"
    )
    _add_root(d_push, "Profiles root to read from (default: the resolved cache)")
    d_push.add_argument(
        "--include-binary",
        action="store_true",
        dest="include_binary",
        help="Store binary artifact bodies too (.cache/embeddings.sqlite can be tens of MB)",
    )

    d_pull = _db_common(
        add_subcommand(
            db_sub,
            "pull",
            "Write a stored profile back out as a directory",
            f"rp db pull {_EG_SLUG} --to ./pulled",
        )
    )
    d_pull.add_argument("ref", help="A rid or a slug")
    d_pull.add_argument("--to", default=None, help="Destination directory (default: ./<slug>)")
    d_pull.add_argument(
        "--with-build",
        action="store_true",
        dest="with_build",
        help="Also write the build sidecar (never part of the published record)",
    )

    _db_common(add_subcommand(db_sub, "list", "List the profiles in the store", "rp db list"))

    d_rm = _db_common(
        add_subcommand(db_sub, "rm", "Delete a profile and every child row", f"rp db rm {_EG_SLUG}")
    )
    d_rm.add_argument("ref", help="A rid or a slug")


def _cmd_graph(args: argparse.Namespace) -> int:
    """Build the derived profile graph over a store (``graph build``)."""
    from ..graph import ProfileGraph, graph_db_path
    from ..store.config import resolve_profiles_root

    if args.graph_cmd == "build":
        root = resolve_profiles_root(args.root)
        graph = ProfileGraph.build_and_save(root)
        path = graph_db_path(root)
        summary = {
            "root": str(root),
            "graph": str(path),
            "nodes": len(graph),
            "edges": len(graph.edges),
            "profiles": graph.meta.get("profile_count"),
        }
        if args.as_json:
            print(json.dumps(summary, indent=2))
        else:
            print(
                f"built graph over {summary['profiles']} profiles: "
                f"{summary['nodes']} nodes, {summary['edges']} edges -> {path}"
            )
        return EXIT_OK
    print(f"rp graph: unknown subcommand {args.graph_cmd!r}", file=sys.stderr)
    return EXIT_USAGE


def _db_init(store, url: str, args: argparse.Namespace) -> int:
    """Create the ``rp_*`` tables on the store."""
    store.create_all()
    if args.as_json:
        print(json.dumps({"database_url": url, "created": True}, indent=2))
    else:
        print(f"created the rp_* tables on {url}")
    return EXIT_OK


def _db_push(store, url: str, args: argparse.Namespace) -> int:
    """Load profile directories into the store."""
    from ..errors import ProfileError
    from ..profile import ResearcherProfile
    from ..store.config import resolve_profiles_root

    root = resolve_profiles_root(args.root)
    targets: list[Path] = []
    if args.all_profiles:
        targets = sorted(
            d for d in root.iterdir() if d.is_dir() and (d / "profile.jsonld").is_file()
        )
    for ref in args.ref:
        candidate = Path(ref)
        if not candidate.is_dir():
            candidate = profile_dir_for_ref(ref, args.root) or (root / ref)
        targets.append(candidate)
    if not targets:
        print("rp db push: nothing to push; name a profile or pass --all", file=sys.stderr)
        print(root_resolution_note(root, explicit=args.root), file=sys.stderr)
        return EXIT_USAGE

    pushed: list[dict] = []
    failed = 0
    for target in targets:
        try:
            prof = ResearcherProfile.from_files(target)
            rid = store.put(prof, include_binary=args.include_binary)
        except (ProfileError, FileNotFoundError, NotADirectoryError, OSError) as e:
            failed += 1
            print(f"rp db push: {target}: {e}", file=sys.stderr)
            continue
        pushed.append({"slug": prof.slug, "rid": rid, "path": str(target)})
    if args.as_json:
        print(json.dumps({"database_url": url, "pushed": pushed, "failed": failed}, indent=2))
    else:
        for entry in pushed:
            print(f"{entry['slug']:<24} {entry['rid']}")
        print(f"pushed {len(pushed)} profile(s) to {url}")
    return EXIT_ERROR if failed else EXIT_OK


def _db_pull(store, url: str, args: argparse.Namespace) -> int:
    """Write a stored profile back out as a directory."""
    from ..store import ProfileNotFoundError

    try:
        rid = store.rid_for(args.ref)
        with store.session() as s:
            from ..store.db import ProfileRow

            row = s.get(ProfileRow, rid)
            slug = row.slug if row is not None else rid
        dest = Path(args.to) if args.to else Path(slug)
        out = store.export_directory(rid, dest, with_build=args.with_build)
    except ProfileNotFoundError as e:
        print(f"rp db pull: {e}", file=sys.stderr)
        return EXIT_USAGE
    if args.as_json:
        print(
            json.dumps({"database_url": url, "rid": rid, "slug": slug, "path": str(out)}, indent=2)
        )
    else:
        print(f"pulled {slug} ({rid}) from {url} -> {out}")
    return EXIT_OK


def _db_list(store, url: str, args: argparse.Namespace) -> int:
    """List the profiles in the store."""
    rows = store.list_profiles()
    if args.as_json:
        print(
            json.dumps(
                {
                    "database_url": url,
                    "profiles": [
                        {
                            "rid": r.rid,
                            "slug": r.slug,
                            "name": r.name,
                            "level": r.level,
                            "date_modified": r.date_modified,
                            "content_hash": r.content_hash,
                        }
                        for r in rows
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"# {url}")
        for r in rows:
            print(f"{r.slug:<24} {r.rid:<24} {r.level:<6} {r.date_modified or '-':<26} {r.name}")
    return EXIT_OK


def _db_rm(store, url: str, args: argparse.Namespace) -> int:
    """Delete a profile and every child row."""
    from ..store import ProfileNotFoundError

    try:
        rid = store.delete(args.ref)
    except ProfileNotFoundError as e:
        print(f"rp db rm: {e}", file=sys.stderr)
        return EXIT_USAGE
    if args.as_json:
        print(json.dumps({"database_url": url, "rid": rid, "deleted": True}, indent=2))
    else:
        print(f"deleted {rid} from {url}")
    return EXIT_OK


#: One ``rp db`` subcommand -> the function that runs it against an open store.
_DB_HANDLERS: dict[str, Callable[..., int]] = {
    "init": _db_init,
    "push": _db_push,
    "pull": _db_pull,
    "list": _db_list,
    "rm": _db_rm,
}


def _cmd_db(args: argparse.Namespace) -> int:
    """Handle the ``db`` verb group (the SQL profile store).

    Every branch prints which database it acted on. A store command whose
    output does not name its store is one config file away from a confident
    report about the wrong database.
    """
    from ..store.config import DatabaseUrlNotConfigured, resolve_database_url

    try:
        url = resolve_database_url(args.database_url)
    except DatabaseUrlNotConfigured as e:
        print(f"rp db: {e}", file=sys.stderr)
        return EXIT_USAGE

    try:
        from ..store.sql import SqlProfileStore
    except ImportError as e:
        print(f"rp db: the profile store needs the `sql` extra: {e}", file=sys.stderr)
        print(
            "  Requires the 'sql' extra; see the install instructions in the README.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    handler = _DB_HANDLERS.get(args.db_cmd)
    if handler is None:
        print(f"rp db: unknown subcommand {args.db_cmd!r}", file=sys.stderr)
        return EXIT_USAGE
    return handler(SqlProfileStore(url), url, args)


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "graph": _cmd_graph,
    "db": _cmd_db,
}
