# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""The device authorization grant (RFC 8628), client side.

Used only when the server's metadata lists ``device_authorization_endpoint``
(Prosopia does; snapgate apps do not). It prints nothing: the caller shows the
link and the code.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from .errors import (
    DiscoveryError,
    LoginDenied,
    LoginFailed,
    client_misconfigured,
    server_unavailable,
)
from .grants import (
    MISCONFIGURED,
    blank_credential,
    client_auth,
    parse_token_response as _parse,
    resource_for,
    retry_after,
)
from .http import Http, HttpResponse, TransportError
from .models import ClientConfig, Credential, ServerMetadata

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_INTERVAL = 5.0  # RFC 8628 section 3.2, when the server sends none
SLOW_DOWN_STEP = 5.0  # RFC 8628 section 3.5
START_ATTEMPTS = 3  # tries at the device endpoint while it answers 429 or 503


@dataclass(frozen=True)
class DeviceCode:
    """A started device login: what to show the person, and what to poll with."""

    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    interval: float
    expires_at: float
    device_code: str = field(repr=False)


def _describe(resp: HttpResponse) -> str:
    body = resp.json() or {}
    error = body.get("error")
    return f"HTTP {resp.status}" + (f" {error}" if isinstance(error, str) else "")


def start_device_authorization(
    meta: ServerMetadata,
    config: ClientConfig,
    *,
    http: Http,
    extra_params: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> DeviceCode:
    """POST the device endpoint with ``client_id``, ``scope`` and ``extra_params``.

    Retries up to 3 times on 429 or 503, waiting ``Retry-After`` or 5 s. A 404
    or a missing endpoint raises ``DiscoveryError``.
    """
    server = meta.server
    endpoint = meta.device_authorization_endpoint
    no_code = DiscoveryError(f"{server} does not offer a login code.", server=server)
    if not endpoint:
        raise no_code
    form = {"client_id": config.client_id}
    if config.scope:
        form["scope"] = config.scope
    form.update(extra_params or {})
    headers = client_auth(form, config)
    resp = None
    for attempt in range(START_ATTEMPTS):
        try:
            resp = http.request(
                "POST", endpoint, form=form, headers=headers, timeout=config.timeout
            )
        except TransportError as e:
            raise server_unavailable(server, str(e), renewing=False) from None
        if resp.status not in (429, 503) or attempt == START_ATTEMPTS - 1:
            break
        sleep(retry_after(resp) or DEFAULT_INTERVAL)
    if resp.status == 404:
        raise no_code
    body = resp.json() or {}
    if resp.status in (400, 401) and body.get("error") in MISCONFIGURED:
        raise client_misconfigured(server, config, body["error"], body.get("error_description"))
    if resp.status != 200:
        raise LoginFailed(
            f"{server} refused the login code request ({_describe(resp)}). {config.relogin_hint}",
            server=server,
        )
    try:
        interval = body.get("interval")
        return DeviceCode(
            user_code=str(body["user_code"]),
            verification_uri=str(body["verification_uri"]),
            verification_uri_complete=body.get("verification_uri_complete") or None,
            interval=DEFAULT_INTERVAL if interval is None else float(interval),
            expires_at=now() + float(body["expires_in"]),
            device_code=str(body["device_code"]),
        )
    except (KeyError, TypeError, ValueError):
        raise LoginFailed(
            f"{server} answered the login code request with something unreadable.", server=server
        ) from None


def poll_device_token(
    meta: ServerMetadata,
    config: ClientConfig,
    code: DeviceCode,
    *,
    http: Http,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    timeout: float = 600,
) -> Credential:
    """Poll the token endpoint until the person approves, refuses, or time runs out.

    Every ``interval`` seconds until ``min(timeout, expires_in)``.
    ``authorization_pending``: keep waiting. ``slow_down``: add 5 s for good. A
    429: ``max(interval + 5, Retry-After)``. ``access_denied``:
    :class:`LoginDenied`. ``expired_token`` or ``invalid_grant``:
    :class:`LoginFailed`.
    """
    server = meta.server
    hint = config.relogin_hint
    resource = resource_for(meta.issuer, config)
    form = {
        "grant_type": DEVICE_GRANT_TYPE,
        "device_code": code.device_code,
        "client_id": config.client_id,
    }
    if resource:
        form["resource"] = resource
    headers = client_auth(form, config)
    template = blank_credential(
        server=server,
        issuer=meta.issuer,
        client_id=config.client_id,
        token_endpoint=meta.token_endpoint,
        revocation_endpoint=meta.revocation_endpoint,
        resource=resource,
        scope=config.scope,
    )
    url = config.route(meta.token_endpoint)
    interval = code.interval
    deadline = min(now() + timeout, code.expires_at)
    while now() < deadline:
        sleep(interval)
        try:
            resp = http.request("POST", url, form=form, headers=headers, timeout=config.timeout)
        except TransportError as e:
            raise server_unavailable(server, str(e), renewing=False) from None
        if resp.status == 200:
            return _parse(resp, template, config=config, during="device", now=now)
        if resp.status == 429:
            interval = max(interval + SLOW_DOWN_STEP, retry_after(resp) or 0.0)
            continue
        error = (resp.json() or {}).get("error") if resp.status == 400 else None
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += SLOW_DOWN_STEP
            continue
        if error == "access_denied":
            raise LoginDenied(f"The login to {server} was refused in the browser.", server=server)
        if error in ("expired_token", "invalid_grant"):
            raise LoginFailed(f"The login code expired or was already used. {hint}", server=server)
        raise LoginFailed(
            f"The login to {server} failed ({_describe(resp)}). {hint}", server=server
        )
    raise LoginFailed(f"Timed out waiting for the login to be approved. {hint}", server=server)
