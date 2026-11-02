# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""``FakeAuthorizationServer``: an in-memory server with snapgate's rules, for tests.

It implements :class:`~.http.Http`, so pass it as ``http=``. Its
:meth:`~FakeAuthorizationServer.opener` stands in for the browser: it "signs
in", then really GETs the redirect on the :class:`LoopbackReceiver`. Host test
suites use it instead of writing their own fake.

Enforced: the metadata ``issuer``; exact ``client_id``, ``redirect_uri`` and
``resource`` matches; PKCE S256; single-use codes (a replayed code revokes the
connection it minted); refresh rotation with a grace window, where a retired
token shown after its window revokes the whole connection and an overlapping
refresh inherits the window; RFC 7009 revoke (always 200); the device grant.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from .http import HttpResponse, TransportError

_LOOPBACK_REDIRECT = re.compile(r"http://(127\.0\.0\.1|\[::1\]):([0-9]{1,5})/callback")
_CHALLENGE = re.compile(r"[A-Za-z0-9\-_]{43}")
_VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _json(status: int, body: dict, headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        status,
        {"content-type": "application/json", "cache-control": "no-store", **(headers or {})},
        json.dumps(body).encode(),
    )


def _error(error: str, status: int = 400, description: str | None = None) -> HttpResponse:
    body = {"error": error}
    if description:
        body["error_description"] = description
    return _json(status, body)


@dataclass
class FakeConnection:
    """One connection (a login): its current tokens and whether it is revoked."""

    id: int
    client_id: str
    scope: str | None
    resource: str | None
    refresh_token: str
    access_tokens: dict[str, float] = field(default_factory=dict)  # token -> expires at
    revoked: bool = False
    #: Retired refresh tokens -> the end of their grace window.
    history: dict[str, float] = field(default_factory=dict)


class FakeAuthorizationServer:
    """An OAuth server in memory, for tests. Implements ``Http``."""

    def __init__(
        self,
        issuer: str = "https://auth.example.org",
        *,
        client_id: str,
        scope: str | None = None,
        resource_path: str | None = None,
        device: bool = False,
        iss: bool = True,
        access_ttl: int = 3600,
        refresh_grace: int = 120,
        now: Callable[[], float] = time.time,
    ):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.scope = scope
        self.resource = self.issuer + resource_path if resource_path else None
        self.iss = iss
        self.access_ttl = access_ttl
        self.refresh_grace = refresh_grace
        self.now = now
        self.device_interval: float = 5
        #: What was sent, tokens included: ``("POST /oauth/token", form)``.
        self.calls: list[tuple[str, dict]] = []
        self.connections: list[FakeConnection] = []
        self.metadata: dict = {
            "issuer": self.issuer,
            "authorization_endpoint": self.issuer + "/oauth/authorize",
            "token_endpoint": self.issuer + "/oauth/token",
            "revocation_endpoint": self.issuer + "/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": iss,
        }
        if device:
            self.metadata["device_authorization_endpoint"] = self.issuer + "/oauth/device"
            self.metadata["grant_types_supported"].append(DEVICE_GRANT_TYPE)
        #: The last page the stand-in browser read from the loopback receiver.
        self.last_page: bytes | None = None
        self._codes: dict[str, dict] = {}
        self._spent_codes: dict[str, int | None] = {}  # code -> connection it minted
        self._devices: dict[str, dict] = {}
        self._device_plan: tuple[int, str] | None = None
        self._failures: list = []
        self._serial = 0

    # -- test controls ---------------------------------------------------------------

    def fail_next(self, what, *, reached: bool = False) -> None:
        """The next request fails: a status (``int``), a full ``HttpResponse``, or an
        exception class (e.g. ``TransportError``). With ``reached=True`` and an
        exception, the request is handled first and its answer lost."""
        self._failures.append((what, reached))

    def approve_device(self, *, after_polls: int = 0, decision: str = "approve") -> None:
        """Approve (or ``decision="deny"``) every device login, now and later,
        after answering ``authorization_pending`` ``after_polls`` times."""
        self._device_plan = (after_polls, decision)
        for row in self._devices.values():
            row["pending"], row["decision"] = after_polls, decision

    def revoke_all(self) -> None:
        for conn in self.connections:
            conn.revoked = True

    def live_connections(self) -> list[FakeConnection]:
        return [c for c in self.connections if not c.revoked]

    def access_ok(self, token: str) -> bool:
        """Whether ``token`` is a live access token (unexpired, connection not revoked)."""
        for conn in self.connections:
            expires = conn.access_tokens.get(token)
            if expires is not None:
                return not conn.revoked and self.now() < expires
        return False

    # -- the stand-in browser --------------------------------------------------------

    def authorize(self, url: str, *, decision: str = "approve") -> str:
        """The consent page and the person's decision: the redirect URL it sends back."""
        parts = urlsplit(url)
        assert (
            f"{parts.scheme}://{parts.netloc}{parts.path}"
            == self.metadata["authorization_endpoint"]
        ), url
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        self.calls.append(("GET /oauth/authorize", q))
        assert q.get("client_id") == self.client_id, f"client_id {q.get('client_id')!r}"
        assert q.get("response_type") == "code"
        redirect = q.get("redirect_uri", "")
        m = _LOOPBACK_REDIRECT.fullmatch(redirect)
        assert m and 0 < int(m.group(2)) < 65536, f"redirect_uri {redirect!r}"
        assert q.get("code_challenge_method") == "S256"
        assert _CHALLENGE.fullmatch(q.get("code_challenge", "")), "code_challenge"
        assert q.get("resource") == self.resource, f"resource {q.get('resource')!r}"
        if q.get("scope"):
            assert q["scope"] == self.scope, f"scope {q['scope']!r}"
        back = {"state": q.get("state", "")}
        if decision == "approve":
            code = f"code_{secrets.token_hex(8)}"
            self._codes[code] = q
            back["code"] = code
        else:
            back["error"] = "access_denied"
        if self.iss:
            back["iss"] = self.issuer
        return redirect + "?" + urlencode(back)

    def opener(self, *, decision: str = "approve") -> Callable[[str], bool]:
        """A stand-in browser: "signs in", then really GETs the redirect on the
        LoopbackReceiver with urllib, carrying code, state and iss."""

        def open_(url: str) -> bool:
            target = self.authorize(url, decision=decision)
            with urllib.request.urlopen(target, timeout=10) as resp:  # noqa: S310
                self.last_page = resp.read()
            return True

        return open_

    # -- Http ------------------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        form: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,  # noqa: ARG002
    ) -> HttpResponse:
        parts = urlsplit(url)
        method = method.upper()
        sent = dict(form or {})
        if method == "GET":
            sent.update(parse_qsl(parts.query))
        self.calls.append((f"{method} {parts.path}", sent))
        reached = False
        if self._failures:
            what, reached = self._failures.pop(0)
            if isinstance(what, int):
                return _error("temporarily_unavailable" if what in (429, 503) else "failed", what)
            if isinstance(what, HttpResponse):
                return what
            if not reached:
                raise what("connection refused")
        if f"{parts.scheme}://{parts.netloc}" != self.issuer:
            resp = _json(404, {"detail": "not this server"})
        else:
            resp = self._route(method, parts.path, sent, dict(headers or {}))
        if reached:
            raise what("connection reset after the request was sent")
        return resp

    def _route(self, method: str, path: str, form: dict, headers: dict) -> HttpResponse:
        if method == "GET" and path == "/.well-known/oauth-authorization-server":
            return _json(200, self.metadata)
        if method == "POST" and path == "/oauth/token":
            return self._token(form, headers)
        if method == "POST" and path == "/oauth/revoke":
            return self._revoke(form, headers)
        if (
            method == "POST"
            and path == "/oauth/device"
            and "device_authorization_endpoint" in self.metadata
        ):
            return self._device_start(form, headers)
        return _json(404, {"detail": "not found"})

    def _client_id(self, form: dict, headers: dict) -> str:
        auth = {k.lower(): v for k, v in headers.items()}.get("authorization", "")
        if auth.startswith("Basic "):
            user = base64.b64decode(auth[6:]).decode().split(":", 1)[0]
            return unquote(user)
        return form.get("client_id", "")

    def _mint(self, conn: FakeConnection) -> HttpResponse:
        self._serial += 1
        access = f"at_{self._serial}"
        conn.refresh_token = f"rt_{self._serial}"
        conn.access_tokens[access] = self.now() + self.access_ttl
        body = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.access_ttl,
            "refresh_token": conn.refresh_token,
        }
        if conn.scope:
            body["scope"] = conn.scope
        return _json(200, body)

    def _resource_ok(self, form: dict) -> bool:
        return "resource" not in form or form["resource"] == self.resource

    def _token(self, form: dict, headers: dict) -> HttpResponse:
        grant = form.get("grant_type")
        client_id = self._client_id(form, headers)
        if grant not in ("authorization_code", "refresh_token", DEVICE_GRANT_TYPE):
            return _error("unsupported_grant_type")
        if client_id != self.client_id:
            return _error("invalid_client", 401)
        if not self._resource_ok(form):
            return _error("invalid_target", description="resource is not this server's")
        if grant == "authorization_code":
            return self._redeem(form)
        if grant == "refresh_token":
            return self._refresh(form)
        return self._device_poll(form)

    def _redeem(self, form: dict) -> HttpResponse:
        code, verifier = form.get("code", ""), form.get("code_verifier", "")
        if not code or not _VERIFIER.fullmatch(verifier):
            return _error("invalid_request", description="code and a valid code_verifier")
        q = self._codes.pop(code, None)
        if q is None:
            minted = self._spent_codes.get(code)
            if minted is not None:
                self.connections[minted].revoked = True  # a replayed code leaked
            return _error("invalid_grant")
        self._spent_codes[code] = None
        if form.get("redirect_uri") != q["redirect_uri"] or _s256(verifier) != q["code_challenge"]:
            return _error("invalid_grant")
        conn = FakeConnection(
            id=len(self.connections),
            client_id=self.client_id,
            scope=self.scope,
            resource=self.resource,
            refresh_token="",
        )
        self.connections.append(conn)
        self._spent_codes[code] = conn.id
        return self._mint(conn)

    def _refresh(self, form: dict) -> HttpResponse:
        presented = form.get("refresh_token", "")
        if not presented:
            return _error("invalid_request", description="refresh_token is required")
        now = self.now()
        for conn in self.connections:
            if conn.refresh_token == presented:
                grace_until = now + self.refresh_grace
                break
            if presented in conn.history:
                if conn.history[presented] <= now:
                    conn.revoked = True  # a retired token after its window: it leaked
                    return _error("invalid_grant")
                grace_until = conn.history[presented]  # overlapping: inherit the window
                break
        else:
            return _error("invalid_grant")
        if conn.revoked:
            return _error("invalid_grant")
        if form.get("scope") and form["scope"] != conn.scope:
            return _error("invalid_scope")
        conn.history[conn.refresh_token] = grace_until
        return self._mint(conn)

    def _revoke(self, form: dict, headers: dict) -> HttpResponse:
        token = form.get("token", "")
        client_id = self._client_id(form, headers)
        for conn in self.connections:
            if (conn.refresh_token == token or token in conn.history) and (
                conn.client_id == client_id
            ):
                conn.revoked = True
        return _json(200, {})

    def _device_start(self, form: dict, headers: dict) -> HttpResponse:
        if self._client_id(form, headers) != self.client_id:
            return _error("invalid_client", 401)
        if form.get("scope") and form["scope"] != self.scope:
            return _error("invalid_scope")
        device_code = f"dc_{secrets.token_hex(8)}"
        pending, decision = self._device_plan or (10**9, "approve")
        self._devices[device_code] = {"pending": pending, "decision": decision, "used": False}
        verify = self.issuer + "/device"
        return _json(
            200,
            {
                "device_code": device_code,
                "user_code": "ABCD-EFGH",
                "verification_uri": verify,
                "verification_uri_complete": verify + "?user_code=ABCD-EFGH",
                "expires_in": 600,
                "interval": self.device_interval,
            },
        )

    def _device_poll(self, form: dict) -> HttpResponse:
        row = self._devices.get(form.get("device_code", ""))
        if row is None or row["used"]:
            return _error("expired_token")
        if row["pending"] > 0:
            row["pending"] -= 1
            return _error("authorization_pending")
        if row["decision"] != "approve":
            return _error("access_denied")
        row["used"] = True
        conn = FakeConnection(
            id=len(self.connections),
            client_id=self.client_id,
            scope=self.scope,
            resource=self.resource,
            refresh_token="",
        )
        self.connections.append(conn)
        return self._mint(conn)


__all__ = ["FakeAuthorizationServer", "FakeConnection", "TransportError"]
