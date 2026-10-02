"""``rp login`` / ``logout`` / ``whoami`` and the stored-credential precedence.

The device flow (RFC 8628: ``/api/auth/device`` then ``/api/auth/token``) is
exercised against a stubbed server (an ``httpx`` mock transport), so this
covers the CLI's side of the protocol: start, poll through pending /
slow_down / 429 until approved or refused, store 0600, without a real server.
"""

import json
import stat
from unittest.mock import MagicMock
from urllib.parse import parse_qsl

import httpx
import pytest

from researcher_profiles.cli import main
from researcher_profiles.cli.auth import credentials as creds

SERVER = "https://people.example.org"
_RealClient = httpx.Client


@pytest.fixture
def stored(tmp_path):
    login = creds.Login(url=SERVER, token="rpk_stored", orcid="0000-0002-1825-0097", name="Jane")
    creds.save_login(login)
    return login


class TestStore:
    def test_save_is_owner_only_and_round_trips(self, stored):
        path = creds.credentials_path()
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text())["token"] == "rpk_stored"
        assert creds.load_login() == stored

    def test_missing_or_broken_file_is_none(self):
        assert creds.load_login() is None
        p = creds.credentials_path()
        p.parent.mkdir(parents=True)
        p.write_text("{not json")
        assert creds.load_login() is None
        p.write_text(json.dumps({"url": SERVER}))  # no token
        assert creds.load_login() is None

    def test_clear(self, stored):
        assert creds.clear_login() is True
        assert creds.load_login() is None
        assert creds.clear_login() is False


class TestPrecedence:
    def test_explicit_token_wins(self, stored, monkeypatch):
        monkeypatch.setenv(creds.TOKEN_ENV_VAR, "env-token")
        assert creds.resolve_token("flag-token", SERVER) == "flag-token"

    def test_env_beats_stored(self, stored, monkeypatch):
        monkeypatch.setenv(creds.TOKEN_ENV_VAR, "env-token")
        assert creds.resolve_token(None, SERVER) == "env-token"

    def test_stored_only_for_its_own_server(self, stored):
        assert creds.resolve_token(None, SERVER) == "rpk_stored"
        assert creds.resolve_token(None, SERVER + "/") == "rpk_stored"
        assert creds.resolve_token(None, SERVER + "/api/v1/profiles") == "rpk_stored"
        assert creds.resolve_token(None, None) == "rpk_stored"
        assert creds.resolve_token(None, "https://other.example.org") is None

    def test_url_defaults_to_the_login(self, stored):
        assert creds.resolve_url(None) == SERVER
        assert creds.resolve_url("https://x") == "https://x"
        creds.clear_login()
        assert creds.resolve_url(None) is None


class TestCliWiring:
    """The flags reach the library with the stored login filled in."""

    def _spy(self, monkeypatch, target):
        """A recorder returning a push-shaped result; other targets reset it."""
        from researcher_profiles.client import PushPlan, PushResult

        spy = MagicMock(
            return_value=PushResult(
                plan=PushPlan(slug="s", exists=False),
                summary={"slug": "s", "name": "n", "level": "l", "indexed": False},
            )
        )
        module, attr = target.rsplit(".", 1)
        import importlib

        monkeypatch.setattr(importlib.import_module(module), attr, spy)
        return spy

    def test_push_with_no_flags_uses_the_login(self, stored, monkeypatch, tmp_path):
        spy = self._spy(monkeypatch, "researcher_profiles.client.push_profile")
        assert main(["push", str(tmp_path)]) == 0
        assert spy.call_args.args[0] == SERVER
        assert spy.call_args.kwargs["token"] == "rpk_stored"

    def test_push_to_another_server_does_not_leak_the_key(self, stored, monkeypatch, tmp_path):
        spy = self._spy(monkeypatch, "researcher_profiles.client.push_profile")
        assert main(["push", str(tmp_path), "--url", "https://other.example.org"]) == 0
        assert spy.call_args.kwargs["token"] is None

    def test_push_with_nothing_is_a_usage_error(self, tmp_path, capsys):
        assert main(["push", str(tmp_path)]) == 2
        assert "rp login" in capsys.readouterr().err

    def test_install_and_listr_pick_up_the_login(self, stored, monkeypatch):
        inst = self._spy(monkeypatch, "researcher_profiles.client.install_profile")
        inst.return_value = {"slug": "a", "status": "present", "path": "/x"}
        assert main(["install", "a"]) == 0
        assert inst.call_args.kwargs["url"] == SERVER
        assert inst.call_args.kwargs["token"] == "rpk_stored"

        listr = MagicMock(return_value=[])
        monkeypatch.setattr("researcher_profiles.client.list_registry", listr)
        main(["listr"])
        assert listr.call_args.args[0] == SERVER
        assert listr.call_args.kwargs["token"] == "rpk_stored"

    def test_registry_env_var_keeps_precedence_for_the_url(self, stored, monkeypatch):
        monkeypatch.setenv("RESEARCHER_PROFILES_REGISTRY_URL", "https://other.example.org")
        listr = MagicMock(return_value=[])
        monkeypatch.setattr("researcher_profiles.client.list_registry", listr)
        main(["listr"])
        assert listr.call_args.args[0] == "https://other.example.org"
        assert listr.call_args.kwargs["token"] is None  # not that server's key

    def test_logout(self, stored, capsys):
        assert main(["logout"]) == 0
        assert creds.load_login() is None
        assert main(["logout"]) == 0
        assert "not logged in" in capsys.readouterr().out

    def test_whoami_without_login(self, capsys):
        assert main(["whoami"]) == 2
        assert "rp login" in capsys.readouterr().err


# The device flow against a stubbed server


class FakeServer:
    """Enough of the RFC 8628 endpoints (``/api/auth/device`` + ``/api/auth/token``)
    to drive ``rp login``.

    ``replies`` scripts the token endpoint: each poll pops the next entry, an
    OAuth ``error`` code (400), an int status (with an empty body), or
    ``"ok"`` (the approved 200). When it runs out, polls get ``"ok"``.
    """

    VERIFY = f"{SERVER}/device"

    def __init__(self, replies=("authorization_pending", "authorization_pending")):
        self.replies = list(replies)
        self.polls = 0
        self.started: list[dict] = []
        self.collected = False
        self.complete_uri = True
        self.interval: int | None = 0
        self.retry_after: str | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/device" and request.method == "POST":
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            self.started.append(dict(parse_qsl(request.content.decode())))
            body = {
                "device_code": "rpd_secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": self.VERIFY,
                "expires_in": 600,
            }
            if self.complete_uri:
                body["verification_uri_complete"] = f"{self.VERIFY}?user_code=ABCD-EFGH"
            if self.interval is not None:
                body["interval"] = self.interval
            return httpx.Response(200, json=body)
        if path == "/api/auth/token" and request.method == "POST":
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            assert dict(parse_qsl(request.content.decode())) == {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": "rpd_secret",
                "client_id": "rp",
            }
            self.polls += 1
            if self.collected:
                return httpx.Response(400, json={"error": "expired_token"})
            reply = self.replies.pop(0) if self.replies else "ok"
            if isinstance(reply, int):
                headers = {"Retry-After": self.retry_after} if self.retry_after else {}
                return httpx.Response(reply, headers=headers)
            if reply != "ok":
                return httpx.Response(400, json={"error": reply})
            self.collected = True
            return httpx.Response(
                200,
                json={
                    "access_token": "rpk_minted",
                    "token_type": "Bearer",
                    "scope": "push_own",
                    "orcid": "0000-0002-1825-0097",
                    "name": "Jane A. Doe",
                    "url": SERVER,
                },
            )
        if path == "/api/manage/whoami":
            if request.headers.get("authorization") != "Bearer rpk_minted":
                return httpx.Response(401, json={"detail": "bad"})
            return httpx.Response(
                200,
                json={
                    "consumer": "cli_abc",
                    "scopes": ["push_own"],
                    "owner": {"orcid": "0000-0002-1825-0097", "name": "Jane A. Doe"},
                    "profiles": [
                        {"slug": "jane-doe", "rid": "0000-0002-1825-0097", "role": "owner"}
                    ],
                },
            )
        return httpx.Response(404, json={"detail": "no such route"})

    def client(self) -> httpx.Client:
        return _RealClient(base_url=SERVER, transport=httpx.MockTransport(self.handle))


@pytest.fixture
def fake(monkeypatch):
    """A FakeServer wired in as the httpx client every credential call builds."""
    server = FakeServer()
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: server.client())
    return server


def _login(**kw):
    """Run the flow with no browser, recording every sleep."""
    slept: list[float] = []
    kw.setdefault("open_browser", False)
    result = creds.login(SERVER, sleep=slept.append, **kw)
    return result, slept


class TestLoginFlow:
    def test_polls_until_approved(self, fake, capsys):
        result, slept = _login(label="laptop")
        assert result == creds.Login(
            url=SERVER, token="rpk_minted", orcid="0000-0002-1825-0097", name="Jane A. Doe"
        )
        assert fake.polls == 3
        assert fake.started == [{"client_id": "rp", "scope": "push_own", "label": "laptop"}]
        assert slept == [0, 0, 0]
        err = capsys.readouterr().err
        assert f"{FakeServer.VERIFY}?user_code=ABCD-EFGH" in err and "Code: ABCD-EFGH" in err

    def test_interval_defaults_to_five_seconds(self, fake):
        fake.interval = None
        _, slept = _login()
        assert slept == [5, 5, 5]

    def test_slow_down_adds_five_seconds_for_good(self, fake):
        fake.replies = ["authorization_pending", "slow_down", "authorization_pending", "slow_down"]
        _, slept = _login()
        assert slept == [0, 0, 5, 5, 10]

    def test_429_is_slow_down(self, fake):
        fake.replies = [429, "authorization_pending"]
        _, slept = _login()
        assert slept == [0, 5, 5]

    def test_429_honors_a_longer_retry_after(self, fake):
        fake.replies = [429]
        fake.retry_after = "30"
        _, slept = _login()
        assert slept == [0, 30]

    def test_opens_the_complete_uri_when_asked(self, fake, monkeypatch):
        opened = []
        monkeypatch.setattr(creds.webbrowser, "open", lambda url: opened.append(url))
        _login(open_browser=True)
        assert opened == [f"{FakeServer.VERIFY}?user_code=ABCD-EFGH"]

    def test_falls_back_to_the_plain_uri(self, fake, monkeypatch, capsys):
        fake.complete_uri = False
        opened = []
        monkeypatch.setattr(creds.webbrowser, "open", lambda url: opened.append(url))
        _login(open_browser=True)
        assert opened == [FakeServer.VERIFY]
        assert "Code: ABCD-EFGH" in capsys.readouterr().err

    def test_denied(self, fake):
        fake.replies = ["authorization_pending", "access_denied"]
        with pytest.raises(creds.LoginError, match="refused"):
            _login()
        assert fake.polls == 2

    @pytest.mark.parametrize("error", ["expired_token", "invalid_grant"])
    def test_expired(self, fake, error):
        fake.replies = [error]
        with pytest.raises(creds.LoginError, match="expired or was already used"):
            _login()
        assert fake.polls == 1

    @pytest.mark.parametrize("reply", ["unsupported_grant_type", "something_new", 500, 404])
    def test_anything_else_is_fatal(self, fake, reply):
        fake.replies = [reply]
        with pytest.raises(creds.LoginError, match="login failed"):
            _login()
        assert fake.polls == 1

    def test_busy_start_is_retried_after_retry_after(self, fake, monkeypatch):
        real = fake.handle
        busy = [httpx.Response(503, headers={"Retry-After": "7"}), httpx.Response(429)]

        def handle(request):
            if request.url.path == "/api/auth/device" and busy:
                return busy.pop(0)
            return real(request)

        fake.handle = handle
        result, slept = _login()
        assert result.token == "rpk_minted"
        assert slept[:2] == [7, 5]  # Retry-After, then the default when absent
        assert len(fake.started) == 1

    def test_start_gives_up_when_always_busy(self, monkeypatch):
        calls = []

        def handle(request):
            calls.append(request.url.path)
            return httpx.Response(503)

        monkeypatch.setattr(
            httpx,
            "Client",
            lambda *a, **kw: _RealClient(base_url=SERVER, transport=httpx.MockTransport(handle)),
        )
        with pytest.raises(creds.LoginError, match="refused \\(503"):
            _login()
        assert len(calls) == creds.START_ATTEMPTS

    def test_server_without_the_flow(self, monkeypatch):
        calls = []

        def handle(request):
            calls.append(request.url.path)
            return httpx.Response(404)

        monkeypatch.setattr(
            httpx,
            "Client",
            lambda *a, **kw: _RealClient(base_url=SERVER, transport=httpx.MockTransport(handle)),
        )
        with pytest.raises(creds.LoginError, match="does not offer command-line login"):
            _login()
        assert calls == ["/api/auth/device"]  # no retry, no other path

    def test_timeout(self, monkeypatch):
        server = FakeServer(replies=["authorization_pending"] * 10**3)
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: server.client())
        clock = iter(range(0, 10**6))
        monkeypatch.setattr(creds.time, "monotonic", lambda: next(clock))
        with pytest.raises(creds.LoginError, match="timed out"):
            creds.login(SERVER, open_browser=False, timeout=5, sleep=lambda _s: None)

    def test_rp_login_stores_and_whoami_reads(self, fake, monkeypatch, capsys):
        monkeypatch.setattr(creds.time, "sleep", lambda _s: None)
        assert main(["login", SERVER, "--no-browser"]) == 0
        stored = creds.load_login()
        assert stored is not None and stored.token == "rpk_minted"
        assert stat.S_IMODE(creds.credentials_path().stat().st_mode) == 0o600
        assert "logged in to" in capsys.readouterr().err

        assert main(["whoami"]) == 0
        out = capsys.readouterr().out
        assert "Jane A. Doe" in out and "jane-doe" in out

        assert main(["whoami", "--json"]) == 0
        rec = json.loads(capsys.readouterr().out)
        assert rec["url"] == SERVER and rec["scopes"] == ["push_own"]

        # A later `rp login` with no server argument re-logs into the same server.
        fake.collected = False
        fake.polls = 0
        assert main(["login", "--no-browser"]) == 0
        assert fake.started[-1]["label"]  # the hostname, by default

    def test_rp_login_reports_failure(self, fake, monkeypatch, capsys):
        fake.collected = True
        monkeypatch.setattr(creds.time, "sleep", lambda _s: None)
        assert main(["login", SERVER, "--no-browser"]) == 1
        assert "login failed" in capsys.readouterr().err
        assert creds.load_login() is None
