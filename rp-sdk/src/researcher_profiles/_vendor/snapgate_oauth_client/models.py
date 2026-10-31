# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""The data the client passes around: settings, server metadata, credentials.

Token fields never show in a ``repr``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from .errors import CredentialStoreError

TokenAuthMethod = Literal["none", "client_secret_post", "client_secret_basic"]
_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")
#: Two tries of one HTTP call must fit inside the server's 120 s refresh grace window.
MAX_TIMEOUT = 50.0


@dataclass(frozen=True)
class ClientConfig:
    """How one app signs in. The same for every server it talks to."""

    client_id: str  # "lumenoia-cli", "rp-cli"
    app_name: str  # "lmn", "rp": messages, the loopback page, the User-Agent
    scope: str | None = None  # "cli:login"; None sends no scope
    #: "/api/v1" -> resource=<issuer>/api/v1 on authorize, token and refresh
    #: (RFC 8707); None sends none.
    resource_path: str | None = None
    relogin_hint: str = "Sign in again."  # "Run `lmn login`."
    #: RFC 9728 fallback when the URL serves no metadata: GET it without a credential.
    probe_path: str | None = None
    client_secret: str | None = field(default=None, repr=False)  # confidential clients
    #: None: "client_secret_basic" with a client_secret, else "none".
    token_auth_method: TokenAuthMethod | None = None
    #: A server-to-server origin ("http://mygrants:8128") for discovery, token,
    #: revoke and API calls; the browser keeps the public addresses.
    backchannel_url: str | None = None
    refresh_margin: float = 60.0  # refresh this many seconds before expiry
    timeout: float = 30.0  # per HTTP call; at most 50 (a refresh retry must fit in 120 s)

    def __post_init__(self) -> None:
        if not self.client_id:
            raise ValueError("ClientConfig needs a client_id")
        if self.token_auth_method is None:
            method = "client_secret_basic" if self.client_secret else "none"
            object.__setattr__(self, "token_auth_method", method)
        if self.resource_path is not None and not self.resource_path.startswith("/"):
            raise ValueError("ClientConfig resource_path must start with /")
        if self.probe_path is not None and not self.probe_path.startswith("/"):
            raise ValueError("ClientConfig probe_path must start with /")
        if not 0 < self.timeout <= MAX_TIMEOUT:
            raise ValueError(f"ClientConfig timeout must be more than 0 and at most {MAX_TIMEOUT}")
        if self.token_auth_method not in _AUTH_METHODS:
            raise ValueError(f"unknown token_auth_method {self.token_auth_method!r}")
        if (self.token_auth_method == "none") != (not self.client_secret):
            raise ValueError(
                "client_secret goes with token_auth_method client_secret_post or "
                "client_secret_basic, and only with them"
            )
        if self.backchannel_url is not None:
            # Plain http is allowed for any host: it names a private network
            # the operator chose (a docker network, a VPN).
            parts = urlsplit(self.backchannel_url.strip().rstrip("/"))
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(
                    f"backchannel_url {self.backchannel_url!r} is not a server-to-server "
                    "address (use http(s)://host)"
                )
            if parts.query or parts.fragment:
                raise ValueError(
                    f"backchannel_url {self.backchannel_url!r} has a query or fragment"
                )
            object.__setattr__(self, "backchannel_url", f"{parts.scheme}://{parts.netloc}")

    def route(self, url: str) -> str:
        """``url`` with its origin swapped for ``backchannel_url`` when set (path and query kept)."""
        if not self.backchannel_url:
            return url
        parts = urlsplit(url)
        return self.backchannel_url + parts.path + (f"?{parts.query}" if parts.query else "")


@dataclass(frozen=True)
class ServerMetadata:
    """A server's RFC 8414 metadata, checked."""

    server: str  # the normalized URL the host talks to
    issuer: str
    authorization_endpoint: str | None
    token_endpoint: str
    revocation_endpoint: str | None
    device_authorization_endpoint: str | None
    iss_parameter_supported: bool
    raw: dict = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class CredentialKey:
    """Which stored login: a server, and (for a shared store) the local user."""

    server: str  # normalized base URL
    owner: str | None = None  # the local user the connection belongs to

    @property
    def storage_key(self) -> str:
        """``server``, or ``"<owner>@<server>"`` when ``owner`` is set."""
        return f"{self.owner}@{self.server}" if self.owner else self.server


def _str(raw: dict, name: str, *, optional: bool = False) -> Any:
    value = raw.get(name)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(name)
    return value


def _int(raw: dict, name: str) -> int | None:
    value = raw.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(name)
    return int(value)


@dataclass(frozen=True)
class Credential:
    """One login: its tokens, where to renew and revoke them, and who it is.

    A tombstone (``ended_at`` set) has no tokens and says why the login ended.
    """

    server: str
    issuer: str
    client_id: str
    token_endpoint: str
    revocation_endpoint: str | None
    resource: str | None
    scope: str | None
    access_token: str = field(repr=False)
    access_expires_at: int | None  # unix seconds; None = the server sent no expires_in
    refresh_token: str | None = field(repr=False)
    obtained_at: int
    refreshed_at: int | None = None
    extra: dict = field(default_factory=dict)  # host-owned identity: {"email", "name", ...}
    ended_at: int | None = None  # tombstone: when the login ended
    ended_reason: str | None = None  # ...and why

    @property
    def ended(self) -> bool:
        return self.ended_at is not None

    def expiring(self, now: float, margin: float) -> bool:
        """Expired, or expiring within ``margin`` seconds. Never, without an expiry."""
        if self.access_expires_at is None:
            return False
        return now + margin >= self.access_expires_at

    def to_dict(self) -> dict:
        return asdict(self)

    def status_dict(self) -> dict:
        """For ``status --json`` and status pages: every field but the tokens, plus ``signed_in``."""
        out = {k: v for k, v in asdict(self).items() if k not in ("access_token", "refresh_token")}
        return {**out, "signed_in": not self.ended}

    @classmethod
    def from_dict(cls, raw: dict) -> Credential:
        """A credential from :meth:`to_dict` output. ``CredentialStoreError`` on a bad shape."""
        if not isinstance(raw, dict):
            raise CredentialStoreError("A stored login is not a JSON object.")
        try:
            extra = raw.get("extra") or {}
            if not isinstance(extra, dict):
                raise ValueError("extra")
            return cls(
                server=_str(raw, "server"),
                issuer=_str(raw, "issuer"),
                client_id=_str(raw, "client_id"),
                token_endpoint=_str(raw, "token_endpoint"),
                revocation_endpoint=_str(raw, "revocation_endpoint", optional=True),
                resource=_str(raw, "resource", optional=True),
                scope=_str(raw, "scope", optional=True),
                access_token=_str(raw, "access_token"),
                access_expires_at=_int(raw, "access_expires_at"),
                refresh_token=_str(raw, "refresh_token", optional=True),
                obtained_at=_int(raw, "obtained_at") or 0,
                refreshed_at=_int(raw, "refreshed_at"),
                extra=extra,
                ended_at=_int(raw, "ended_at"),
                ended_reason=_str(raw, "ended_reason", optional=True),
            )
        except ValueError as e:
            raise CredentialStoreError(
                f"A stored login has a missing or bad field ({e})."
            ) from None


@dataclass(frozen=True)
class PendingAuthorization:
    """Everything needed between sending the browser away and its callback.

    :meth:`to_dict` keeps only what cannot be rediscovered (about 200 bytes),
    so a web app can keep it in a session cookie between the two requests;
    ``grants.resume_authorization`` rebuilds the rest from the server's metadata.
    """

    server: str
    issuer: str
    authorization_url: str
    redirect_uri: str
    state: str
    code_verifier: str = field(repr=False)
    token_endpoint: str
    revocation_endpoint: str | None
    resource: str | None
    scope: str | None
    require_iss: bool
    created_at: int

    def to_dict(self) -> dict:
        """``{"redirect_uri", "state", "code_verifier", "created_at"}``."""
        return {
            "redirect_uri": self.redirect_uri,
            "state": self.state,
            "code_verifier": self.code_verifier,
            "created_at": self.created_at,
        }
