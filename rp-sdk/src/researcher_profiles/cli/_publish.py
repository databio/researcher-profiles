"""``rp render``, ``rp site`` and ``rp push``: the three ways a profile goes out.

``render`` writes a profile's own HTML in place, ``site`` writes the collection
files for a set of profiles, and ``push`` uploads a built directory to a remote
API server.
"""

import argparse
import json
import sys
from collections.abc import Callable

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
        "rp push ./profiles/voss-elena --url http://localhost:8109 --json",
        extra="With no --url, pushes to the server you ran `rp login` against.\n",
    )
    p_push.add_argument("profile", help="A built profile directory, a rid, or a slug")
    _add_root(p_push, "Profiles root (for a rid/slug)")
    _add_url(p_push, "Server base URL (default: the server you ran `rp login` against)")
    p_push.add_argument(
        "--slug", default=None, help="Target slug (default: profile directory name)"
    )
    _add_token(p_push)
    add_json(p_push, "Emit the server's summary as JSON")
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


def _cmd_push(args: argparse.Namespace) -> int:
    """Upload a built profile directory to a remote API server."""
    import httpx

    from ..client import push_profile

    target = resolve_profile_arg("push", args.profile, args.root)
    if target is None:
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
    try:
        summary = push_profile(
            url,
            target,
            slug=args.slug,
            token=token,
            include_fulltext=args.include_fulltext,
        )
    except httpx.HTTPError as e:
        # The server could not be reached or would not answer. That is a
        # general error (1), not a usage error (2): nothing was mistyped.
        print(f"push failed: {url}: {e}", file=sys.stderr)
        print(
            f"Check the server is up and the URL is the API base:\n  rp listr --url {url}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except (FileNotFoundError, PermissionError, RuntimeError) as e:
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
    if args.as_json:
        print(json.dumps(summary, indent=2, default=str))
        return EXIT_OK
    print(
        f"pushed {summary.get('slug')} ({summary.get('name')}, "
        f"level={summary.get('level')}, indexed={summary.get('indexed')})"
    )
    return EXIT_OK


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "render": _cmd_render,
    "site": _cmd_site,
    "push": _cmd_push,
}
