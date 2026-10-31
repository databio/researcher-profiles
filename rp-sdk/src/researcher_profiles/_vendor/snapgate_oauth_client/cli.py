# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""The command-line front end: login, logout, status, and an opt-in ``token``.

A host builds one :class:`CliApp` and either calls the functions from its own
commands (keeping its own names and flags) or hands argv to :func:`main`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import IO

from .device import poll_device_token, start_device_authorization
from .discovery import discover as _discover, normalize_server
from .errors import LoginFailed, OAuthClientError
from .grants import begin_authorization, complete_authorization, revoke_credential
from .http import Http, UrllibHttp
from .loopback import LoopbackReceiver
from .models import ClientConfig, Credential, CredentialKey, ServerMetadata
from .source import TokenSource
from .store import FileStore

PASTE_HELP = (
    "If the browser is on another computer, open the address there. When it ends on a "
    "page that does not load (127.0.0.1), paste that page's address here and press Enter."
)


@dataclass(frozen=True)
class CliApp:
    """One command-line host: its client settings, its store, its words."""

    config: ClientConfig
    store: FileStore
    login_command: str  # "lmn login"
    #: After login: who is this? Returns the credential's ``extra``. An
    #: ``OAuthClientError`` from it revokes the new login and is re-raised.
    identify: Callable[[Credential], dict] | None = None
    #: Offer ``token`` (prints a fresh access token). Off unless the host's
    #: login may be copied by other programs: lmn's acts as the person.
    token_command: bool = False


def prefer_device_flow(env: Mapping[str, str] | None = None, platform: str | None = None) -> bool:
    """SSH (``SSH_CONNECTION``/``SSH_TTY``), or Linux with no ``DISPLAY``/``WAYLAND_DISPLAY``."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return True
    return platform.startswith("linux") and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def _open(url: str, opener: Callable[[str], bool] | None) -> bool:
    try:
        return bool((opener or webbrowser.open)(url))
    except (webbrowser.Error, OSError):
        return False


def _read_pasted(stream: IO[str], receiver: LoopbackReceiver) -> None:
    """Feed lines pasted at the terminal to the receiver until one is ours."""
    while True:
        try:
            line = stream.readline()
        except (OSError, ValueError):
            return
        if not line or receiver.feed(line):
            return


def _device_login(
    meta: ServerMetadata,
    config: ClientConfig,
    *,
    http: Http,
    extra_params: Mapping[str, str] | None,
    open_browser: bool,
    opener: Callable[[str], bool] | None,
    timeout: float,
    out: IO[str],
    sleep: Callable[[float], None],
) -> Credential:
    code = start_device_authorization(
        meta, config, http=http, extra_params=extra_params, sleep=sleep
    )
    link = code.verification_uri_complete or code.verification_uri
    print(f"Open this link in your browser to approve the login:\n\n  {link}\n", file=out)
    print(f"Code: {code.user_code}", file=out)
    if open_browser:
        _open(link, opener)  # no browser is fine: the link is printed
    print("Waiting for approval...", file=out)
    return poll_device_token(meta, config, code, http=http, sleep=sleep, timeout=timeout)


def login(
    app: CliApp,
    server: str,
    *,
    browser: bool = True,
    device: bool | None = None,
    extra_params: Mapping[str, str] | None = None,
    timeout: float = 300.0,
    out: IO[str] | None = None,
    stdin: IO[str] | None = None,
    opener: Callable[[str], bool] | None = None,
    http: Http | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Credential:
    """Sign in to ``server``, store the login, return it.

    The flow: ``device=True`` the device flow; ``device=None`` the device flow
    when this is a remote session (:func:`prefer_device_flow`) and the server
    offers it, else the loopback flow, which switches to the device flow if
    the browser cannot be opened (and the server offers it), or prints the
    address and accepts the address the browser ends on, pasted at ``stdin``.
    Any earlier login for the server is revoked only after the new one
    succeeded. An unreadable or old-format store file is replaced. ``out``
    defaults to ``sys.stderr``, ``stdin`` to ``sys.stdin`` (read at call time).
    """
    out = out if out is not None else sys.stderr
    config = app.config
    http = http or UrllibHttp(config.app_name)
    server = normalize_server(server)
    meta = _discover(server, config, http=http)
    remote = prefer_device_flow()
    has_device = meta.device_authorization_endpoint is not None

    def device_flow(open_browser: bool) -> Credential:
        return _device_login(
            meta,
            config,
            http=http,
            extra_params=extra_params,
            open_browser=open_browser,
            opener=opener,
            timeout=timeout,
            out=out,
            sleep=sleep,
        )

    cred: Credential | None = None
    if device is True:
        if not has_device:
            raise LoginFailed(
                f"{server} offers no login code; use --no-browser and paste the address instead.",
                server=server,
            )
        cred = device_flow(open_browser=False)  # the person chose another device
    elif (device is None and remote and has_device) or (
        meta.authorization_endpoint is None and has_device and device is None
    ):
        cred = device_flow(open_browser=browser and not remote)
    else:
        cred = _loopback_login(
            meta,
            config,
            http=http,
            extra_params=extra_params,
            browser=browser and not remote,
            switch_to_device=has_device and device is None,
            opener=opener,
            timeout=timeout,
            out=out,
            stdin=stdin,
        )
        if cred is None:
            print("Could not open a browser; using a login code instead.\n", file=out)
            cred = device_flow(open_browser=False)

    if app.identify is not None:
        try:
            cred = replace(cred, extra=app.identify(cred))
        except OAuthClientError:
            revoke_credential(cred, config, http=http)
            raise

    TokenSource(config, app.store, CredentialKey(server), http=http).save(cred)
    return cred


def _loopback_login(
    meta: ServerMetadata,
    config: ClientConfig,
    *,
    http: Http,
    extra_params: Mapping[str, str] | None,
    browser: bool,
    switch_to_device: bool,
    opener: Callable[[str], bool] | None,
    timeout: float,
    out: IO[str],
    stdin: IO[str] | None,
) -> Credential | None:
    """The loopback flow; None when the browser failed and the device flow should run."""
    with LoopbackReceiver(app_name=config.app_name) as receiver:
        pending = begin_authorization(
            meta, config, receiver.redirect_uri, extra_params=extra_params
        )
        receiver.expect(pending.state)
        url = pending.authorization_url
        if browser and _open(url, opener):
            print(
                f"Opened your browser to sign in. If it did not open, visit:\n\n  {url}\n", file=out
            )
        elif browser and switch_to_device:
            return None
        else:
            print(f"Open this address in a browser to sign in:\n\n  {url}\n", file=out)
            print(PASTE_HELP, file=out)
            threading.Thread(
                target=_read_pasted,
                args=(stdin if stdin is not None else sys.stdin, receiver),
                daemon=True,
            ).start()
        print("Waiting for you to sign in...", file=out)
        params = receiver.wait(timeout)
    return complete_authorization(pending, params, config, http=http)


def logout(app: CliApp, server: str, *, http: Http | None = None) -> bool:
    """Revoke the stored login (best effort), then forget it either way.

    True if the server confirmed the revoke. The host prints its own words.
    """
    key = CredentialKey(normalize_server(server))
    return TokenSource(app.config, app.store, key, http=http).sign_out()


def status(app: CliApp, server: str) -> Credential | None:
    """What is stored for ``server`` (a live login, a tombstone, or None). No network."""
    return app.store.load(CredentialKey(normalize_server(server)))


def print_token(
    app: CliApp, server: str, *, out: IO[str] | None = None, http: Http | None = None
) -> None:
    """Print a fresh access token, and only it, on ``out`` (default ``sys.stdout``).
    ``ValueError`` unless ``app.token_command``."""
    out = out if out is not None else sys.stdout
    if not app.token_command:
        raise ValueError(f"{app.config.app_name} does not offer a token command")
    key = CredentialKey(normalize_server(server))
    print(TokenSource(app.config, app.store, key, http=http).access_token(), file=out)


def add_login_flags(parser: argparse.ArgumentParser) -> None:
    """``--no-browser``, ``--device``, ``--timeout``."""
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser here: open the printed address anywhere and paste back "
        "the address the browser ends on",
    )
    parser.add_argument(
        "--device",
        action="store_true",
        help="Use a login code to approve on another device (when the server offers one)",
    )
    parser.add_argument(
        "--timeout", type=float, default=300.0, help="Seconds to wait for sign-in (default 300)"
    )


def _when(at: int | None) -> str:
    if at is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(at))


def _print_status(server: str, cred: Credential | None, login_command: str) -> None:
    if cred is not None and not cred.ended:
        print(f"Signed in to {server}.")
        print(f"Access token expires: {_when(cred.access_expires_at)}")
        print(f"Signed in at: {_when(cred.obtained_at)}; last renewed: {_when(cred.refreshed_at)}")
    elif cred is not None:
        print(
            f"The login for {server} ended at {_when(cred.ended_at)} ({cred.ended_reason}). "
            f"Run `{login_command}`."
        )
    else:
        print(f"Not signed in to {server}. Run `{login_command}`.")


def main(app: CliApp, argv: list[str] | None = None, *, prog: str) -> int:
    """``login | logout | status [--json] | token`` (``token`` only when enabled).

    Exit 0 on success (``status``: signed in), 1 otherwise. Errors print one
    sentence, ``<prog>: <message>``, on stderr.
    """
    parser = argparse.ArgumentParser(prog=prog, description=f"Sign {prog} in to a server.")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")
    p = sub.add_parser("login", help="Sign in through the browser and store the login")
    p.add_argument("server", help="Server base URL")
    add_login_flags(p)
    p = sub.add_parser("logout", help="Revoke the stored login and forget it")
    p.add_argument("server", help="Server base URL")
    p = sub.add_parser("status", help="Show what is stored (no tokens)")
    p.add_argument("server", help="Server base URL")
    p.add_argument("--json", action="store_true", help="Print the status as JSON")
    if app.token_command:
        p = sub.add_parser("token", help="Print a fresh access token on stdout")
        p.add_argument("server", help="Server base URL")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "login":
            cred = login(
                app,
                args.server,
                browser=not args.no_browser,
                device=True if args.device else None,
                timeout=args.timeout,
            )
            print(f"Signed in to {cred.server}.", file=sys.stderr)
            return 0
        if args.cmd == "logout":
            confirmed = logout(app, args.server)
            note = "" if confirmed else " (the server did not confirm; revoke it on the web)"
            print(f"Signed out of {normalize_server(args.server)}{note}.", file=sys.stderr)
            return 0
        if args.cmd == "status":
            server = normalize_server(args.server)
            cred = status(app, server)
            if args.json:
                shown = cred.status_dict() if cred else {"server": server, "signed_in": False}
                print(json.dumps(shown, indent=2))
            else:
                _print_status(server, cred, app.login_command)
            return 0 if cred is not None and not cred.ended else 1
        print_token(app, args.server)
        return 0
    except OAuthClientError as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return 1
