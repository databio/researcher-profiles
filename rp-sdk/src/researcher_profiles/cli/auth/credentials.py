"""``rp login``: a stored push credential for one registry.

The scripted credential (``RESEARCHER_PROFILES_TOKEN``, an operator or
``push`` key) is for scripts. A person pushing their own profile from a laptop
gets one by logging in: ``rp login <server>`` runs the server's
device-authorization flow (``POST /api/manage/cli-auth``, approve in the
browser, then poll), and the key that comes back is stored here, in
``~/.config/researcher-profiles/credentials.json`` (mode 0600)::

    {"url": "https://profiles.example.org", "token": "rpk_...", "orcid": ..., "name": ...}

Every remote command then resolves its token in this order (:func:`resolve_token`):
an explicit ``--token``, then ``RESEARCHER_PROFILES_TOKEN``, then the stored
login, but only for the server it was minted for. And
``--url`` defaults to the stored login's URL (:func:`resolve_url`), which is
what lets ``rp push path/to/profile`` work with no flags at all.

Distinct from :mod:`researcher_profiles.cli.auth.agent`, which resolves ``rpa_`` AGENT
keys (per-profile write scopes for an assistant) from ``credentials.toml``.
This file holds a person's own ``push_own`` key.
"""

import json
import os
import sys
import time
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

TOKEN_ENV_VAR = "RESEARCHER_PROFILES_TOKEN"
CREDENTIALS_FILENAME = "credentials.json"


class LoginError(Exception):
    """The login flow did not produce a key (server error, timeout, denial)."""


@dataclass(frozen=True)
class Login:
    url: str
    token: str
    orcid: Optional[str] = None
    name: Optional[str] = None


def _config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "researcher-profiles"
    return Path.home() / ".config" / "researcher-profiles"


def credentials_path() -> Path:
    return _config_dir() / CREDENTIALS_FILENAME


def server_base(url: str) -> str:
    """Normalize a server URL for comparison and storage."""
    url = url.strip().rstrip("/")
    marker = "/api/v1/"
    if marker in url:
        url = url[: url.index(marker)]
    return url


def load_login(path: Optional[Path] = None) -> Optional[Login]:
    """The stored login, or ``None`` when there is none or it is unreadable."""
    p = path or credentials_path()
    try:
        raw = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    url, token = raw.get("url"), raw.get("token")
    if not url or not token:
        return None
    return Login(url=server_base(url), token=token, orcid=raw.get("orcid"), name=raw.get("name"))


def save_login(login: Login, path: Optional[Path] = None) -> Path:
    """Write the login with owner-only permissions. Replaces any previous one."""
    p = path or credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(asdict(login), indent=2) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(data)
    os.chmod(p, 0o600)
    return p


def clear_login(path: Optional[Path] = None) -> bool:
    """Delete the stored login. True when a file was removed."""
    p = path or credentials_path()
    try:
        p.unlink()
    except FileNotFoundError:
        return False
    return True


def resolve_url(explicit: Optional[str], path: Optional[Path] = None) -> Optional[str]:
    """``--url`` if given, else the stored login's server, else ``None``."""
    if explicit:
        return explicit
    login = load_login(path)
    return login.url if login else None


def resolve_token(
    explicit: Optional[str], url: Optional[str], path: Optional[Path] = None
) -> Optional[str]:
    """The credential for ``url``: ``--token`` > env var > stored login for that server.

    The stored key is scoped to the server it came from, so it is offered
    only when ``url`` is that server (or ``url`` is unset).
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
        return login.token
    return None


# --- the device flow -----------------------------------------------------------


def login(
    url: str,
    *,
    label: Optional[str] = None,
    open_browser: bool = True,
    timeout: float = 600.0,
    client: Any = None,
    out=None,
    sleep: Optional[Callable[[float], None]] = None,
) -> Login:
    """Run the device-authorization flow against ``url`` and return the key.

    Prints the verify URL (and opens it in a browser when asked to) on ``out``
    (stderr by default), then polls until the person approves the request in
    their browser. Raises :class:`LoginError` on refusal or timeout. The
    result is not stored; the caller decides (``rp login`` stores it).
    """
    import httpx

    out = out or sys.stderr
    base = server_base(url)
    http = client if client is not None else httpx.Client(base_url=base, timeout=30.0)
    try:
        try:
            r = http.post("/api/manage/cli-auth", json={"label": label or _default_label()})
        except httpx.HTTPError as e:
            raise LoginError(f"{base}: {e}") from e
        if r.status_code == 404:
            raise LoginError(f"{base} does not offer command-line login (no /api/manage/cli-auth)")
        if r.status_code >= 400:
            raise LoginError(f"{base}: login request refused ({r.status_code}): {r.text[:200]}")
        start = r.json()
        device_code = start["device_code"]
        verify_url = start["verify_url"]
        interval = 3.0 if start.get("interval") is None else float(start["interval"])
        expires_in = float(start.get("expires_in") or timeout)
        sleep = sleep or time.sleep

        print(f"Open this link in your browser to approve the login:\n\n  {verify_url}\n", file=out)
        print(f"Code: {start['user_code']}", file=out)
        if open_browser:
            try:
                webbrowser.open(verify_url)
            except (webbrowser.Error, OSError):  # no browser is fine, the URL is printed
                pass
        print("Waiting for approval...", file=out)

        deadline = time.monotonic() + min(timeout, expires_in)
        while time.monotonic() < deadline:
            sleep(interval)
            try:
                p = http.post("/api/manage/cli-auth/poll", json={"device_code": device_code})
            except httpx.HTTPError as e:
                raise LoginError(f"{base}: {e}") from e
            if p.status_code == 404:
                raise LoginError("login request expired or was already used; run rp login again")
            if p.status_code >= 400:
                raise LoginError(f"{base}: poll failed ({p.status_code}): {p.text[:200]}")
            body = p.json()
            if body.get("status") == "approved":
                return Login(
                    url=base, token=body["token"], orcid=body.get("orcid"), name=body.get("name")
                )
        raise LoginError("timed out waiting for approval; run rp login again")
    finally:
        if client is None:
            http.close()


def _default_label() -> str:
    import socket

    try:
        return socket.gethostname()
    except OSError:
        return "rp"


def whoami(url: str, token: str, *, client: Any = None, timeout: float = 30.0) -> dict:
    """``GET /api/manage/whoami`` with ``token``. Raises :class:`LoginError` on refusal."""
    import httpx

    base = server_base(url)
    http = client if client is not None else httpx.Client(base_url=base, timeout=timeout)
    try:
        try:
            r = http.get("/api/manage/whoami", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            raise LoginError(f"{base}: {e}") from e
        if r.status_code == 401:
            raise LoginError("token rejected; run rp login again")
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
    "clear_login",
    "credentials_path",
    "load_login",
    "login",
    "resolve_token",
    "resolve_url",
    "save_login",
    "server_base",
    "whoami",
]
