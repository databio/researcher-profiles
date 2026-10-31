# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""Finding a server's sign-in: RFC 8414 metadata, with an RFC 9728 fallback."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .errors import DiscoveryError, SignInNotNeeded, server_unavailable
from .http import Http, HttpResponse, TransportError
from .models import ClientConfig, ServerMetadata

WELL_KNOWN = "/.well-known/oauth-authorization-server"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
_ENDPOINTS = (
    "authorization_endpoint",
    "token_endpoint",
    "revocation_endpoint",
    "device_authorization_endpoint",
)
_RESOURCE_METADATA = re.compile(r'resource_metadata="([^"]+)"')


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in LOOPBACK_HOSTS


def normalize_server(url: str) -> str:
    """Strip spaces and trailing '/'. Must be https, except loopback hosts
    (localhost, 127.0.0.1, ::1) for development. No query or fragment."""
    text = (url or "").strip().rstrip("/")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DiscoveryError(f"{url!r} is not a server address (use https://...).")
    if parts.query or parts.fragment:
        raise DiscoveryError(f"{text} has a query or fragment; give the server's base address.")
    if parts.scheme == "http" and not is_loopback(text):
        raise DiscoveryError(f"{text} must use https (http is allowed only for localhost).")
    return text


def _get(http: Http, url: str, server: str, config: ClientConfig) -> HttpResponse:
    try:
        return http.request("GET", url, timeout=config.timeout)
    except TransportError as e:
        raise server_unavailable(server, str(e), renewing=False) from None


def _metadata_at(origin: str, http: Http, server: str, config: ClientConfig) -> dict | None:
    """The metadata document at ``origin``, or None when there is none.

    A 404, a 3xx, another 4xx, or a body that is not a JSON object (some hosts
    answer every path with an HTML page) all mean "none". A 429 or 5xx is
    ``ServerUnavailable``.
    """
    resp = _get(http, origin + WELL_KNOWN, server, config)
    if resp.status == 429 or resp.status >= 500:
        raise server_unavailable(server, f"HTTP {resp.status}", renewing=False)
    return resp.json() if resp.status == 200 else None


def _via_probe(server: str, config: ClientConfig, http: Http) -> tuple[str, dict | None]:
    """RFC 9728: the probe path's 401 names the protected-resource metadata,
    which names the authorization server. ``(issuer origin, metadata or None)``."""
    resp = _get(http, server + config.probe_path, server, config)
    if resp.status == 200:
        raise SignInNotNeeded(
            f"{server} runs without sign-in; nothing to sign in to.", server=server
        )
    m = _RESOURCE_METADATA.search(resp.headers.get("www-authenticate", ""))
    if resp.status != 401 or not m:
        return server, None
    pr = _get(http, m.group(1), server, config).json() or {}
    servers = pr.get("authorization_servers")
    if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
        return server, None
    try:
        auth_server = normalize_server(servers[0])
    except DiscoveryError:
        return server, None
    origin = _origin(auth_server)
    return origin, _metadata_at(origin, http, server, config)


def discover(server: str, config: ClientConfig, *, http: Http) -> ServerMetadata:
    """The server's checked OAuth metadata.

    1. ``GET <server>/.well-known/oauth-authorization-server``.
    2. If there is none and ``config.probe_path`` is set: the RFC 9728 fallback
       (a 200 there raises :class:`SignInNotNeeded`).
    3. Still none: :class:`DiscoveryError`.
    4. Checks: ``issuer`` equals ``server``'s origin (RFC 8414 section 3.3;
       issuers with a path are not supported); a token endpoint; https
       endpoints unless the issuer is a loopback host; ``S256`` when
       ``code_challenge_methods_supported`` is listed.

    With ``config.backchannel_url`` set, the metadata is fetched from
    ``config.route(<origin> + WELL_KNOWN)`` (``http://mygrants:8128`` inside a
    docker network; no probe fallback). The issuer must still be ``server``'s
    origin, and the endpoints come back as published (the grant calls route
    their own token and revoke requests).
    """
    server = normalize_server(server)
    origin = _origin(server)
    if config.backchannel_url:
        raw = _metadata_at(config.route(origin), http, server, config)
    else:
        raw = _metadata_at(origin, http, server, config)
        if raw is None and config.probe_path:
            origin, raw = _via_probe(server, config, http)
    if raw is None:
        raise DiscoveryError(
            f"{server} does not offer sign-in (no {WELL_KNOWN}).",
            server=server,
        )
    issuer = raw.get("issuer")
    if not isinstance(issuer, str) or issuer.rstrip("/") != origin:
        raise DiscoveryError(
            f"{server}: its sign-in metadata names issuer {issuer!r}, not {origin}.",
            server=server,
        )
    issuer = issuer.rstrip("/")
    endpoints: dict[str, str | None] = {}
    for name in _ENDPOINTS:
        value = raw.get(name)
        if value in (None, ""):
            endpoints[name] = None
            continue
        if not isinstance(value, str):
            raise DiscoveryError(f"{server}: its sign-in metadata has a bad {name}.", server=server)
        scheme = urlsplit(value).scheme
        if scheme != "https" and not (scheme == "http" and is_loopback(issuer)):
            raise DiscoveryError(f"{server}: its {name} is not https ({value}).", server=server)
        endpoints[name] = value
    if endpoints["token_endpoint"] is None:
        raise DiscoveryError(
            f"{server}: its sign-in metadata has no token_endpoint.", server=server
        )
    methods = raw.get("code_challenge_methods_supported")
    if methods is not None and (not isinstance(methods, list) or "S256" not in methods):
        raise DiscoveryError(
            f"{server} does not support PKCE with S256, which sign-in requires.", server=server
        )
    return ServerMetadata(
        server=server,
        issuer=issuer,
        authorization_endpoint=endpoints["authorization_endpoint"],
        token_endpoint=endpoints["token_endpoint"],
        revocation_endpoint=endpoints["revocation_endpoint"],
        device_authorization_endpoint=endpoints["device_authorization_endpoint"],
        iss_parameter_supported=raw.get("authorization_response_iss_parameter_supported") is True,
        raw=raw,
    )
