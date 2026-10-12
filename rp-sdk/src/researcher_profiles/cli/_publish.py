"""``rp render``, ``rp site``, ``rp publish`` and ``rp push``: how a profile goes out."""

import argparse
import json
import sys
from collections.abc import Callable

# ``utils.paths`` is stdlib-only, so this import keeps the parser cheap.
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
        "Render index.html into a profile folder and refresh its manifest "
        "(in place; to deploy, use rp publish)",
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

    p_publish = add_subcommand(
        sub,
        "publish",
        "Write the static site one audience may see (public by default) into a "
        "folder that any sync tool can upload as is",
        "rp publish ~/researcher-profiles --out ./_site",
        "rp publish ~/researcher-profiles --who limited --out ./_lab",
        f"rp publish {_EG_SLUG} --out ./_one --dry-run",
        extra=(
            "Upload is a separate step; the folder needs no filtering:\n"
            "  aws s3 sync ./_site s3://my-bucket/ --delete\n"
            "  aws s3 sync ./_site s3://my-bucket/ --delete "
            "--endpoint-url https://<account>.r2.cloudflarestorage.com\n"
            "  rclone sync ./_site remote:my-bucket\n"
            "The folder also holds .rp-publish.json, which records the audience "
            "and the file list\nand is uploaded with the rest; it holds nothing "
            "beyond paths already in the tree.\n"
        ),
    )
    p_publish.add_argument(
        "profiles", help="A profiles root, or one profile (a directory, a rid, or a slug)"
    )
    _add_root(p_publish, "Profiles root (for a rid/slug)")
    p_publish.add_argument(
        "-o", "--out", required=True, help="Output folder (created, or a previous rp publish)"
    )
    p_publish.add_argument(
        "--who",
        choices=("public", "limited", "private"),
        default="public",
        help="The audience: what this tier may see is exactly what is written (default: public)",
    )
    p_publish.add_argument(
        "--base-url",
        default=None,
        help="Public base URL of the site (canonical links, sitemap, robots, catalog ids)",
    )
    p_publish.add_argument("--no-index", action="store_true", help="Add noindex directives")
    p_publish.add_argument(
        "--now",
        default=None,
        metavar="ISO8601",
        help="Pin timestamps for deterministic output",
    )
    p_publish.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what ships and what is withheld, and why. Writes nothing.",
    )
    p_publish.add_argument(
        "--change-audience",
        action="store_true",
        help="Allow --who to be wider than the audience the folder was last published for",
    )
    add_json(p_publish, "Emit the export plan and result as JSON")

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


def _resolve_publish_source(ref: str, root: str | None):
    """A profiles root as given, or one profile resolved like any other verb."""
    from pathlib import Path

    path = Path(ref).expanduser()
    if path.is_dir():
        return path
    return resolve_profile_arg("publish", ref, root)


def _cmd_publish(args: argparse.Namespace) -> int:
    """Write the static tree one audience may see."""
    from pydantic import ValidationError

    from ..publish import PublishError, publish_collection

    source = _resolve_publish_source(args.profiles, args.root)
    if source is None:
        return EXIT_USAGE
    if args.who == "private":
        print(
            "warning: --who private exports everything, including what only the "
            "owner may see. Never upload it anywhere others can read.",
            file=sys.stderr,
        )
    try:
        result = publish_collection(
            source,
            args.out,
            viewer=args.who,
            base_url=args.base_url,
            no_index=args.no_index,
            now=args.now,
            dry_run=args.dry_run,
            change_audience=args.change_audience,
        )
    except (PublishError, ValidationError) as e:
        print(f"publish refused: {e}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as e:
        print(f"publish failed: {e}", file=sys.stderr)
        return EXIT_USAGE
    for w in result.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if args.as_json:
        print(json.dumps(result.as_dict(), indent=2))
        return EXIT_OK
    verb = "would write" if args.dry_run else "wrote"
    for p in result.profiles:
        print(f"{p.slug}: {len(p.files)} file(s), {len(p.withheld)} withheld")
        if args.dry_run:
            for rel in p.files:
                print(f"  + {rel}")
            for rel, why in sorted(p.withheld.items()):
                print(f"  - {rel}  ({why})")
    for slug, why in sorted(result.skipped.items()):
        print(f"{slug}: skipped ({why})")
    for rel in result.removed:
        print(f"removed {rel}")
    for rel in result.would_remove:
        print(f"would remove {rel}")
    print(f"{verb} {len(result.profiles)} profile(s) for --who {result.viewer} to {result.out_dir}")
    return EXIT_OK


#: Longest per-group path list a human-readable dry run prints before it
#: summarizes the rest. ``--json`` is the way to see all of them.
_PLAN_PATH_CAP = 20

#: What each archive-builder drop reason means to somebody reading a push.
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

    ``--only`` means merge. ``--prune`` with either keep flag contradicts.
    """
    if args.prune and (args.only or args.merge):
        return None
    if args.prune:
        return "prune"
    if args.merge or args.only:
        return "merge"
    return "replace"


def _manifest_line(plan) -> str:
    """The one line that says whether the profile is about to get smaller (printed first)."""
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

    ``unchanged`` and ``kept`` get a count and no listing.
    """
    from ..client import PLAN_GROUPS

    state = "update" if plan.exists else "create"
    lines = [f"dry run: push {plan.slug} -> {url} ({state}, {mode})", _manifest_line(plan)]
    lines += [_count_line(g, getattr(plan, g)) for g in PLAN_GROUPS]
    for group in ("changed", "added", "removed", "respliced"):
        lines += _path_block(group, plan.paths(group))
    return lines


def _warn_dropped(dropped: dict[str, list[str]]) -> None:
    """Warn on stderr about what the archive builder left behind.

    Withheld fulltext is the default, so it is a count, not a warning.
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


def _report_refusal(e) -> None:
    """``push refused [code]: what``, the detail lines, then ``next: fix``."""
    print(f"push refused [{e.code}]: {e}", file=sys.stderr)
    for line in e.details:
        print(f"  {line}", file=sys.stderr)
    if e.fix:
        print(f"  next: {e.fix}", file=sys.stderr)


def _cmd_push(args: argparse.Namespace) -> int:
    """Upload a built profile directory to a remote API server."""
    import httpx

    from ..client import PushInsufficientAccess, PushRefused, PushWouldRemove, push_profile

    target = resolve_profile_arg("push", args.profile, args.root)
    if target is None:
        return EXIT_USAGE
    mode = _push_mode(args)
    if mode is None:
        print("push: --prune conflicts with --only/--merge; pick one", file=sys.stderr)
        return EXIT_USAGE
    url, token = _remote_target(args.url, args.token)
    if not url:
        print(
            "push failed [no-url]: no server URL\n  next: --url <base>, or `rp login <base>` once",
            file=sys.stderr,
        )
        return EXIT_USAGE
    # Name source and target on every run, so a wrong directory is visible.
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
    except PushRefused as e:
        if isinstance(e, PushWouldRemove):
            _warn_dropped(e.plan.dropped)
        _report_refusal(e)
        # Exit 1 for the server's part-by-part refusal: nothing was mistyped.
        return EXIT_ERROR if isinstance(e, PushInsufficientAccess) else EXIT_USAGE
    except httpx.HTTPError as e:
        print(
            f"push failed [unreachable]: {url}: {e}\n"
            f"  next: `rp listr --url {url}` to check the server is up and the URL is the API base",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except (FileNotFoundError, PermissionError, RuntimeError, ValueError) as e:
        print(
            f"push failed: {e}\n"
            f"  next: `rp validate {args.profile}`; for auth, `rp login {url}` "
            "or --token <token> (or RESEARCHER_PROFILES_TOKEN)",
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

    Uses the server's post-commit count, not the client's prediction, to catch
    a disagreement. A drop is also reported on stderr.
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
    "publish": _cmd_publish,
    "push": _cmd_push,
}
