"""``rp login`` / ``logout`` / ``whoami`` and the stored-credential precedence.

The device flow is exercised against a stubbed server (an ``httpx`` mock
transport standing in for a management server), so this covers the CLI's side
of the protocol: start, poll until approved, store 0600, without a real
server.
"""

import json
import stat
from unittest.mock import MagicMock

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
        spy = MagicMock(return_value={"slug": "s", "name": "n", "level": "l", "indexed": False})
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
    """Enough of /api/manage/cli-auth to drive ``rp login``.

    ``approve_after`` is how many polls return pending before the key appears.
    """

    def __init__(self, approve_after: int = 2):
        self.approve_after = approve_after
        self.polls = 0
        self.started: list[dict] = []
        self.collected = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/manage/cli-auth" and request.method == "POST":
            self.started.append(json.loads(request.content))
            return httpx.Response(
                201,
                json={
                    "device_code": "rpd_secret",
                    "user_code": "ABCD-EFGH",
                    "verify_url": f"{SERVER}/api/manage/cli-auth/approve?code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 0,
                },
            )
        if path == "/api/manage/cli-auth/poll":
            assert json.loads(request.content) == {"device_code": "rpd_secret"}
            self.polls += 1
            if self.collected:
                return httpx.Response(404, json={"detail": "already collected"})
            if self.polls <= self.approve_after:
                return httpx.Response(200, json={"status": "pending", "interval": 0})
            self.collected = True
            return httpx.Response(
                200,
                json={
                    "status": "approved",
                    "token": "rpk_minted",
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


class TestLoginFlow:
    def test_polls_until_approved(self, fake, capsys):
        result = creds.login(SERVER, label="laptop", open_browser=False, sleep=lambda _s: None)
        assert result == creds.Login(
            url=SERVER, token="rpk_minted", orcid="0000-0002-1825-0097", name="Jane A. Doe"
        )
        assert fake.polls == 3
        assert fake.started == [{"label": "laptop"}]
        err = capsys.readouterr().err
        assert "cli-auth/approve?code=ABCD-EFGH" in err and "ABCD-EFGH" in err

    def test_opens_the_browser_when_asked(self, fake, monkeypatch):
        opened = []
        monkeypatch.setattr(creds.webbrowser, "open", lambda url: opened.append(url))
        creds.login(SERVER, open_browser=True, sleep=lambda _s: None)
        assert opened == [f"{SERVER}/api/manage/cli-auth/approve?code=ABCD-EFGH"]

    def test_expired_request_is_an_error(self, fake):
        fake.collected = True  # every poll now 404s
        with pytest.raises(creds.LoginError, match="expired or was already used"):
            creds.login(SERVER, open_browser=False, sleep=lambda _s: None)

    def test_server_without_the_flow(self, monkeypatch):
        def handle(_request):
            return httpx.Response(404)

        monkeypatch.setattr(
            httpx,
            "Client",
            lambda *a, **kw: _RealClient(base_url=SERVER, transport=httpx.MockTransport(handle)),
        )
        with pytest.raises(creds.LoginError, match="does not offer"):
            creds.login(SERVER, open_browser=False, sleep=lambda _s: None)

    def test_timeout(self, monkeypatch):
        server = FakeServer(approve_after=10**6)
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
