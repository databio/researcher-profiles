# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""The errors the OAuth client raises: one family, each message a full sentence.

``str(error)`` is written for a person and never holds a token. ``error.server``
names the server it is about, when there is one.
"""

from __future__ import annotations

from typing import Any


class OAuthClientError(Exception):
    """Base. ``str(e)`` is a full sentence for a person; ``.server`` names the server."""

    def __init__(self, message: str, *, server: str | None = None):
        super().__init__(message)
        self.message = message
        self.server = server

    def __str__(self) -> str:
        return self.message


class DiscoveryError(OAuthClientError):
    """No or bad metadata, an issuer mismatch, or a missing endpoint."""


class SignInNotNeeded(DiscoveryError):
    """The probe path answered 200 without a credential: the server needs no sign-in."""


class LoginFailed(OAuthClientError):
    """The interactive flow failed: timeout, state or iss mismatch, code refused."""


class LoginDenied(LoginFailed):
    """The person clicked Deny (``access_denied``)."""


class IssuerMismatch(LoginFailed):
    """The browser's answer named another server, or none when the server promised to (RFC 9207)."""


class LoginRequired(OAuthClientError):
    """No token can be had without signing in again."""


class NotSignedIn(LoginRequired):
    """Nothing is stored for this server."""


class LoginEnded(LoginRequired):
    """The refresh answered ``invalid_grant``, or the stored entry is a tombstone.

    ``reason`` says why (``"refused"``: the server ended it), ``ended_at`` when
    (unix seconds).
    """

    def __init__(
        self,
        message: str,
        *,
        server: str | None = None,
        reason: str = "refused",
        ended_at: int | None = None,
    ):
        super().__init__(message, server=server)
        self.reason = reason
        self.ended_at = ended_at


class ServerUnavailable(OAuthClientError):
    """A network error, a 429 or a 5xx. Any stored login is untouched."""

    def __init__(
        self, message: str, *, server: str | None = None, retry_after: float | None = None
    ):
        super().__init__(message, server=server)
        self.retry_after = retry_after


class ClientMisconfigured(OAuthClientError):
    """The server refused the client's settings (``invalid_client``, ...): a bug.

    ``error`` is the server's OAuth error code (``"invalid_client"`` for a wrong
    client secret), when it sent one.
    """

    def __init__(self, message: str, *, server: str | None = None, error: str | None = None):
        super().__init__(message, server=server)
        self.error = error


class CredentialStoreError(OAuthClientError):
    """The credentials file is unreadable or from an older version."""


# -- message templates -------------------------------------------------------------
# ``config`` is a ClientConfig (only ``app_name`` and ``relogin_hint`` are read).


def not_signed_in(server: str, config: Any) -> NotSignedIn:
    return NotSignedIn(f"You are not signed in to {server}. {config.relogin_hint}", server=server)


def login_ended(
    server: str, config: Any, *, reason: str = "refused", ended_at: int | None = None
) -> LoginEnded:
    return LoginEnded(
        f"Your {config.app_name} login for {server} has ended (signed out, revoked, or "
        f"unused too long). {config.relogin_hint}",
        server=server,
        reason=reason,
        ended_at=ended_at,
    )


def server_unavailable(
    server: str, why: str, *, retry_after: float | None = None, renewing: bool = True
) -> ServerUnavailable:
    if renewing:
        text = (
            f"Could not reach {server} to renew your login ({why}). Your login is kept; try again."
        )
    else:
        text = f"Could not reach {server} to finish signing in ({why}). Try again in a moment."
    return ServerUnavailable(text, server=server, retry_after=retry_after)


def client_misconfigured(server: str, config: Any, error: str, description: str | None):
    detail = f"{error}: {description}" if description else error
    return ClientMisconfigured(
        f"{server} refused {config.app_name}'s sign-in settings ({detail}). "
        f"This is a bug in {config.app_name}.",
        server=server,
        error=error,
    )
