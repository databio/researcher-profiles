"""``rp login``: stored, self-renewing push credentials, one per registry.

A thin adapter over snapgate's shared OAuth client, vendored byte for byte at
``researcher_profiles/_vendor/snapgate_oauth_client`` (re-vendor with the
monorepo's ``scripts/vendor-snapgate-oauth-client.sh``; never edit the copy).
It needs only the standard library, so ``rp login`` works on a bare install.

``rp login <server>`` reads the server's OAuth metadata (RFC 8414) and signs
in as the registered public client ``rp-cli``, asking for the
``profiles:push`` scope. Two ways:

- **Loopback + PKCE (the default; RFC 8252, RFC 7636).** ``rp`` listens on
  ``http://127.0.0.1:<random port>/callback`` and opens the system browser at
  the server's ``authorization_endpoint``.
- **The device flow (RFC 8628).** ``rp login --device``, and automatically over
  SSH or on Linux with no display, or when the browser cannot be opened, when
  the server offers it (Prosopia does). It prints a link and a code to approve
  on any device. With ``--no-browser`` (or over SSH at a server without a
  device flow) ``rp`` prints the address and takes the address the browser
  ends on, pasted at the terminal.

The tokens are stored in ``~/.config/researcher-profiles/credentials.json``
(mode 0600, written atomically), one login per server, in the shared client's
format::

    {"version": 1, "logins": {"https://profiles.example.org": {
      "server": ..., "issuer": ..., "client_id": "rp-cli", "token_endpoint": ...,
      "access_token": "rpa_...", "access_expires_at": 1767225600,
      "refresh_token": "rpr_...", "extra": {"orcid": ..., "name": ...}, ...}}}

A stored login is offered only to its own server. Commands refresh its access
token when it has expired (or is about to), and once more when the server
answers 401, under a lock, so two ``rp`` processes never present the same
refresh token. A refresh that fails raises one plain sentence (``rp: ...``).
``rp logout`` revokes the login at the server (RFC 7009) and forgets it. A file
from an older ``rp`` (including the never-expiring ``rpk_`` key files) is no
longer read: the error says to run ``rp login``, which replaces it.

Scripts use ``RESEARCHER_PROFILES_TOKEN`` instead. ``rpa_`` keys minted on the
Privacy page live in ``credentials.toml`` (:mod:`researcher_profiles.cli.auth.agent`).
"""

import os
from pathlib import Path
from typing import Any, Callable, Optional

from ..._vendor.snapgate_oauth_client import (
    ClientConfig,
    Credential,
    CredentialKey,
    FileStore,
    NotSignedIn,
    OAuthClientError,
    TokenSource,
)
from ..._vendor.snapgate_oauth_client import cli as oauth_cli

TOKEN_ENV_VAR = "RESEARCHER_PROFILES_TOKEN"
CREDENTIALS_FILENAME = "credentials.json"

CONFIG = ClientConfig(
    client_id="rp-cli",
    app_name="rp",
    scope="profiles:push",
    resource_path=None,  # Prosopia: resource is optional; send none
    relogin_hint="Run `rp login` again.",
)

#: rp's callers keep catching ``LoginError``: every error the login raises.
LoginError = OAuthClientError


class TokenRejected(OAuthClientError, PermissionError):
    """The server answered 401 to a stored or given token."""


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


def store() -> FileStore:
    return FileStore(credentials_path(), login_command="rp login")


def _logins() -> list[tuple[CredentialKey, Credential]]:
    """Every live stored login. ``LoginError`` for a file from an older ``rp``."""
    s = store()
    found = []
    for key in s.keys():
        cred = s.load(key)
        if cred is not None and not cred.ended:
            found.append((key, cred))
    return found


def default_server() -> Optional[str]:
    """The server of the most recently obtained live login, or ``None``."""
    logins = _logins()
    if not logins:
        return None
    return max(logins, key=lambda kc: kc[1].obtained_at)[0].server


def _identify(cred: Credential) -> dict:
    """Who the new login is (``whoami``); unknown when the server will not say."""
    try:
        owner = whoami(cred.server, cred.access_token).get("owner") or {}
    except (OAuthClientError, ImportError, ValueError):
        owner = {}
    return {"orcid": owner.get("orcid"), "name": owner.get("name")}


def cli_app() -> oauth_cli.CliApp:
    """``rp login`` on the shared client. No ``token`` command."""
    return oauth_cli.CliApp(
        config=CONFIG, store=store(), login_command="rp login", identify=_identify
    )


def resolve_url(explicit: Optional[str]) -> Optional[str]:
    """``--url`` if given, else the most recent stored login's server, else ``None``."""
    return explicit or default_server()


def stored_token(url: str) -> Optional[str]:
    """The stored login's access token for ``url``, refreshed first if it has expired.

    ``None`` when nothing is stored for that server. A login that has ended, or
    a refresh that fails, raises :data:`LoginError`.
    """
    source = TokenSource(CONFIG, store(), CredentialKey(server_base(url)))
    try:
        return source.access_token()
    except NotSignedIn:
        return None


def resolve_token(explicit: Optional[str], url: Optional[str]) -> Optional[str]:
    """The credential for ``url``: ``--token`` > env var > stored login for that server.

    A stored token goes only to the server it came from. With no ``url``, the
    most recent stored login's server.
    """
    if explicit:
        return explicit
    env = os.environ.get(TOKEN_ENV_VAR) or None
    if env:
        return env
    url = url or default_server()
    return stored_token(url) if url else None


def _stored_key_for(token: Optional[str]) -> Optional[CredentialKey]:
    if not token:
        return None
    try:
        logins = _logins()
    except OAuthClientError:
        return None
    return next((key for key, cred in logins if cred.access_token == token), None)


def with_token_refresh(token: Optional[str], call: Callable[[Optional[str]], Any]) -> Any:
    """``call(token)``, retried once with a refreshed token if it raises ``PermissionError``.

    Remote calls raise ``PermissionError`` on a 401. Only a token that came from
    a stored login is refreshed (the login's own ``TokenSource``); a token from
    ``--token`` or the environment is used as given.
    """
    key = _stored_key_for(token)
    if key is None:
        return call(token)
    return TokenSource(CONFIG, store(), key).call(call)


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
    "CONFIG",
    "LoginError",
    "TOKEN_ENV_VAR",
    "TokenRejected",
    "cli_app",
    "config_dir",
    "credentials_path",
    "default_server",
    "resolve_token",
    "resolve_url",
    "server_base",
    "store",
    "stored_token",
    "whoami",
    "with_token_refresh",
]
