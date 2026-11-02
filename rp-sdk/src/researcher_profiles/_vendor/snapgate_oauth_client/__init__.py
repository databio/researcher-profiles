# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""Snapgate's OAuth client: sign a command line, service or web app in to a server.

Standard library only, and it imports only its own sibling modules, so this
folder can be copied alone into any package (see README.md). Nothing in
``snapgate/__init__.py`` imports it.

    from .oauth_client import ClientConfig, CredentialKey, FileStore, TokenSource, cli

The command-line front end is :mod:`.cli`; the fake server for tests is
:mod:`.testing` (neither is imported here).
"""

from .device import DeviceCode, poll_device_token, start_device_authorization
from .discovery import discover, normalize_server
from .errors import (
    ClientMisconfigured,
    CredentialStoreError,
    DiscoveryError,
    IssuerMismatch,
    LoginDenied,
    LoginEnded,
    LoginFailed,
    LoginRequired,
    NotSignedIn,
    OAuthClientError,
    ServerUnavailable,
    SignInNotNeeded,
)
from .grants import (
    begin_authorization,
    complete_authorization,
    new_pkce,
    parse_token_response,
    refresh_credential,
    resume_authorization,
    revoke_credential,
)
from .http import Http, HttpResponse, TransportError, UrllibHttp
from .loopback import LoopbackReceiver
from .models import (
    ClientConfig,
    Credential,
    CredentialKey,
    PendingAuthorization,
    ServerMetadata,
)
from .source import TokenSource
from .store import CredentialStore, FileStore, LockedEntry, MemoryStore, tombstone

__version__ = "9.0.0"

__all__ = [
    "ClientConfig",
    "ClientMisconfigured",
    "Credential",
    "CredentialKey",
    "CredentialStore",
    "CredentialStoreError",
    "DeviceCode",
    "DiscoveryError",
    "FileStore",
    "Http",
    "HttpResponse",
    "IssuerMismatch",
    "LockedEntry",
    "LoginDenied",
    "LoginEnded",
    "LoginFailed",
    "LoginRequired",
    "LoopbackReceiver",
    "MemoryStore",
    "NotSignedIn",
    "OAuthClientError",
    "PendingAuthorization",
    "ServerMetadata",
    "ServerUnavailable",
    "SignInNotNeeded",
    "TokenSource",
    "TransportError",
    "UrllibHttp",
    "__version__",
    "begin_authorization",
    "complete_authorization",
    "discover",
    "new_pkce",
    "normalize_server",
    "parse_token_response",
    "poll_device_token",
    "refresh_credential",
    "resume_authorization",
    "revoke_credential",
    "start_device_authorization",
    "tombstone",
]
