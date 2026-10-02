"""OpenAlexClient: the key travels only in the Authorization header.

Every request goes through ``httpx.MockTransport``; no network.
"""

import logging

import httpx
import pytest

from researcher_profiles.openalex_client import (
    OpenAlexAuthError,
    OpenAlexBudgetError,
    OpenAlexClient,
    OpenAlexHTTPError,
)

KEY = "sk-test-0123456789abcdef"


def _client(handler, key=KEY):
    return OpenAlexClient(key, transport=httpx.MockTransport(handler))


def _ok(body=None, headers=None):
    def handler(request):
        handler.requests.append(request)
        return httpx.Response(200, json=body or {"results": []}, headers=headers or {})

    handler.requests = []
    return handler


def test_key_goes_in_the_header_and_never_the_url():
    handler = _ok()
    client = _client(handler)
    client.get("/works", {"filter": "title.search:chromatin", "per-page": 1})
    req = handler.requests[0]
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in str(req.url)
    assert "api_key" not in str(req.url)
    assert "mailto" not in str(req.url)


def test_no_key_sends_no_authorization_header():
    handler = _ok()
    client = _client(handler, key="  ")
    client.get("/works")
    assert "authorization" not in handler.requests[0].headers
    assert not client.has_key


@pytest.mark.parametrize("name", ["api_key", "API-KEY", "mailto"])
def test_credentials_in_params_are_refused(name):
    client = _client(_ok())
    with pytest.raises(ValueError, match="header"):
        client.get("/works", {name: "x"})


def test_repr_hides_the_key():
    client = _client(_ok())
    assert KEY not in repr(client)
    assert KEY not in str(client)
    assert repr(client) == "OpenAlexClient(has_key=True)"


def _status(code, headers=None):
    return lambda request: httpx.Response(code, json={"error": "x"}, headers=headers or {})


def test_429_is_a_budget_error():
    client = _client(_status(429, {"retry-after": "120"}))
    with pytest.raises(OpenAlexBudgetError) as ei:
        client.get("/works", {"filter": "title.search:secret terms"})
    assert ei.value.retry_after == 120
    assert KEY not in str(ei.value)
    assert "secret terms" not in str(ei.value)


def test_401_is_an_auth_error():
    client = _client(_status(401))
    with pytest.raises(OpenAlexAuthError) as ei:
        client.get("/works")
    assert KEY not in str(ei.value)


def test_other_errors_carry_status_and_path_only():
    client = _client(_status(500))
    with pytest.raises(OpenAlexHTTPError) as ei:
        client.get("/works", {"filter": "title.search:secret terms"})
    assert str(ei.value) == "OpenAlex /works: HTTP 500"
    assert ei.value.__cause__ is None


def test_transport_errors_drop_the_chain():
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    client = _client(boom)
    with pytest.raises(OpenAlexHTTPError) as ei:
        client.get("/works", {"filter": "title.search:secret terms"})
    assert "secret terms" not in str(ei.value)
    assert ei.value.__cause__ is None
    assert ei.value.__suppress_context__


def test_redirects_are_not_followed():
    handler_calls = []

    def handler(request):
        handler_calls.append(request)
        return httpx.Response(302, headers={"location": "https://elsewhere.example/x"})

    client = _client(handler)
    with pytest.raises(OpenAlexHTTPError, match="HTTP 302"):
        client.get("/works")
    assert len(handler_calls) == 1


def test_cost_and_remaining_budget_are_tracked():
    handler = _ok(
        {"results": [], "meta": {"cost_usd": 0.001}},
        {"x-ratelimit-remaining-usd": "0.75"},
    )
    client = _client(handler)
    client.get("/works")
    client.get("/works")
    assert client.cost_usd == pytest.approx(0.002)
    assert client.remaining_usd == pytest.approx(0.75)


def test_metered_view_counts_its_own_cost_and_feeds_the_parent():
    handler = _ok(
        {"results": [], "meta": {"cost_usd": 0.001}},
        {"x-ratelimit-remaining-usd": "0.5"},
    )
    client = _client(handler)
    client.get("/works")
    view = client.metered()
    view.get("/works")
    assert view.cost_usd == pytest.approx(0.001)
    assert client.cost_usd == pytest.approx(0.002)
    assert client.remaining_usd == pytest.approx(0.5)
    assert view.has_key
    assert handler.requests[-1].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in repr(view)


def test_rate_limit_reads_remaining_from_the_body():
    handler = _ok({"api_key": "…cdef", "rate_limit": {"daily_remaining_usd": 0.42}})
    client = _client(handler)
    client.rate_limit()
    assert handler.requests[0].url.path == "/rate-limit"
    assert client.remaining_usd == pytest.approx(0.42)


def test_httpx_request_lines_are_not_logged(caplog):
    client = _client(_ok())
    with caplog.at_level(logging.INFO):
        client.get("/works", {"filter": "title.search:x"})
    assert not [r for r in caplog.records if r.name.startswith("httpx")]
