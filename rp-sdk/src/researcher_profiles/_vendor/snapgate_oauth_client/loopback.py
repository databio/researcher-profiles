# SNAPGATE module - do not edit. Overwritten on updates. Configure via ClientConfig.
"""``LoopbackReceiver``: the ``http://127.0.0.1:<port>/callback`` listener (RFC 8252).

Only ``GET /callback`` is served. A callback for another sign-in attempt (a
different ``state``) gets a 400 page and the receiver keeps waiting, so a stray
or hostile request can neither end the login nor learn anything.
"""

from __future__ import annotations

import http.server
import queue
import secrets
import threading
from html import escape
from urllib.parse import parse_qs, urlsplit

from .errors import LoginFailed

CALLBACK_PATH = "/callback"


def _query(text: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(text, keep_blank_values=True).items()}


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;"
        f"padding:0 1rem'><h1>{escape(title)}</h1><p>{escape(body)}</p></body></html>"
    ).encode()


class _Server(http.server.HTTPServer):
    receiver: LoopbackReceiver


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Server

    def do_GET(self):  # noqa: N802 - http.server's name
        parts = urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            self._send(404, _page("Not found", "There is nothing here."))
            return
        receiver = self.server.receiver
        params = _query(parts.query)
        if not receiver._matches(params):
            self._send(
                400,
                _page(
                    "Wrong sign-in attempt",
                    "This page is for a different sign-in attempt. Return to your terminal.",
                ),
            )
            return
        app = receiver.app_name
        if params.get("error"):
            page = _page("Sign-in refused", "Sign-in refused. Return to your terminal.")
        else:
            page = _page(f"Signed in to {app}", f"Signed in to {app}. You can close this tab.")
        self._send(200, page)
        receiver._deliver(params)

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # no request logging: the query holds the code
        pass


class LoopbackReceiver:
    """``http://127.0.0.1:<port>/callback`` (port 0 = any free port), served on a
    daemon thread. Use as a context manager.

    Binds 127.0.0.1 only (never ``localhost``, matching snapgate's rule).
    """

    def __init__(
        self, *, expected_state: str | None = None, port: int = 0, app_name: str = "this app"
    ):
        self.app_name = app_name
        self._state = expected_state
        self._lock = threading.Lock()
        self._done = False
        self._replies: queue.Queue[dict[str, str]] = queue.Queue()
        self._server = _Server(("127.0.0.1", port), _Handler)
        self._server.receiver = self
        self.port: int = self._server.server_address[1]
        #: What snapgate's ``native_redirect_ok`` accepts.
        self.redirect_uri = f"http://127.0.0.1:{self.port}{CALLBACK_PATH}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="oauth-loopback", daemon=True
        )
        self._thread.start()
        self._closed = False

    def __enter__(self) -> LoopbackReceiver:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def expect(self, state: str) -> None:
        """The state of the attempt this receiver serves (set after begin_authorization)."""
        with self._lock:
            self._state = state

    def _matches(self, params: dict[str, str]) -> bool:
        with self._lock:
            expected = self._state
        got = params.get("state")
        return bool(expected and got) and secrets.compare_digest(got.encode(), expected.encode())

    def _deliver(self, params: dict[str, str]) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._replies.put(params)

    def feed(self, address_or_query: str) -> bool:
        """A pasted address (or bare query). False if it is not for this attempt."""
        text = (address_or_query or "").strip()
        if not text:
            return False
        parts = urlsplit(text)
        query = parts.query if (parts.scheme or parts.netloc or "?" in text) else text.lstrip("?")
        params = _query(query)
        if not self._matches(params):
            return False
        self._deliver(params)
        return True

    def wait(self, timeout: float) -> dict[str, str]:
        """The callback's query parameters. ``LoginFailed`` on timeout."""
        try:
            return self._replies.get(timeout=timeout)
        except queue.Empty:
            raise LoginFailed(
                f"Timed out after {timeout:g} s waiting for the browser sign-in. Try again."
            ) from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
