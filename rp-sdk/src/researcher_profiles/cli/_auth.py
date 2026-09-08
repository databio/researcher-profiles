"""``rp login`` / ``logout`` / ``whoami``: one person's own push credential."""

import argparse
import json
import sys
from collections.abc import Callable

from ._shared import (
    _CREDENTIALS_HINT,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    _add_token,
    _add_url,
    _remote_target,
    add_json,
    add_subcommand,
)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register this group's verbs on the root subparser action."""
    p_login = add_subcommand(
        sub,
        "login",
        "Log in to a profile server from the command line and store a push key for it",
        "rp login https://profiles.example.org",
        "rp login http://localhost:8109 --no-browser",
        extra=(
            "Prints a link to open in your browser; approving it there mints a key\n"
            "that can push only the profiles you own or edit. The key is stored in\n"
            f"{_CREDENTIALS_HINT} and used by rp push / install / listr / whoami.\n"
            "With no argument, logs in again to the server already stored.\n"
        ),
    )
    p_login.add_argument(
        "server",
        nargs="?",
        default=None,
        help="Server base URL (default: the server already logged in to)",
    )
    p_login.add_argument(
        "--label",
        default=None,
        help="Name for this machine on the approve page (default: hostname)",
    )
    p_login.add_argument(
        "--no-browser", action="store_true", help="Print the link only; do not open a browser"
    )
    add_json(p_login, "Emit {url, orcid, name} as JSON")

    add_subcommand(
        sub,
        "logout",
        "Forget the stored login",
        "rp logout",
    )

    p_whoami = add_subcommand(
        sub,
        "whoami",
        "Show who the stored login is and which profiles it may push",
        "rp whoami",
        "rp whoami --json",
    )
    _add_url(p_whoami, "Server base URL (default: the login's)")
    _add_token(p_whoami)
    add_json(p_whoami, "Emit the server's identity record as JSON")


def _cmd_login(args: argparse.Namespace) -> int:
    """Log in to a registry via the device flow and store the resulting key."""
    from .auth.credentials import LoginError, load_login, login, save_login

    stored = load_login()
    url = args.server or (stored.url if stored else None)
    if not url:
        print(
            "no server URL given:\n  rp login https://profiles.example.org",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        result = login(url, label=args.label, open_browser=not args.no_browser)
    except LoginError as e:
        print(f"login failed: {e}", file=sys.stderr)
        return EXIT_ERROR
    path = save_login(result)
    if args.as_json:
        print(json.dumps({"url": result.url, "orcid": result.orcid, "name": result.name}, indent=2))
        return EXIT_OK
    who = result.name or result.orcid or "you"
    print(f"logged in to {result.url} as {who}; key stored in {path}", file=sys.stderr)
    print("Push your profile with:  rp push <profile-dir>", file=sys.stderr)
    return EXIT_OK


def _cmd_logout(_args: argparse.Namespace) -> int:
    """Delete the stored login."""
    from .auth.credentials import clear_login, credentials_path

    if clear_login():
        print(f"logged out; removed {credentials_path()}")
    else:
        print("not logged in (nothing stored)")
    return EXIT_OK


def _cmd_whoami(args: argparse.Namespace) -> int:
    """Ask the server who the stored (or given) token is."""
    from .auth.credentials import LoginError, whoami

    url, token = _remote_target(args.url, args.token)
    if not url or not token:
        print("not logged in. Run:\n  rp login https://profiles.example.org", file=sys.stderr)
        return EXIT_USAGE
    try:
        record = whoami(url, token)
    except LoginError as e:
        print(f"whoami failed: {e}", file=sys.stderr)
        return EXIT_ERROR
    if args.as_json:
        print(json.dumps({"url": url, **record}, indent=2))
        return EXIT_OK
    owner = record.get("owner") or {}
    who = owner.get("name") or owner.get("orcid") or record.get("consumer") or "unknown"
    print(
        f"{url}: {who}"
        + (f" ({owner['orcid']})" if owner.get("orcid") and owner.get("name") else "")
    )
    print(f"scopes: {', '.join(record.get('scopes') or []) or '-'}")
    profiles = record.get("profiles") or []
    if profiles:
        print("can push:")
        for prof in profiles:
            print(f"  {prof.get('slug') or '-'}\t{prof.get('rid')}\t{prof.get('role')}")
    elif owner:
        print("can push: nothing yet (claim your profile on the server first)")
    return EXIT_OK


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "login": _cmd_login,
    "logout": _cmd_logout,
    "whoami": _cmd_whoami,
}
