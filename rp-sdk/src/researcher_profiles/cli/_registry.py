"""``rp install`` / ``seek`` / ``list`` / ``listr``: the local cache over a registry."""

import argparse
import json
import os
import sys
from collections.abc import Callable

from ..store.config import PROFILES_ROOT_ENV_VAR
from ._shared import (
    _EG_SLUG,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    _add_root,
    _add_token,
    _add_url,
    _no_such_profile,
    add_json,
    add_subcommand,
)

# Not imported from ``..client.REGISTRY_ENV_VAR``: that package pulls in numpy
# at import time (``embeddings.flat``), and this module is imported eagerly to
# build the ``rp`` parser for every invocation, including ones (``rp where``)
# that must stay numpy-free. Literal here, lazily imported (with the real
# value) inside ``_registry_target`` below, same split the module already
# used before this rename.
_REGISTRY_URL_ENV_VAR_NAME = "RESEARCHER_PROFILES_REGISTRY_URL"
_REGISTRY_URL_HELP = (
    f"Registry base URL(s), comma-separated (default: {_REGISTRY_URL_ENV_VAR_NAME}, "
    "else the `rp login` server)"
)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register this group's verbs on the root subparser action."""
    p_install = add_subcommand(
        sub,
        "install",
        "Pull profiles from a registry into the local cache",
        f"rp install {_EG_SLUG}",
        "rp install voss-elena jane-doe --url https://profiles.example.org",
        f"rp install {_EG_SLUG} --force --json",
    )
    p_install.add_argument("slug", nargs="+", help="Profile slug(s) to install")
    _add_url(p_install, _REGISTRY_URL_HELP)
    _add_root(
        p_install,
        f"Local profiles root (default: {PROFILES_ROOT_ENV_VAR}, else ~/researcher-profiles)",
    )
    _add_token(p_install)
    p_install.add_argument(
        "--force", action="store_true", help="Re-download even if already cached"
    )
    add_json(p_install, "Emit one JSON record per requested slug")

    p_seek = add_subcommand(
        sub,
        "seek",
        "Print the local path of an installed profile",
        "rp seek voss-elena",
        "cat $(rp seek voss-elena)/profile.jsonld",
        f"rp seek {_EG_SLUG} --json",
        extra=(
            "`seek` takes a directory slug only. To resolve an ORCID or a rid,\nuse `rp where`.\n"
        ),
    )
    p_seek.add_argument("slug", help="Directory slug of an installed profile")
    _add_root(p_seek, "Local profiles root")
    add_json(p_seek, "Emit {slug, path, root} as JSON")

    p_list = add_subcommand(
        sub,
        "list",
        "List profiles in the local cache",
        "rp list",
        "rp list --json",
        "rp list --root ~/work/som/profiles",
    )
    _add_root(p_list, "Local profiles root")
    add_json(p_list, "Emit {root, profiles} as JSON")

    p_listr = add_subcommand(
        sub,
        "listr",
        "List profiles available on a registry",
        "rp listr --url https://profiles.example.org",
        f"{_REGISTRY_URL_ENV_VAR_NAME}=https://profiles.example.org rp listr --json",
    )
    _add_url(p_listr, _REGISTRY_URL_HELP)
    _add_token(p_listr)
    add_json(p_listr, "Emit one JSON record per remote profile")


def _registry_target(url_flag: str | None, token_flag: str | None) -> tuple[str | None, str | None]:
    """``(url, token)`` for a registry command, where ``url`` may name several servers.

    ``RESEARCHER_PROFILES_REGISTRY_URL`` keeps its precedence over the stored
    login for the URL; the stored key is offered only when the login's server
    is among the registries actually being asked.
    """
    from ..client import REGISTRY_ENV_VAR, resolve_registries
    from .auth.credentials import TOKEN_ENV_VAR, load_login, server_base

    login = load_login()
    url = url_flag or os.environ.get(REGISTRY_ENV_VAR) or (login.url if login else None)
    if token_flag:
        return url, token_flag
    env = os.environ.get(TOKEN_ENV_VAR) or None
    if env:
        return url, env
    servers = {server_base(u) for u in resolve_registries(url)}
    if login and login.url in servers:
        return url, login.token
    return url, None


def _cmd_install(args: argparse.Namespace) -> int:
    """Pull profiles from a registry into the local cache."""
    from ..client import install_profile

    url, token = _registry_target(args.url, args.token)
    rc = 0
    records = []
    for slug in args.slug:
        try:
            summary = install_profile(
                slug,
                url=url,
                root=args.root,
                token=token,
                force=args.force,
            )
        except (ValueError, PermissionError, RuntimeError) as e:
            print(f"install failed for {slug}: {e}", file=sys.stderr)
            print(
                "Name a registry and, if it is private, a token:\n"
                "  rp install <slug> --url https://profiles.example.org\n"
                f"  export {_REGISTRY_URL_ENV_VAR_NAME}=... RESEARCHER_PROFILES_TOKEN=...\n"
                "  rp listr --url <registry>    # what that registry holds",
                file=sys.stderr,
            )
            rc = EXIT_USAGE
            continue
        records.append(summary)
        if args.as_json:
            # Progress belongs on stderr while stdout is carrying JSON.
            continue
        if summary["status"] == "present":
            print(f"{slug}: already installed at {summary['path']} (--force to replace)")
        else:
            ft = "with fulltext" if summary.get("fulltext") else "no fulltext"
            print(
                f"installed {summary['slug']} ({summary.get('name')}, "
                f"level={summary.get('level')}, {ft}) -> {summary['path']}"
            )
    if args.as_json:
        print(json.dumps(records, indent=2, default=str))
    return rc


def _cmd_seek(args: argparse.Namespace) -> int:
    """Print the local path of an installed profile, by directory slug."""
    from ..client import seek_profile
    from ..store.config import resolve_profiles_root

    root = resolve_profiles_root(args.root)
    try:
        path = seek_profile(args.slug, root=args.root)
    except FileNotFoundError as e:
        _no_such_profile(
            f"rp seek: {e}",
            root=root,
            explicit=args.root,
            next_steps=(
                ("rp list", "what this cache actually holds"),
                (f"rp where {args.slug}", "if that was a rid, not a slug"),
                (f"rp install {args.slug}", "pull it from a registry"),
            ),
        )
        return EXIT_USAGE
    if args.as_json:
        print(
            json.dumps(
                {
                    "slug": args.slug,
                    "path": str(path),
                    "root": str(root),
                },
                indent=2,
            )
        )
    else:
        print(path)
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    """List the profiles in the local cache."""
    from ..client import list_installed
    from ..store.config import resolve_profiles_root

    root = resolve_profiles_root(args.root)
    slugs = list_installed(args.root)
    if args.as_json:
        print(json.dumps({"root": str(root), "profiles": slugs}, indent=2))
    elif slugs:
        for slug in slugs:
            print(slug)
    else:
        print(f"no profiles installed in {root}")
    if not slugs:
        # An empty cache is almost always the wrong root rather than an
        # empty registry, so say which root and where it came from, on
        # stderr, so `rp list --json` stays a clean JSON document.
        _no_such_profile(
            root=root,
            explicit=args.root,
            next_steps=(
                ("rp install <slug> --url <registry>", "pull one in"),
                ("rp listr --url <registry>", "what a registry holds"),
            ),
        )
    return EXIT_OK


def _cmd_listr(args: argparse.Namespace) -> int:
    """List the profiles available on one or more registries."""
    from ..client import list_registry

    url, token = _registry_target(args.url, args.token)
    listings = list_registry(url, token=token)
    if not listings:
        print(
            "no registry URL given. Name one, or set it once:\n"
            "  rp listr --url https://profiles.example.org\n"
            f"  export {_REGISTRY_URL_ENV_VAR_NAME}=https://profiles.example.org",
            file=sys.stderr,
        )
        return EXIT_USAGE
    records = []
    reached = 0
    for listing in listings:
        if listing.error is not None:
            print(f"{listing.base_url}: {listing.error}", file=sys.stderr)
            print(
                "If that registry is private, give it a token:\n"
                "  rp listr --url <registry> --token <token>\n"
                "  export RESEARCHER_PROFILES_TOKEN=<token>",
                file=sys.stderr,
            )
            continue
        reached += 1
        for entry in listing.profiles:
            if args.as_json:
                # Which server answered is data too: two registries can hold
                # the same slug, and the human form loses that.
                records.append({"registry": listing.base_url, **entry})
                continue
            print(
                f"{entry['slug']}\t{entry.get('name')}\t"
                f"level={entry.get('level')}\tpapers={entry.get('paper_count')}"
            )
    if args.as_json:
        print(json.dumps(records, indent=2, default=str))
    if not reached:
        # Every named registry failed. Returning 0 with an empty list would
        # read to a caller checking $? exactly like "the registry is empty",
        # which is the one thing it is not.
        return EXIT_ERROR
    return EXIT_OK


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "install": _cmd_install,
    "seek": _cmd_seek,
    "list": _cmd_list,
    "listr": _cmd_listr,
}
