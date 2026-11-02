# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""The wire calls: PKCE, the authorize URL, the code trade, refresh and revoke.

Each rule here matches snapgate's server (``snapgate/oauth.py``): PKCE S256
only, ``iss`` on every authorization response (RFC 9207), rotating refresh
tokens with a 120 s grace window, and RFC 7009 revoke.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import quote, urlencode

from .errors import (
    DiscoveryError,
    IssuerMismatch,
    LoginDenied,
    LoginFailed,
    OAuthClientError,
    client_misconfigured,
    login_ended,
    server_unavailable,
)
from .http import Http, HttpResponse, TransportError
from .models import ClientConfig, Credential, PendingAuthorization, ServerMetadata

#: Authorize-URL parameters the client sets itself; ``extra_params`` may not name them.
RESERVED = frozenset(
    {
        "response_type",
        "client_id",
        "redirect_uri",
        "scope",
        "state",
        "code_challenge",
        "code_challenge_method",
        "resource",
    }
)
#: Errors that mean the client's own settings are wrong (a bug, not the person).
MISCONFIGURED = frozenset(
    {
        "invalid_client",
        "unauthorized_client",
        "invalid_target",
        "invalid_scope",
        "invalid_request",
        "unsupported_grant_type",
    }
)

During = Literal["code", "refresh", "device"]


def new_pkce() -> tuple[str, str]:
    """``(verifier, S256 challenge)`` (RFC 7636). 86 and 43 characters."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def resource_for(issuer: str, config: ClientConfig) -> str | None:
    """``issuer + resource_path`` (RFC 8707), or None when the app sends none."""
    return issuer.rstrip("/") + config.resource_path if config.resource_path else None


def client_auth(
    form: dict[str, str], config: ClientConfig, client_id: str | None = None
) -> dict[str, str]:
    """Add client authentication to ``form``; return the extra headers.

    Only for ``token_auth_method != "none"`` (confidential web clients).
    """
    headers: dict[str, str] = {}
    if config.token_auth_method == "client_secret_post":
        form["client_secret"] = config.client_secret or ""
    elif config.token_auth_method == "client_secret_basic":
        pair = f"{quote(client_id or config.client_id, safe='')}:{quote(config.client_secret or '', safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode("ascii")
    return headers


def retry_after(resp: HttpResponse) -> float | None:
    """``Retry-After`` in seconds (delta or HTTP date), or None."""
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def begin_authorization(
    meta: ServerMetadata,
    config: ClientConfig,
    redirect_uri: str,
    *,
    extra_params: Mapping[str, str] | None = None,
    now: Callable[[], float] = time.time,
) -> PendingAuthorization:
    """Build the authorize URL: response_type=code, client_id, redirect_uri, state
    (``token_urlsafe(24)``), code_challenge + S256, scope (if set), resource
    (issuer + resource_path, if set), then ``extra_params`` (e.g. rp's
    ``label``; ``ValueError`` if one names a :data:`RESERVED` key).
    ``DiscoveryError`` if the server has no authorization_endpoint."""
    if not meta.authorization_endpoint:
        raise DiscoveryError(
            f"{meta.server} does not offer browser sign-in (no authorization_endpoint).",
            server=meta.server,
        )
    extra = dict(extra_params or {})
    clash = sorted(RESERVED & set(extra))
    if clash:
        raise ValueError(f"extra_params may not set {', '.join(clash)}")
    verifier, challenge = new_pkce()
    state = secrets.token_urlsafe(24)
    resource = resource_for(meta.issuer, config)
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if config.scope:
        params["scope"] = config.scope
    if resource:
        params["resource"] = resource
    params.update(extra)
    endpoint = meta.authorization_endpoint
    return _pending(
        meta,
        config,
        redirect_uri,
        state=state,
        code_verifier=verifier,
        created_at=int(now()),
        authorization_url=endpoint + ("&" if "?" in endpoint else "?") + urlencode(params),
    )


def _pending(
    meta: ServerMetadata,
    config: ClientConfig,
    redirect_uri: str,
    *,
    state: str,
    code_verifier: str,
    created_at: int,
    authorization_url: str = "",
) -> PendingAuthorization:
    return PendingAuthorization(
        server=meta.server,
        issuer=meta.issuer,
        authorization_url=authorization_url,
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
        token_endpoint=meta.token_endpoint,
        revocation_endpoint=meta.revocation_endpoint,
        resource=resource_for(meta.issuer, config),
        scope=config.scope,
        require_iss=meta.iss_parameter_supported,
        created_at=created_at,
    )


def resume_authorization(
    meta: ServerMetadata, config: ClientConfig, saved: Mapping[str, Any]
) -> PendingAuthorization:
    """The attempt ``PendingAuthorization.to_dict()`` saved, rebuilt against the
    server's metadata (a web app keeps only that small dict between the redirect
    and the callback). Extra keys in ``saved`` are ignored. ValueError on a bad shape."""
    if not isinstance(saved, Mapping):
        raise ValueError("not a saved sign-in attempt")
    for name in ("redirect_uri", "state", "code_verifier"):
        if not isinstance(saved.get(name), str) or not saved[name]:
            raise ValueError(f"saved sign-in attempt has a missing or bad {name}")
    created_at = saved.get("created_at")
    if isinstance(created_at, bool) or not isinstance(created_at, int | float):
        raise ValueError("saved sign-in attempt has a missing or bad created_at")
    return _pending(
        meta,
        config,
        saved["redirect_uri"],
        state=saved["state"],
        code_verifier=saved["code_verifier"],
        created_at=int(created_at),
    )


def blank_credential(
    *,
    server: str,
    issuer: str,
    client_id: str,
    token_endpoint: str,
    revocation_endpoint: str | None,
    resource: str | None,
    scope: str | None,
) -> Credential:
    """A credential with no tokens yet: the template a first token response fills in."""
    return Credential(
        server=server,
        issuer=issuer,
        client_id=client_id,
        token_endpoint=token_endpoint,
        revocation_endpoint=revocation_endpoint,
        resource=resource,
        scope=scope,
        access_token="",
        access_expires_at=None,
        refresh_token=None,
        obtained_at=0,
    )


def complete_authorization(
    pending: PendingAuthorization,
    params: Mapping[str, str],
    config: ClientConfig,
    *,
    http: Http,
    now: Callable[[], float] = time.time,
    max_age: float = 900,
) -> Credential:
    """Check the browser's answer and trade its code for a credential (not stored).

    In order: the attempt is not older than ``max_age``; ``state`` matches;
    ``iss`` (RFC 9207) matches when present, and is present when the server
    promised it (else :class:`IssuerMismatch`); ``error`` (``access_denied`` is
    :class:`LoginDenied`); a code. Then the code trade at the token endpoint
    (``config.route``-d to the backchannel when set).
    """
    hint = config.relogin_hint
    server = pending.server
    if now() - pending.created_at > max_age:
        raise LoginFailed(f"This sign-in attempt expired. {hint}", server=server)
    state = params.get("state") or ""
    if not secrets.compare_digest(state.encode(), pending.state.encode()):
        raise LoginFailed(
            f"The browser answered for a different sign-in attempt. {hint}", server=server
        )
    iss = params.get("iss")
    if iss is not None and iss.rstrip("/") != pending.issuer.rstrip("/"):
        raise IssuerMismatch(
            f"The browser's answer came from {iss}, not {pending.issuer}. {hint}", server=server
        )
    if iss is None and pending.require_iss:
        raise IssuerMismatch(
            f"The browser's answer did not say which server sent it (no iss). {hint}",
            server=server,
        )
    error = params.get("error")
    if error == "access_denied":
        raise LoginDenied(f"Sign-in to {server} was refused in the browser.", server=server)
    if error:
        desc = params.get("error_description")
        raise LoginFailed(
            f"Sign-in to {server} failed ({error}{': ' + desc if desc else ''}). {hint}",
            server=server,
        )
    code = params.get("code")
    if not code:
        raise LoginFailed(f"The browser's answer carried no sign-in code. {hint}", server=server)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "client_id": config.client_id,
        "code_verifier": pending.code_verifier,
    }
    if pending.resource:
        form["resource"] = pending.resource
    headers = client_auth(form, config)
    url = config.route(pending.token_endpoint)
    try:
        # Never retried: a code works once, and a second presentation revokes
        # whatever the first one minted.
        resp = http.request("POST", url, form=form, headers=headers, timeout=config.timeout)
    except TransportError as e:
        raise server_unavailable(server, str(e), renewing=False) from None
    template = blank_credential(
        server=server,
        issuer=pending.issuer,
        client_id=config.client_id,
        token_endpoint=pending.token_endpoint,
        revocation_endpoint=pending.revocation_endpoint,
        resource=pending.resource,
        scope=pending.scope,
    )
    return parse_token_response(resp, template, config=config, during="code", now=now)


def _classify(
    resp: HttpResponse, during: During, server: str, config: ClientConfig, now: float
) -> OAuthClientError:
    """The error for a token-endpoint response that is not a 200."""
    body = resp.json() or {}
    error = body.get("error") if isinstance(body.get("error"), str) else None
    desc = body.get("error_description") if isinstance(body.get("error_description"), str) else None
    hint = config.relogin_hint
    if resp.status == 400 and error == "invalid_grant":
        if during == "refresh":
            return login_ended(server, config, reason="refused", ended_at=int(now))
        if during == "device":
            return LoginFailed(f"The login code expired or was already used. {hint}", server=server)
        return LoginFailed(f"The server refused the sign-in code. {hint}", server=server)
    if resp.status in (400, 401) and error in MISCONFIGURED:
        return client_misconfigured(server, config, error, desc)
    if resp.status == 429 or resp.status >= 500:
        return server_unavailable(
            server,
            f"HTTP {resp.status}" + (f" {error}" if error else ""),
            retry_after=retry_after(resp),
            renewing=during == "refresh",
        )
    what = f"HTTP {resp.status}" + (f" {error}" if error else "") + (f": {desc}" if desc else "")
    if during == "refresh":
        return server_unavailable(server, what)
    return LoginFailed(f"Sign-in to {server} failed ({what}). {hint}", server=server)


def parse_token_response(
    resp: HttpResponse,
    previous: Credential,
    *,
    config: ClientConfig,
    during: During,
    now: Callable[[], float] = time.time,
) -> Credential:
    """A credential from a token response (RFC 6749 section 5.1).

    ``previous`` is the login being renewed (``during="refresh"``) or a
    :func:`blank_credential` template. A missing ``refresh_token`` keeps
    ``previous.refresh_token`` (RFC 6749 section 6). An ``account`` object (a
    snapgate server names the person to its registered clients) is kept in
    ``extra["account"]``. Error bodies raise (see the table in the README).
    """
    t = now()
    server = previous.server
    if resp.status != 200:
        raise _classify(resp, during, server, config, t)
    body = resp.json() or {}
    access = body.get("access_token")
    token_type = body.get("token_type")
    if (
        not isinstance(access, str)
        or not access
        or (token_type is not None and str(token_type).lower() != "bearer")
    ):
        why = "the server answered with an unusable token response"
        if during == "refresh":
            raise server_unavailable(server, why)
        raise LoginFailed(
            f"Sign-in to {server} failed ({why}). {config.relogin_hint}", server=server
        )
    expires_in = body.get("expires_in")
    expires_at = (
        int(t + float(expires_in))
        if isinstance(expires_in, int | float) and not isinstance(expires_in, bool)
        else None
    )
    refresh = body.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        refresh = previous.refresh_token
    scope = body.get("scope") if isinstance(body.get("scope"), str) else previous.scope
    extra = previous.extra
    # A snapgate server names the person to registered clients (``account``).
    if isinstance(body.get("account"), dict):
        extra = {**previous.extra, "account": body["account"]}
    renewing = during == "refresh"
    return replace(
        previous,
        access_token=access,
        access_expires_at=expires_at,
        refresh_token=refresh,
        scope=scope,
        extra=extra,
        obtained_at=previous.obtained_at if renewing else int(t),
        refreshed_at=int(t) if renewing else None,
        ended_at=None,
        ended_reason=None,
    )


def refresh_credential(
    cred: Credential,
    config: ClientConfig,
    *,
    http: Http,
    now: Callable[[], float] = time.time,
) -> Credential:
    """Trade ``cred``'s refresh token for a new pair (not stored).

    Never sends ``scope``. On a :class:`TransportError` it retries once, at
    once, with the same token: if the first try reached the server and
    rotated, the token is retired but inside its 120 s grace window, so the
    retry is accepted. A second ``TransportError`` raises ``ServerUnavailable``.
    ``invalid_grant`` (revoked, lapsed, or a replay) raises ``LoginEnded``.
    Call it only while holding the store's lock (see :class:`TokenSource`).
    """
    if not cred.refresh_token:
        raise login_ended(cred.server, config, reason="no_refresh_token", ended_at=int(now()))
    form = {
        "grant_type": "refresh_token",
        "refresh_token": cred.refresh_token,
        "client_id": cred.client_id,
    }
    if cred.resource:
        form["resource"] = cred.resource
    headers = client_auth(form, config, cred.client_id)
    url = config.route(cred.token_endpoint)
    resp = None
    for attempt in (1, 2):
        try:
            resp = http.request("POST", url, form=form, headers=headers, timeout=config.timeout)
            break
        except TransportError as e:
            if attempt == 2:
                raise server_unavailable(cred.server, str(e)) from None
    return parse_token_response(resp, cred, config=config, during="refresh", now=now)


def revoke_credential(cred: Credential, config: ClientConfig, *, http: Http) -> bool:
    """RFC 7009: POST ``token=<refresh token>``, ``token_type_hint=refresh_token``,
    ``client_id`` (+ client auth). True on 200. False with no refresh token or no
    revocation endpoint. ``TransportError`` -> False (best effort; callers say so)."""
    if not cred.refresh_token or not cred.revocation_endpoint:
        return False
    form = {
        "token": cred.refresh_token,
        "token_type_hint": "refresh_token",
        "client_id": cred.client_id,
    }
    headers = client_auth(form, config, cred.client_id)
    url = config.route(cred.revocation_endpoint)
    try:
        resp = http.request("POST", url, form=form, headers=headers, timeout=config.timeout)
    except TransportError:
        return False
    return resp.status == 200
