"""``rp render``, ``rp site`` and ``rp push``: the three ways a profile goes out.

``render`` writes a profile's own HTML in place, ``site`` writes the collection
files for a set of profiles, and ``push`` uploads a built directory to a remote
API server.
"""

import argparse
import json
import sys
from collections.abc import Callable

# ``utils.paths`` is stdlib-only by contract (see its package docstring), so
# naming the retired cache directory here costs the parser nothing.
from ..utils.paths import LEGACY_CACHE_DIRNAME
from ._shared import (
    _EG_SLUG,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    _add_root,
    _add_token,
    _add_url,
    _remote_target,
    add_json,
    add_subcommand,
    resolve_profile_arg,
)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register this group's verbs on the root subparser action."""
    p_render = add_subcommand(
        sub,
        "render",
        "Render index.html into a profile folder, refresh its manifest and "
        ".publishignore (in place; no transform, no separate output tree)",
        f"rp render {_EG_SLUG}",
        "rp render ./profiles/voss-elena --base-url https://profiles.example.org",
        "rp render ./profiles/voss-elena --no-index",
    )
    p_render.add_argument("profile", help="A profile directory, a rid, or a slug")
    _add_root(p_render, "Profiles root (for a rid/slug)")
    p_render.add_argument("--base-url", default=None, help="Base URL for links")
    p_render.add_argument("--no-index", action="store_true", help="Add noindex directives")

    p_site = add_subcommand(
        sub,
        "site",
        "Write the collection files (index.json, by-rid.json, SKILL.md, "
        "sitemap.xml, ...) for a set of profiles into an output directory",
        "rp site ~/researcher-profiles --out ./_site",
        "rp site ~/researcher-profiles -o ./_site --base-url https://profiles.example.org",
        "rp site ~/researcher-profiles -o ./_site --no-index",
    )
    p_site.add_argument("profiles_dir", help="Root directory containing profile subdirectories")
    # ``-o`` is the Unix spelling for an output location and costs nothing to
    # accept; ``--out`` stays the primary name because every existing script and
    # doc uses it.
    p_site.add_argument(
        "-o", "--out", required=True, help="Output directory for the collection files"
    )
    p_site.add_argument(
        "--base-url",
        default=None,
        help="Public base URL of the site (canonical links, sitemap, robots, catalog ids)",
    )
    p_site.add_argument("--no-index", action="store_true", help="Add noindex directives")
    p_site.add_argument(
        "--now",
        default=None,
        metavar="ISO8601",
        help="Pin timestamps for deterministic output",
    )

    p_push = add_subcommand(
        sub,
        "push",
        "Upload a profile directory to a remote API server",
        f"rp push {_EG_SLUG}",
        f"rp push {_EG_SLUG} --url https://profiles.example.org",
        f"rp push {_EG_SLUG} --dry-run",
        "rp push ./profiles/voss-elena --url http://localhost:8109 --json",
        extra=(
            "With no --url, pushes to the server you ran `rp login` against.\n"
            "\n"
            "Every push diffs the archive against the server first and refuses\n"
            "to delete anything unless you say --force.\n"
        ),
    )
    p_push.add_argument("profile", help="A built profile directory, a rid, or a slug")
    _add_root(p_push, "Profiles root (for a rid/slug)")
    _add_url(p_push, "Server base URL (default: the server you ran `rp login` against)")
    p_push.add_argument(
        "--slug", default=None, help="Target slug (default: profile directory name)"
    )
    _add_token(p_push)
    add_json(p_push, "Emit the server's summary as JSON (with --dry-run, the whole plan)")
    p_push.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what the push would change and stop. Uploads nothing.",
    )
    p_push.add_argument(
        "--force",
        action="store_true",
        help="Push even though it removes files the server holds.",
    )
    p_push.add_argument(
        "--only",
        nargs="+",
        default=None,
        metavar="PATH",
        help=(
            "Send only these profile-relative files. profile.jsonld travels "
            "too, but its manifest is taken from the server, so nothing else "
            "changes. Inline sections (name, expertise) still come from the "
            "local document; to change one field only, use `rp work patch`."
        ),
    )
    p_push.add_argument(
        "--include-fulltext",
        action="store_true",
        help=(
            "Include extracted paper fulltext (sources/papers/) in the upload. "
            "Off by default: that text is derived from publisher-copyrighted "
            "works. Use only for a destination entitled to hold it; the server "
            "still strips it unless it runs with accept_fulltext enabled."
        ),
    )
    p_push.add_argument(
        "--merge",
        action="store_true",
        help=(
            "Keep every server-side file this push does not carry, not just "
            "fulltext and the index (implied by --only)."
        ),
    )
    p_push.add_argument(
        "--prune",
        action="store_true",
        help=(
            "Delete server-side fulltext and index files this push does not "
            "carry (default: keep them)."
        ),
    )


def _cmd_render(args: argparse.Namespace) -> int:
    """Render index.html into a profile folder, in place."""
    from ..publish import render_profile

    target = resolve_profile_arg("render", args.profile, args.root)
    if target is None:
        return EXIT_USAGE
    try:
        render_profile(
            target,
            base_url=args.base_url,
            no_index=args.no_index,
        )
    except FileNotFoundError as e:
        print(f"render failed: {e}", file=sys.stderr)
        return EXIT_USAGE
    print(f"rendered {target}")
    return EXIT_OK


def _cmd_site(args: argparse.Namespace) -> int:
    """Write the collection files for a set of profiles into an output directory."""
    from ..publish import build_site

    try:
        result = build_site(
            args.profiles_dir,
            args.out,
            base_url=args.base_url,
            no_index=args.no_index,
            now=args.now,
        )
    except FileNotFoundError as e:
        print(f"site failed: {e}", file=sys.stderr)
        return EXIT_USAGE
    for w in getattr(result, "warnings", None) or []:
        print(f"warning: {w}", file=sys.stderr)
    return EXIT_OK


#: Longest per-group path list a human-readable dry run prints before it
#: summarizes the rest. ``--json`` is the way to see all of them.
_PLAN_PATH_CAP = 20

#: What each archive-builder drop reason means to somebody reading a push.
#: ``legacy_cache`` never reaches here (``push_profile`` refuses it outright),
#: but a reason with no sentence still gets one.
_DROP_WARNINGS = {
    "not_in_spec": "are not profile members and did not ship",
    "legacy_cache": f"are under the retired {LEGACY_CACHE_DIRNAME}/ directory and did not ship",
}


def _count_line(group: str, paths_by_role: dict[str, list[str]]) -> str:
    """One plan group as ``  added      12  paper_summary 8, works 4``."""
    n = sum(len(paths) for paths in paths_by_role.values())
    roles = ", ".join(f"{role} {len(paths)}" for role, paths in sorted(paths_by_role.items()))
    return f"  {group:<10}{n:>4}" + (f"  {roles}" if roles else "")


def _path_block(group: str, paths: list[str]) -> list[str]:
    """One group's paths under a ``group (n):`` heading, capped."""
    if not paths:
        return []
    lines = [f"{group} ({len(paths)}):"] + [f"  {p}" for p in paths[:_PLAN_PATH_CAP]]
    if len(paths) > _PLAN_PATH_CAP:
        lines.append(f"  ... and {len(paths) - _PLAN_PATH_CAP} more")
    return lines


def _push_mode(args: argparse.Namespace) -> str | None:
    """The push mode the flags name, or ``None`` when they contradict.

    ``--only`` means merge: naming a file to send has never meant "and delete
    the rest of the profile". ``--prune`` is the opposite instruction, so
    pairing it with either keep flag is a mistake worth stopping on.
    """
    if args.prune and (args.only or args.merge):
        return None
    if args.prune:
        return "prune"
    if args.merge or args.only:
        return "merge"
    return "replace"


def _manifest_line(plan) -> str:
    """The one line that says whether the profile is about to get smaller.

    First, before the group counts, because it is the answer to the question a
    reader actually has. ``removed 0`` was true of the push that deleted 53
    artifacts; ``141 entries -> 88`` would not have been.
    """
    counts = plan.counts()
    detail = f"{counts['removed']} removed"
    if counts["respliced"]:
        detail += f", {counts['respliced']} respliced"
    return (
        f"manifest: {plan.manifest_before} entries on server -> "
        f"{plan.manifest_after} after push ({detail})"
    )


def _plan_report(plan, url: str, mode: str) -> list[str]:
    """The human dry-run report: the manifest line, counts, then the paths.

    ``unchanged`` and ``kept`` get a count and no listing: they are the part of
    the profile nothing is about to happen to, and listing them would bury the
    three groups that matter.
    """
    from ..client import PLAN_GROUPS

    state = "update" if plan.exists else "create"
    lines = [f"dry run: push {plan.slug} -> {url} ({state}, {mode})", _manifest_line(plan)]
    lines += [_count_line(g, getattr(plan, g)) for g in PLAN_GROUPS]
    for group in ("changed", "added", "removed", "respliced"):
        lines += _path_block(group, plan.paths(group))
    return lines


def _warn_dropped(dropped: dict[str, list[str]]) -> None:
    """Say out loud, on stderr, what the archive builder left behind.

    Withheld fulltext is a count and not a warning: it is the default, it is
    the copyright-safe answer, and flagging it every push would train the
    reader to ignore the lines that do mean something.
    """
    fulltext = dropped.get("fulltext") or []
    if fulltext:
        print(
            f"withheld {len(fulltext)} fulltext file(s) under sources/papers/ "
            "(--include-fulltext to send them)",
            file=sys.stderr,
        )
    for reason, paths in sorted(dropped.items()):
        if reason == "fulltext" or not paths:
            continue
        what = _DROP_WARNINGS.get(reason, "did not ship")
        shown = ", ".join(paths[:5]) + (", ..." if len(paths) > 5 else "")
        print(f"warning: {len(paths)} path(s) {what}: {shown}", file=sys.stderr)


def _cmd_push(args: argparse.Namespace) -> int:
    """Upload a built profile directory to a remote API server."""
    import httpx

    from ..client import PushRefused, PushWouldRemove, push_profile

    target = resolve_profile_arg("push", args.profile, args.root)
    if target is None:
        return EXIT_USAGE
    mode = _push_mode(args)
    if mode is None:
        print(
            "--prune deletes what the push does not carry; --only and --merge keep it. Pick one.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    url, token = _remote_target(args.url, args.token)
    if not url:
        print(
            "no server URL given. Name one, or log in once:\n"
            "  rp push <profile> --url https://profiles.example.org\n"
            "  rp login https://profiles.example.org",
            file=sys.stderr,
        )
        return EXIT_USAGE
    # Both halves of "what is about to overwrite what", named before anything
    # moves and on every run, dry or not. A push that turned out to have read
    # the wrong directory is only obvious afterwards if it said which one.
    slug = args.slug or target.name
    print(f"source: {target}", file=sys.stderr)
    print(f"target: {url}/api/v1/profiles/{slug} (mode: {mode})", file=sys.stderr)
    try:
        result = push_profile(
            url,
            target,
            slug=args.slug,
            token=token,
            include_fulltext=args.include_fulltext,
            mode=mode,
            only=args.only,
            force=args.force,
            dry_run=args.dry_run,
        )
    except PushWouldRemove as e:
        _warn_dropped(e.plan.dropped)
        print(f"push refused: {e}", file=sys.stderr)
        print(_manifest_line(e.plan), file=sys.stderr)
        for line in _path_block("removed", e.plan.paths("removed")):
            print(line, file=sys.stderr)
        if e.plan.shrinks and not e.plan.paths("removed"):
            # Nothing landed in ``removed``, so the usual three-flag hint is
            # the wrong advice: the manifest being pushed is the problem.
            print(
                "The local manifest lists fewer artifacts than the server does. "
                "That is usually a\npartial copy of the profile, not a deletion "
                "you meant: check `rp where` and push\nfrom the directory that "
                "built it, or --force if the entries really should go.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        print(
            "re-run with --merge to keep them, --force to remove them, "
            "or --prune to also drop kept fulltext/index",
            file=sys.stderr,
        )
        return EXIT_USAGE
    except PushRefused as e:
        print(f"push refused: {e}", file=sys.stderr)
        return EXIT_USAGE
    except httpx.HTTPError as e:
        # The server could not be reached or would not answer. That is a
        # general error (1), not a usage error (2): nothing was mistyped.
        print(f"push failed: {url}: {e}", file=sys.stderr)
        print(
            f"Check the server is up and the URL is the API base:\n  rp listr --url {url}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except (FileNotFoundError, PermissionError, RuntimeError, ValueError) as e:
        print(f"push failed: {e}", file=sys.stderr)
        print(
            "Check the profile is built and the target accepts you:\n"
            f"  rp validate {args.profile}\n"
            f"  rp login {url}                (push your own profile)\n"
            "  rp push ... --token <token>   (or export "
            "RESEARCHER_PROFILES_TOKEN)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    _warn_dropped(result.plan.dropped)
    if args.dry_run:
        if args.as_json:
            print(json.dumps(result.plan.as_dict(), indent=2, default=str))
        else:
            print("\n".join(_plan_report(result.plan, url, result.mode)))
        return EXIT_OK
    summary = result.summary or {}
    if args.as_json:
        print(json.dumps(summary, indent=2, default=str))
        return EXIT_OK
    counts = result.plan.counts()
    line = (
        f"pushed {summary.get('slug')}: "
        f"+{counts['added']} ~{counts['changed']} -{counts['removed']}"
    )
    kept = sum((summary.get("kept") or {}).values())
    spliced = summary.get("spliced") or 0
    notes = [f"kept {kept}"] if kept else []
    if spliced:
        notes.append(f"respliced {spliced}")
    if notes:
        line += f" ({', '.join(notes)})"
    print(
        f"{line} name={summary.get('name')}, level={summary.get('level')}, "
        f"indexed={summary.get('indexed')}"
    )
    _report_manifest_counts(summary, result.plan)
    return EXIT_OK


def _report_manifest_counts(summary: dict, plan) -> None:
    """Say what the server holds now, and shout if that is less than before.

    The server's own post-commit count, not the client's prediction: the point
    is to catch the case where the two disagree. A push whose artifact count
    fell is reported on stderr as well, because by this point the bytes are
    already gone and a line buried in stdout is not a warning.
    """
    counts = summary.get("manifest_counts") or {}
    if not counts:
        return
    total = sum(counts.values())
    roles = ", ".join(f"{role} {n}" for role, n in sorted(counts.items()))
    if plan.exists and total < plan.manifest_before:
        print(
            f"warning: artifact count fell from {plan.manifest_before} to {total}",
            file=sys.stderr,
        )
    print(f"server now holds {total} artifacts: {roles}")


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "render": _cmd_render,
    "site": _cmd_site,
    "push": _cmd_push,
}
