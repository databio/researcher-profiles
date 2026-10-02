"""The one HTTP client for OpenAlex, and the only holder of an OpenAlex API key.

Every OpenAlex request in rp-sdk (and in the services built on it) goes
through :class:`OpenAlexClient`. Callers build a client once and pass the
object around; no other function takes or reads a key string.

The key goes in the ``Authorization: Bearer`` header and nowhere else: never a
URL, query param, log line, exception message, or repr. Since February 2026
OpenAlex requires a key (no key gets about $0.10 of usage a day, a free key
$1); the old ``mailto`` "polite pool" address is ignored, so it is gone too.

Errors are mapped to three types whose messages carry only a status and a
path, never the request URL (which holds search terms) or the key:

- :class:`OpenAlexBudgetError`: HTTP 429, the daily budget is used up or the
  caller sent more than 100 requests a second.
- :class:`OpenAlexAuthError`: HTTP 401, the key is missing or rejected.
- :class:`OpenAlexHTTPError`: any other failure (4xx/5xx, transport, bad JSON).

``httpx`` is imported when a client is built (the ``client`` extra), so the
error types import on a core-only install.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

OPENALEX_BASE_URL = "https://api.openalex.org"

# httpx logs every request URL at INFO; keep those lines out of service logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

_FORBIDDEN_PARAMS = {"api_key", "mailto"}


class OpenAlexError(Exception):
    """Base for every OpenAlex failure raised by :class:`OpenAlexClient`."""


class OpenAlexBudgetError(OpenAlexError):
    """HTTP 429: the daily budget is used up, or more than 100 requests/s."""

    def __init__(self, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class OpenAlexAuthError(OpenAlexError):
    """HTTP 401: the API key is missing or OpenAlex rejected it."""


class OpenAlexHTTPError(OpenAlexError):
    """Any other failure; the message is a status and a path only."""


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class OpenAlexClient:
    """The only code that sends HTTP to OpenAlex, and the only holder of the key.

    The key goes in the ``Authorization: Bearer`` header and nowhere else:
    never a URL, query param, log line, exception message, or repr.

    ``cost_usd`` sums ``meta.cost_usd`` over this client's calls;
    ``remaining_usd`` is the account's budget left today, from the newest
    ``x-ratelimit-remaining-usd`` header. :meth:`metered` gives a view with
    its own ``cost_usd`` over the same connection and key, for counting one
    run's cost on a shared client.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = OPENALEX_BASE_URL,
        timeout: float = 30.0,
        transport: Any = None,
    ):
        try:
            import httpx
        except ImportError as e:
            raise ImportError(
                "OpenAlexClient requires httpx; requires the 'client' extra, see the "
                "install instructions in the README"
            ) from e
        self.__key = (api_key or "").strip() or None
        headers = {"Authorization": f"Bearer {self.__key}"} if self.__key else {}
        # No redirects: a redirect to another host must not carry the key.
        self._http = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )
        self._owns_http = True
        self._parent: OpenAlexClient | None = None
        self._lock = threading.Lock()
        self.cost_usd = 0.0
        self.remaining_usd: float | None = None

    @property
    def has_key(self) -> bool:
        return self.__key is not None

    def __repr__(self) -> str:
        return f"OpenAlexClient(has_key={self.has_key})"

    __str__ = __repr__

    def metered(self) -> OpenAlexClient:
        """A view on this client with its own ``cost_usd``, starting at 0.

        It shares the connection and key; its calls also count toward this
        client's ``cost_usd`` and update its ``remaining_usd``.
        """
        view = object.__new__(OpenAlexClient)
        view.__key = self.__key
        view._http = self._http
        view._owns_http = False
        view._parent = self
        view._lock = threading.Lock()
        view.cost_usd = 0.0
        view.remaining_usd = self.remaining_usd
        return view

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> OpenAlexClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET ``path`` (relative to the base URL) and return the JSON body."""
        import httpx

        params = dict(params or {})
        if any(str(k).lower().replace("-", "_") in _FORBIDDEN_PARAMS for k in params):
            raise ValueError("credentials go in the header, not in params")
        try:
            r = self._http.get(path, params=params)
        except httpx.HTTPError as e:
            raise OpenAlexHTTPError(f"OpenAlex {path}: {type(e).__name__}") from None
        self._note_usage(r)
        if r.status_code == 429:
            retry = _float(r.headers.get("retry-after") or r.headers.get("x-ratelimit-reset"))
            raise OpenAlexBudgetError(
                f"OpenAlex {path}: HTTP 429 (daily budget used up or too many requests)",
                retry_after=int(retry) if retry is not None else None,
            )
        if r.status_code == 401:
            raise OpenAlexAuthError("OpenAlex rejected the API key")
        if r.status_code >= 300:  # 3xx too: redirects are never followed
            raise OpenAlexHTTPError(f"OpenAlex {path}: HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise OpenAlexHTTPError(f"OpenAlex {path}: response is not JSON") from None
        if not isinstance(body, dict):
            raise OpenAlexHTTPError(f"OpenAlex {path}: unexpected response shape")
        self._add_cost(_float((body.get("meta") or {}).get("cost_usd")) or 0.0)
        return body

    def rate_limit(self) -> dict:
        """``GET /rate-limit``: today's budget for this key. Free; 401 means a bad key."""
        body = self.get("/rate-limit")
        if self.remaining_usd is None:
            found = _find_remaining_usd(body)
            if found is not None:
                self._set_remaining(found)
        return body

    def _note_usage(self, r) -> None:
        remaining = _float(r.headers.get("x-ratelimit-remaining-usd"))
        if remaining is not None:
            self._set_remaining(remaining)

    def _set_remaining(self, value: float) -> None:
        self.remaining_usd = value
        if self._parent is not None:
            self._parent._set_remaining(value)

    def _add_cost(self, cost: float) -> None:
        with self._lock:
            self.cost_usd += cost
        if self._parent is not None:
            self._parent._add_cost(cost)


def _find_remaining_usd(body: Any) -> float | None:
    """The first ``remaining_usd``-like number in a ``/rate-limit`` body."""
    if isinstance(body, dict):
        for k, v in body.items():
            key = str(k).lower()
            if "remaining" in key and "usd" in key and "prepaid" not in key:
                found = _float(v)
                if found is not None:
                    return found
        for v in body.values():
            found = _find_remaining_usd(v)
            if found is not None:
                return found
    return None


__all__ = [
    "OPENALEX_BASE_URL",
    "OpenAlexAuthError",
    "OpenAlexBudgetError",
    "OpenAlexClient",
    "OpenAlexError",
    "OpenAlexHTTPError",
]
