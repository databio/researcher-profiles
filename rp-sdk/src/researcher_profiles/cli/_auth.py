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
        "Log in to a profile server from the command line and store a push login for it",
        "rp login https://profiles.example.org",
        "rp login https://profiles.example.org --device",
        "rp login http://localhost:8109 --no-browser",
        extra=(
            "Opens your browser to sign in (OAuth with PKCE, back to a one-time\n"
            "address on this computer). Over SSH, with no display, or with --device,\n"
            "it prints a link and a code to approve on any device instead (when the\n"
            "server offers one). The login can push only the profiles you own or\n"
            f"edit, renews itself, and is stored in {_CREDENTIALS_HINT}, one per\n"
            "server; rp push / install / listr / whoami use it for its own server.\n"
            "With no argument, logs in again to the most recent server.\n"
        ),
    )
    p_login.add_argument(
        "server",
        nargs="?",
        default=None,
        help="Server base URL (default: the server most recently logged in to)",
    )
    p_login.add_argument(
        "--label",
        default=None,
        help="Name for this machine on the approve page and Privacy page (default: hostname)",
    )
    p_login.add_argument(
        "--device",
        action="store_true",
        help="Use a login code to approve on another device (automatic over SSH)",
    )
    p_login.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser here: open the printed link anywhere, then paste back "
        "the address the browser ends on",
    )
    add_json(p_login, "Emit {url, orcid, name} as JSON")

    p_logout = add_subcommand(
        sub,
        "logout",
        "Sign out: revoke the stored login at the server and forget it",
        "rp logout",
        "rp logout https://profiles.example.org",
    )
    p_logout.add_argument(
        "server",
        nargs="?",
        default=None,
        help="Server base URL (default: the server most recently logged in to)",
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
    """Log in to a registry (loopback or device flow) and store the login."""
    from .._vendor.snapgate_oauth_client import cli as oauth_cli
    from .auth.credentials import (
        LoginError,
        _default_label,
        cli_app,
        credentials_path,
        default_server,
        server_base,
    )

    url = args.server or default_server()
    if not url:
        print(
            "no server URL given:\n  rp login https://profiles.example.org",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        # Replaces any earlier login for this server; the old one is revoked
        # only after this one succeeded.
        cred = oauth_cli.login(
            cli_app(),
            server_base(url),
            browser=not args.no_browser,
            device=True if args.device else None,
            extra_params={"label": args.label or _default_label()},
        )
    except LoginError as e:
        print(f"login failed: {e}", file=sys.stderr)
        return EXIT_ERROR
    # The token response carries no identity; login asked the server (whoami).
    owner = cred.extra
    if args.as_json:
        print(
            json.dumps(
                {"url": cred.server, "orcid": owner.get("orcid"), "name": owner.get("name")},
                indent=2,
            )
        )
        return EXIT_OK
    who = owner.get("name") or owner.get("orcid") or "you"
    print(
        f"logged in to {cred.server} as {who}; login stored in {credentials_path()}",
        file=sys.stderr,
    )
    print("Push your profile with:  rp push <profile-dir>", file=sys.stderr)
    return EXIT_OK


def _cmd_logout(args: argparse.Namespace) -> int:
    """Revoke the stored login at the server, then forget it."""
    from .._vendor.snapgate_oauth_client import cli as oauth_cli
    from .auth.credentials import cli_app, credentials_path, default_server, server_base

    url = args.server or default_server()
    if not url:
        print("not logged in (nothing stored)")
        return EXIT_OK
    url = server_base(url)
    if not oauth_cli.logout(cli_app(), url):
        print(
            f"note: {url} did not confirm the sign-out; revoke 'rp CLI' on its Privacy "
            "page if you need to",
            file=sys.stderr,
        )
    print(f"logged out of {url}; removed it from {credentials_path()}")
    return EXIT_OK


def _cmd_whoami(args: argparse.Namespace) -> int:
    """Ask the server who the stored (or given) token is."""
    from .auth.credentials import LoginError, whoami, with_token_refresh

    url, token = _remote_target(args.url, args.token)
    if not url or not token:
        print("not logged in. Run:\n  rp login https://profiles.example.org", file=sys.stderr)
        return EXIT_USAGE
    try:
        record = with_token_refresh(token, lambda t: whoami(url, t))
    except LoginError as e:
        print(f"whoami failed: {e}", file=sys.stderr)
        return EXIT_ERROR
    if args.as_json:
        print(json.dumps({"url": url, **record}, indent=2))
        return EXIT_OK
    owner = record.get("owner") or {}
    who = owner.get("name") or owner.get("orcid") or record.get("app") or "unknown"
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
