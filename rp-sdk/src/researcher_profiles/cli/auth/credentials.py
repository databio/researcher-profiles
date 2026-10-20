"""``rp login``: a stored, self-renewing push credential for one registry.

``rp login <server>`` reads the server's OAuth metadata (RFC 8414) and signs
in as the registered public client ``rp-cli``, asking for the
``profiles:push`` scope. Two ways:

- **Loopback + PKCE (the default; RFC 8252, RFC 7636).** ``rp`` listens on
  ``http://127.0.0.1:<random port>/callback``, opens the system browser at the
  server's ``authorization_endpoint`` with an S256 code challenge, and trades
  the returned code (plus its verifier) at the ``token_endpoint``.
- **The device flow (RFC 8628).** ``rp login --device``, and automatically
  over SSH (``SSH_CONNECTION`` or ``SSH_TTY`` set), on Linux with no display
  (no ``DISPLAY`` or ``WAYLAND_DISPLAY``), when the browser cannot be opened,
  or when the server names no ``authorization_endpoint``. It prints a link and
  a code to approve on any device.

Either way the server returns a short-lived access token and a refresh token.
They are stored in ``~/.config/researcher-profiles/credentials.json`` (mode
0600, written atomically)::

    {"url": "https://profiles.example.org", "issuer": "https://profiles.example.org",
     "access_token": "rpa_...", "refresh_token": "rpr_...", "expires_at": 1767225600}

Commands refresh the access token when it has expired (or is about to), and
once more when the server answers 401. The refresh token rotates on every use,
so each refresh saves the new one atomically, under a lock, so two ``rp``
processes never present the same old token. ``rp logout`` revokes the login at
the server's ``revocation_endpoint`` (RFC 7009) and deletes the file.

A credentials file from an older ``rp`` holds ``{"url", "token": "rpk_..."}``:
a key that never expires. It keeps working, unchanged, until the next
``rp login`` replaces it.

Scripts use ``RESEARCHER_PROFILES_TOKEN`` instead. ``rpa_`` keys minted on the
Privacy page live in ``credentials.toml`` (:mod:`researcher_profiles.cli.auth.agent`).
"""

import base64
import contextlib
import hashlib
import json
import os
import secrets
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

TOKEN_ENV_VAR = "RESEARCHER_PROFILES_TOKEN"
CREDENTIALS_FILENAME = "credentials.json"


class LoginError(Exception):
    """The login flow did not produce a token (server error, timeout, denial)."""


class TokenRejected(LoginError, PermissionError):
    """The server answered 401 to a stored or given token."""


@dataclass(frozen=True)
class Login:
    """A stored login: the access token and, for an OAuth login, how to renew it.

    A login from an older ``rp`` (an ``rpk_`` key) has no ``refresh_token``
    and no ``expires_at``: it never expires and is never refreshed.
    """

    url: str
    access_token: str
    refresh_token: Optional[str] = None
    #: Unix seconds when ``access_token`` expires; ``None`` for a key that never does.
    expires_at: Optional[int] = None
    issuer: Optional[str] = None

    @property
    def token(self) -> str:
        return self.access_token

    @property
    def refreshable(self) -> bool:
        return bool(self.refresh_token)

    def expired(self, now: Optional[float] = None, margin: float = 60.0) -> bool:
        """Expired, or expiring within ``margin`` seconds. Never, without ``expires_at``."""
        if self.expires_at is None:
            return False
        return (time.time() if now is None else now) + margin >= self.expires_at

    def as_json(self) -> dict:
        out: dict[str, Any] = {"url": self.url, "access_token": self.access_token}
        for name in ("issuer", "refresh_token", "expires_at"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


def config_dir() -> Path:
    """The folder holding both CLI credential files: ``$XDG_CONFIG_HOME/researcher-profiles``, else ``~/.config/researcher-profiles``."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "researcher-profiles"
    return Path.home() / ".config" / "researcher-profiles"


def credentials_path() -> Path:
    return config_dir() / CREDENTIALS_FILENAME


def server_base(url: str) -> str:
    """Normalize a server URL for comparison and storage."""
    url = url.strip().rstrip("/")
    marker = "/api/v1/"
    if marker in url:
        url = url[: url.index(marker)]
    return url


def load_login(path: Optional[Path] = None) -> Optional[Login]:
    """The stored login, or ``None`` when there is none or it is unreadable.

    Reads both the current format and an older ``rp``'s ``{"url", "token"}``.
    """
    p = path or credentials_path()
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    token = raw.get("access_token") or raw.get("token")
    if not isinstance(url, str) or not url or not isinstance(token, str) or not token:
        return None
    expires = raw.get("expires_at")
    refresh = raw.get("refresh_token")
    issuer = raw.get("issuer")
    return Login(
        url=server_base(url),
        access_token=token,
        refresh_token=refresh if isinstance(refresh, str) and refresh else None,
        expires_at=int(expires) if isinstance(expires, (int, float)) else None,
        issuer=issuer if isinstance(issuer, str) else None,
    )


def save_login(login: Login, path: Optional[Path] = None) -> Path:
    """Write the login atomically with owner-only permissions. Replaces any previous one.

    The JSON goes to a 0600 temporary file in the same folder, is flushed to
    disk, and then renamed over the old file, so a reader never sees half a
    file and a crash never loses the rotated refresh token.
    """
    p = path or credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(login.as_json(), indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return p


def clear_login(path: Optional[Path] = None) -> bool:
    """Delete the stored login. True when a file was removed."""
    p = path or credentials_path()
    try:
        p.unlink()
    except FileNotFoundError:
        return False
    return True


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock beside ``path`` (POSIX ``flock``; a no-op elsewhere)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def resolve_url(explicit: Optional[str], path: Optional[Path] = None) -> Optional[str]:
    """``--url`` if given, else the stored login's server, else ``None``."""
    if explicit:
        return explicit
    login = load_login(path)
    return login.url if login else None


def stored_token(path: Optional[Path] = None, *, client: Any = None) -> Optional[str]:
    """The stored login's access token, refreshed first if it has expired.

    A refresh that fails is reported on stderr and the old token is returned,
    so the command's own 401 tells the person to log in again.
    """
    p = path or credentials_path()
    login = load_login(p)
    if login is None:
        return None
    if not (login.refreshable and login.expired()):
        return login.access_token
    try:
        with _locked(p):
            # Another rp may have refreshed while we waited for the lock.
            login = load_login(p) or login
            if login.refreshable and login.expired():
                login = refresh_login(login, path=p, client=client)
    except LoginError as e:
        print(f"rp: could not renew the stored login: {e}", file=sys.stderr)
    return login.access_token


def refresh_after_401(
    rejected: Optional[str], path: Optional[Path] = None, *, client: Any = None
) -> Optional[str]:
    """A new access token after the server rejected ``rejected``, or ``None``.

    Only for the stored login's own access token: a token from ``--token`` or
    the environment is never replaced. If another ``rp`` already refreshed,
    its token is returned without another refresh.
    """
    if not rejected:
        return None
    p = path or credentials_path()
    login = load_login(p)
    if login is None or not login.refreshable:
        return None
    try:
        with _locked(p):
            login = load_login(p)
            if login is None or not login.refreshable:
                return None
            if login.access_token != rejected:
                return login.access_token
            return refresh_login(login, path=p, client=client).access_token
    except LoginError as e:
        print(f"rp: could not renew the stored login: {e}", file=sys.stderr)
        return None


def resolve_token(
    explicit: Optional[str], url: Optional[str], path: Optional[Path] = None
) -> Optional[str]:
    """The credential for ``url``: ``--token`` > env var > stored login for that server.

    The stored token is scoped to the server it came from, so it is offered
    only when ``url`` is that server (or ``url`` is unset). An expired stored
    access token is refreshed first.
    """
    if explicit:
        return explicit
    env = os.environ.get(TOKEN_ENV_VAR) or None
    if env:
        return env
    login = load_login(path)
    if login is None:
        return None
    if url is None or server_base(url) == login.url:
        return stored_token(path)
    return None


def with_token_refresh(token: Optional[str], call: Callable[[Optional[str]], Any]) -> Any:
    """``call(token)``, retried once with a refreshed token if it raises ``PermissionError``.

    Remote calls raise ``PermissionError`` on a 401. Only the stored login's
    access token is refreshed (:func:`refresh_after_401`); otherwise the error
    is raised as it was.
    """
    try:
        return call(token)
    except PermissionError:
        fresh = refresh_after_401(token)
        if fresh is None or fresh == token:
            raise
        return call(fresh)


# --- discovery -------------------------------------------------------------------


METADATA_PATH = "/.well-known/oauth-authorization-server"  # RFC 8414 section 3
CLIENT_ID = "rp-cli"
SCOPE = "profiles:push"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_INTERVAL = 5.0  # RFC 8628 §3.2, when the server sends no interval
SLOW_DOWN_STEP = 5.0  # RFC 8628 §3.5
START_ATTEMPTS = 3  # tries at the device endpoint while it answers 429 or 503
CALLBACK_PATH = "/callback"


def _discover(http: Any, base: str) -> dict:
    """The server's OAuth metadata, checked: right issuer, a token endpoint.

    The metadata's ``issuer`` must be ``base`` itself (RFC 8414 section 3.3), so
    a server cannot point ``rp login`` at another origin's endpoints by
    accident. A server with no metadata has no command-line login.
    """
    import httpx

    try:
        r = http.get(METADATA_PATH)
    except httpx.HTTPError as e:
        raise LoginError(f"{base}: {e}") from e
    if r.status_code == 404:
        raise LoginError(f"{base} does not offer command-line login (no {METADATA_PATH})")
    if r.status_code != 200:
        raise LoginError(f"{base}: server metadata unavailable ({_describe(r)})")
    meta = _json(r)
    if not meta:  # some hosts answer every unknown path with a 200 HTML page
        raise LoginError(f"{base} does not offer command-line login (no {METADATA_PATH})")
    issuer = meta.get("issuer")
    if not isinstance(issuer, str) or issuer.rstrip("/") != base:
        raise LoginError(
            f"{base}: server metadata names issuer {issuer!r}, not {base}; "
            "run rp login with the issuer URL"
        )
    token = meta.get("token_endpoint")
    if not isinstance(token, str) or not token:
        raise LoginError(f"{base}: server metadata has no token_endpoint")
    return meta


def _endpoint(meta: dict, name: str) -> Optional[str]:
    value = meta.get(name)
    return value if isinstance(value, str) and value else None


def prefer_device_flow(env: Optional[dict] = None, platform: Optional[str] = None) -> bool:
    """Whether this machine should use the device flow: SSH, or Linux with no display."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
        return True
    if platform.startswith("linux") and not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        return True
    return False


def _from_token_response(base: str, issuer: str, r: Any, previous: Optional[Login] = None) -> Login:
    """A :class:`Login` from a 200 token response (RFC 6749 section 5.1)."""
    body = _json(r)
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise LoginError(f"{base}: token response has no access_token")
    refresh = body.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        # RFC 6749 section 6: the server may keep the old refresh token.
        refresh = previous.refresh_token if previous else None
    expires_in = body.get("expires_in")
    expires_at = (
        int(time.time() + float(expires_in)) if isinstance(expires_in, (int, float)) else None
    )
    return Login(
        url=base,
        access_token=token,
        refresh_token=refresh,
        expires_at=expires_at,
        issuer=issuer,
    )


def _client_for(base: str, client: Any):
    import httpx

    return client if client is not None else httpx.Client(base_url=base, timeout=30.0)


# --- logging in --------------------------------------------------------------------


def login(
    url: str,
    *,
    label: Optional[str] = None,
    open_browser: bool = True,
    device: Optional[bool] = None,
    timeout: float = 600.0,
    client: Any = None,
    out=None,
    sleep: Optional[Callable[[float], None]] = None,
    opener: Optional[Callable[[str], bool]] = None,
) -> Login:
    """Sign in to ``url`` as ``rp-cli``; return the login (not stored).

    ``device`` picks the flow: ``True`` the device flow, ``False`` the loopback
    flow, ``None`` (the default) the loopback flow unless
    :func:`prefer_device_flow` says otherwise or the server has no
    ``authorization_endpoint``. The loopback flow falls back to the device
    flow when the browser cannot be opened. ``opener`` opens a URL in a
    browser and says whether it could (``webbrowser.open`` by default).

    Raises :class:`LoginError` on a ``404`` (no command-line login), a refusal,
    expiry, or any other failure.
    """
    out = out or sys.stderr
    base = server_base(url)
    http = _client_for(base, client)
    try:
        meta = _discover(http, base)
        use_device = prefer_device_flow() if device is None else device
        if not use_device and not _endpoint(meta, "authorization_endpoint"):
            if device is False:
                raise LoginError(f"{base} does not offer browser login (no authorization_endpoint)")
            use_device = True
        if not use_device:
            result = _loopback_login(
                http,
                base,
                meta,
                label=label or _default_label(),
                open_browser=open_browser,
                timeout=timeout,
                out=out,
                opener=opener,
            )
            if result is not None:
                return result
            print("Could not open a browser; using a login code instead.\n", file=out)
        return _device_login(
            http,
            base,
            meta,
            label=label or _default_label(),
            # Not for --device (the person chose another device) or over SSH.
            open_browser=open_browser and device is not True and not prefer_device_flow(),
            timeout=timeout,
            out=out,
            sleep=sleep or time.sleep,
            opener=opener,
        )
    finally:
        if client is None:
            http.close()


def _open(url: str, opener: Optional[Callable[[str], bool]]) -> bool:
    try:
        return bool((opener or webbrowser.open)(url))
    except (webbrowser.Error, OSError):
        return False


class _CallbackServer(HTTPServer):
    """The one-shot loopback listener: records the first ``/callback`` query."""

    result: Optional[dict[str, str]] = None


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        parts = urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            self.send_error(404)
            return
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if self.server.result is None:
            self.server.result = query
        ok = "code" in query
        body = (
            "<!doctype html><meta charset='utf-8'><title>rp login</title>"
            "<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto'>"
            + (
                "<h1>Signed in</h1><p>You can close this tab and return to your terminal.</p>"
                if ok
                else "<h1>Not signed in</h1><p>Return to your terminal for details.</p>"
            )
            + "</body>"
        ).encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # keep the terminal quiet
        pass


def _pkce_pair() -> tuple[str, str]:
    """``(verifier, S256 challenge)`` (RFC 7636 section 4)."""
    verifier = secrets.token_urlsafe(64)  # 86 characters of the unreserved set
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _loopback_login(
    http: Any,
    base: str,
    meta: dict,
    *,
    label: str,
    open_browser: bool,
    timeout: float,
    out,
    opener: Optional[Callable[[str], bool]],
) -> Optional[Login]:
    """The authorization-code flow with PKCE and a loopback redirect (RFC 8252).

    ``None`` when the browser could not be opened (the caller falls back to
    the device flow).
    """
    import httpx

    issuer = meta["issuer"].rstrip("/")
    endpoint = meta["authorization_endpoint"]
    server = _CallbackServer(("127.0.0.1", 0), _CallbackHandler)
    try:
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        query = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "label": label,
        }
        link = endpoint + ("&" if "?" in endpoint else "?") + urlencode(query)
        if open_browser:
            if not _open(link, opener):
                return None
            print(
                f"Opened your browser to sign in. If it did not open, visit:\n\n  {link}\n",
                file=out,
            )
        else:
            print(f"Open this link in a browser on this computer:\n\n  {link}\n", file=out)
        print("Waiting for you to sign in...", file=out)

        deadline = time.monotonic() + timeout
        while server.result is None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise LoginError("timed out waiting for the browser; run rp login again")
            server.timeout = min(left, 1.0)
            server.handle_request()
        got = server.result
    finally:
        server.server_close()

    if got.get("state") != state:
        raise LoginError("the browser answered for a different login attempt; run rp login again")
    if "iss" in got and got["iss"].rstrip("/") != issuer:  # RFC 9207
        raise LoginError(f"the browser answer came from {got['iss']!r}, not {issuer}")
    if got.get("error") == "access_denied":
        raise LoginError("the login was refused in the browser")
    if got.get("error"):
        desc = got.get("error_description")
        raise LoginError(f"{base}: login failed ({got['error']}" + (f": {desc})" if desc else ")"))
    if not got.get("code"):
        raise LoginError(f"{base}: the browser answer has no code")
    try:
        r = http.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": got["code"],
                "redirect_uri": redirect_uri,
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
            },
        )
    except httpx.HTTPError as e:
        raise LoginError(f"{base}: {e}") from e
    if r.status_code != 200:
        raise LoginError(f"{base}: login failed ({_describe(r)})")
    return _from_token_response(base, issuer, r)


def _device_login(
    http: Any,
    base: str,
    meta: dict,
    *,
    label: str,
    open_browser: bool,
    timeout: float,
    out,
    sleep: Callable[[float], None],
    opener: Optional[Callable[[str], bool]],
) -> Login:
    """The device authorization grant (RFC 8628).

    ``POST`` to the ``device_authorization_endpoint`` opens a request (retried
    on ``429``/``503``). The user code and verification URL are printed on
    ``out``. Then the ``token_endpoint`` is polled every ``interval`` seconds
    until approval or until ``expires_in`` (or ``timeout``) runs out.
    ``slow_down`` (or a ``429``) adds 5 seconds to the interval.
    """
    import httpx

    device_endpoint = _endpoint(meta, "device_authorization_endpoint")
    if device_endpoint is None:
        raise LoginError(
            f"{base} does not offer command-line login (no device_authorization_endpoint)"
        )
    token_endpoint = meta["token_endpoint"]
    form = {"client_id": CLIENT_ID, "scope": SCOPE, "label": label}
    for attempt in range(START_ATTEMPTS):
        try:
            r = http.post(device_endpoint, data=form)
        except httpx.HTTPError as e:
            raise LoginError(f"{base}: {e}") from e
        if r.status_code not in (429, 503) or attempt == START_ATTEMPTS - 1:
            break
        sleep(_retry_after(r) or DEFAULT_INTERVAL)  # busy: retry after Retry-After
    if r.status_code == 404:
        raise LoginError(f"{base} does not offer command-line login (no {device_endpoint})")
    if r.status_code != 200:
        raise LoginError(f"{base}: login request refused ({_describe(r)})")
    try:
        start = r.json()
        device_code = start["device_code"]
        user_code = start["user_code"]
        link = start.get("verification_uri_complete") or start["verification_uri"]
        interval = DEFAULT_INTERVAL if start.get("interval") is None else float(start["interval"])
        expires_in = float(start["expires_in"])
    except (ValueError, KeyError, TypeError) as e:
        raise LoginError(f"{base}: malformed login response: {r.text[:200]}") from e

    print(f"Open this link in your browser to approve the login:\n\n  {link}\n", file=out)
    print(f"Code: {user_code}", file=out)
    if open_browser:
        _open(link, opener)  # no browser is fine, the URL is printed
    print("Waiting for approval...", file=out)

    poll = {"grant_type": DEVICE_GRANT_TYPE, "device_code": device_code, "client_id": CLIENT_ID}
    deadline = time.monotonic() + min(timeout, expires_in)
    while time.monotonic() < deadline:
        sleep(interval)
        try:
            p = http.post(token_endpoint, data=poll)
        except httpx.HTTPError as e:
            raise LoginError(f"{base}: {e}") from e
        if p.status_code == 200:
            return _from_token_response(base, meta["issuer"].rstrip("/"), p)
        if p.status_code == 429:
            interval = max(interval + SLOW_DOWN_STEP, _retry_after(p))
            continue
        error = _json(p).get("error") if p.status_code == 400 else None
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += SLOW_DOWN_STEP
            continue
        if error == "access_denied":
            raise LoginError("the login was refused in the browser")
        if error in ("expired_token", "invalid_grant"):
            raise LoginError("login request expired or was already used; run rp login again")
        raise LoginError(f"{base}: login failed ({_describe(p)})")
    raise LoginError("timed out waiting for approval; run rp login again")


# --- renewing and ending a login ---------------------------------------------------


def refresh_login(login: Login, *, path: Optional[Path] = None, client: Any = None) -> Login:
    """Trade ``login``'s refresh token for a new pair, save it atomically, return it.

    Callers that may race another ``rp`` hold :func:`_locked` around this (as
    :func:`stored_token` and :func:`refresh_after_401` do). Raises
    :class:`LoginError` when the server refuses (the login expired or was
    revoked: run ``rp login`` again) or cannot be reached.
    """
    import httpx

    if not login.refresh_token:
        raise LoginError("this login cannot be renewed; run rp login again")
    base = login.url
    http = _client_for(base, client)
    try:
        meta = _discover(http, base)
        try:
            r = http.post(
                meta["token_endpoint"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": login.refresh_token,
                    "client_id": CLIENT_ID,
                },
            )
        except httpx.HTTPError as e:
            raise LoginError(f"{base}: {e}") from e
    finally:
        if client is None:
            http.close()
    if r.status_code == 400 and _json(r).get("error") == "invalid_grant":
        raise LoginError("the login expired or was revoked; run rp login again")
    if r.status_code != 200:
        raise LoginError(f"{base}: renewing the login failed ({_describe(r)})")
    fresh = _from_token_response(base, meta["issuer"].rstrip("/"), r, previous=login)
    save_login(fresh, path)
    return fresh


def revoke_login(login: Login, *, client: Any = None) -> bool:
    """Revoke ``login`` at the server (RFC 7009). True when the server accepted.

    A login from an older ``rp`` (no refresh token) cannot be revoked here; its
    ``rpk_`` key is revoked on the server's Privacy page. So is a login at a
    server with no ``revocation_endpoint``.
    """
    import httpx

    if not login.refresh_token:
        return False
    http = _client_for(login.url, client)
    try:
        meta = _discover(http, login.url)
        endpoint = _endpoint(meta, "revocation_endpoint")
        if endpoint is None:
            return False
        try:
            r = http.post(
                endpoint,
                data={
                    "token": login.refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": CLIENT_ID,
                },
            )
        except httpx.HTTPError as e:
            raise LoginError(f"{login.url}: {e}") from e
        return r.status_code == 200
    finally:
        if client is None:
            http.close()


def _json(r: Any) -> dict:
    """The response body as a dict, or ``{}`` when it is not a JSON object."""
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _describe(r: Any) -> str:
    """A short description of an error response: status plus the OAuth error, if any."""
    body = _json(r)
    error = body.get("error")
    if error:
        desc = body.get("error_description")
        return f"{r.status_code} {error}" + (f": {desc}" if desc else "")
    return f"{r.status_code}: {r.text[:200]}"


def _retry_after(r: Any) -> float:
    """``Retry-After`` in seconds (delta form only), or 0 when absent or unparseable."""
    try:
        return max(0.0, float(r.headers.get("retry-after", "")))
    except ValueError:
        return 0.0


def _default_label() -> str:
    import socket

    try:
        return socket.gethostname()
    except OSError:
        return "rp"


def whoami(url: str, token: str, *, client: Any = None, timeout: float = 30.0) -> dict:
    """``GET /api/v1/manage/whoami`` with ``token``. Raises :class:`TokenRejected` on a 401."""
    import httpx

    base = server_base(url)
    http = client if client is not None else httpx.Client(base_url=base, timeout=timeout)
    try:
        try:
            r = http.get("/api/v1/manage/whoami", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise LoginError(f"{base}: {e}") from e
        if r.status_code == 401:
            raise TokenRejected("token rejected; run rp login again")
        if r.status_code >= 400:
            raise LoginError(f"{base}: whoami failed ({r.status_code}): {r.text[:200]}")
        return r.json()
    finally:
        if client is None:
            http.close()


__all__ = [
    "Login",
    "LoginError",
    "TOKEN_ENV_VAR",
    "TokenRejected",
    "clear_login",
    "config_dir",
    "credentials_path",
    "load_login",
    "login",
    "prefer_device_flow",
    "refresh_after_401",
    "refresh_login",
    "resolve_token",
    "resolve_url",
    "revoke_login",
    "save_login",
    "server_base",
    "stored_token",
    "whoami",
    "with_token_refresh",
]
