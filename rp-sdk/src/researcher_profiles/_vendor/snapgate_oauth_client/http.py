# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""The only network seam: a tiny ``Http`` protocol and its ``urllib`` default.

Tests and hosts can pass their own ``Http`` (anything with a matching
``request`` method) wherever the client takes ``http=``.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode


@dataclass(frozen=True)
class HttpResponse:
    """A response. ``headers`` names are lower-cased on construction."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", {k.lower(): v for k, v in dict(self.headers).items()})

    def json(self) -> dict | None:
        """The body as a JSON object, else None (not JSON, or not an object)."""
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None


class TransportError(Exception):
    """The request failed on the network; it may or may not have reached the server."""


class Http(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        form: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None  # a 3xx comes back as an HTTPError, then as a response


class UrllibHttp:
    """Default. Form-encoded POST bodies, ``Accept: application/json``, a User-Agent
    of ``snapgate-oauth-client/<version> (<app_name>)``. Follows NO redirects (a 3xx
    comes back as a response). 4xx/5xx come back as responses. ``URLError``,
    ``OSError``, ``TimeoutError`` and ``http.client.HTTPException`` become
    :class:`TransportError`."""

    def __init__(self, app_name: str = "unknown"):
        from . import __version__

        self.user_agent = f"snapgate-oauth-client/{__version__} ({app_name})"
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        *,
        form: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> HttpResponse:
        data = None
        sent = {"Accept": "application/json", "User-Agent": self.user_agent}
        if form is not None:
            data = urlencode(dict(form)).encode("ascii")
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        sent.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=sent, method=method.upper())
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return HttpResponse(resp.status, dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except (OSError, http.client.HTTPException):
                body = b""
            finally:
                e.close()
            return HttpResponse(e.code, dict(e.headers.items()) if e.headers else {}, body)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            reason = getattr(e, "reason", None) or e
            raise TransportError(str(reason) or type(e).__name__) from None
