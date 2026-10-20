"""``rp login`` / ``logout`` / ``whoami`` and the stored-credential precedence.

``rp login`` signs in as the registered OAuth client ``rp-cli``: by default the
loopback flow with PKCE (RFC 8252), else the device flow (RFC 8628), both at
the endpoints the server's RFC 8414 metadata names. Both are exercised against
a stubbed server (an ``httpx`` mock transport) and, for the loopback flow, a
stand-in browser that follows the redirect to the real local listener. The
stored login renews itself: refresh when expired or on a 401, rotated tokens
saved atomically. ``rp logout`` revokes it (RFC 7009). The file also pins the
agent ``ManagementClient`` wire contract from ``cli/auth/agent.py``.
"""

import base64
import hashlib
import json
import os
import stat
import threading
import time
from unittest.mock import MagicMock
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import pytest

from researcher_profiles.cli import main
from researcher_profiles.cli.auth import credentials as creds
from researcher_profiles.cli.auth.agent import (
    AgentAPIError,
    Credential,
    CredentialError,
    InsufficientAccessError,
    ManagementClient,
    resolve_credential,
)

SERVER = "https://people.example.org"
OTHER_SERVER = "https://other.example.org"
SLUG = "jane-doe"
_RealClient = httpx.Client


@pytest.fixture
def stored(tmp_path):
    """A login from an older rp: a non-expiring rpk_ key, no refresh token."""
    login = creds.Login(url=SERVER, access_token="rpk_stored")
    creds.save_login(login)
    return login


@pytest.fixture(autouse=True)
def _desktop(monkeypatch):
    """A desktop session: a display, no SSH. Tests that need otherwise say so."""
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)


class TestConfigDir:
    @pytest.fixture
    def agent_env(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("RESEARCHER_PROFILES_AGENT_KEY", raising=False)
        monkeypatch.delenv("RESEARCHER_PROFILES_API_URL", raising=False)
        monkeypatch.delenv("RESEARCHER_PROFILES_AUTH_HOST", raising=False)

    def _write_toml(self, mode):
        d = creds.config_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / "credentials.toml"
        f.write_text('default = "h"\n[hosts.h]\nkey = "rpa_x"\nurl = "https://x"\n')
        f.chmod(mode)
        return f

    def test_xdg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert creds.config_dir() == tmp_path / "researcher-profiles"

    def test_home_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert creds.config_dir() == tmp_path / ".config" / "researcher-profiles"

    def test_both_files_share_folder(self, agent_env):
        assert creds.credentials_path().parent == creds.config_dir()
        with pytest.raises(CredentialError) as exc:
            resolve_credential()
        assert str(creds.config_dir() / "credentials.toml") in str(exc.value)

    def test_agent_toml_resolves(self, agent_env):
        f = self._write_toml(0o600)
        cred = resolve_credential()
        assert cred.key == "rpa_x"
        assert str(f) in cred.source

    def test_agent_toml_bad_permissions(self, agent_env):
        self._write_toml(0o644)
        with pytest.raises(CredentialError, match="chmod 600"):
            resolve_credential()


OAUTH_LOGIN = creds.Login(
    url=SERVER,
    access_token="rpa_live",
    refresh_token="rpr_live",
    expires_at=4_000_000_000,
    issuer=SERVER,
)


class TestStore:
    def test_save_is_owner_only_atomic_and_round_trips(self):
        path = creds.save_login(OAUTH_LOGIN)
        assert path == creds.credentials_path()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text()) == {
            "url": SERVER,
            "access_token": "rpa_live",
            "refresh_token": "rpr_live",
            "expires_at": 4_000_000_000,
            "issuer": SERVER,
        }
        assert creds.load_login() == OAUTH_LOGIN
        # No temporary file is left behind.
        assert sorted(p.name for p in path.parent.iterdir()) == ["credentials.json"]

    def test_failed_save_keeps_the_old_file(self, monkeypatch):
        creds.save_login(OAUTH_LOGIN)

        def boom(*_a):
            raise OSError("disk full")

        monkeypatch.setattr(creds.os, "replace", boom)
        with pytest.raises(OSError, match="disk full"):
            creds.save_login(creds.Login(url=SERVER, access_token="rpa_new"))
        assert creds.load_login() == OAUTH_LOGIN
        assert sorted(p.name for p in creds.config_dir().iterdir()) == ["credentials.json"]

    def test_an_older_rp_file_still_loads(self):
        p = creds.credentials_path()
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"url": SERVER, "token": "rpk_old", "orcid": "x", "name": "J"}))
        login = creds.load_login()
        assert login == creds.Login(url=SERVER, access_token="rpk_old")
        assert login.token == "rpk_old"
        assert not login.refreshable and not login.expired()

    def test_missing_or_broken_file_is_none(self):
        assert creds.load_login() is None
        p = creds.credentials_path()
        p.parent.mkdir(parents=True)
        p.write_text("{not json")
        assert creds.load_login() is None
        p.write_text(json.dumps({"url": SERVER}))  # no token
        assert creds.load_login() is None
        p.write_text(json.dumps(["not", "an", "object"]))
        assert creds.load_login() is None

    def test_clear(self, stored):
        assert creds.clear_login() is True
        assert creds.load_login() is None
        assert creds.clear_login() is False

    def test_expiry(self):
        login = creds.Login(url=SERVER, access_token="a", refresh_token="r", expires_at=1000)
        assert login.expired(now=999)  # inside the 60 s margin
        assert login.expired(now=1000)
        assert not login.expired(now=900)


class TestPrecedence:
    @pytest.mark.parametrize(
        ("env", "flag", "url", "expected"),
        [
            ("env-token", "flag-token", SERVER, "flag-token"),
            ("env-token", None, SERVER, "env-token"),
            (None, None, SERVER, "rpk_stored"),
            (None, None, SERVER + "/", "rpk_stored"),
            (None, None, SERVER + "/api/v1/profiles", "rpk_stored"),
            (None, None, None, "rpk_stored"),
            (None, None, OTHER_SERVER, None),  # a stored key never goes to another server
        ],
        ids=[
            "flag-beats-env",
            "env-beats-stored",
            "stored-exact",
            "stored-trailing-slash",
            "stored-api-path",
            "stored-no-url",
            "other-server-gets-nothing",
        ],
    )
    def test_resolve_token(self, stored, monkeypatch, env, flag, url, expected):
        if env:
            monkeypatch.setenv(creds.TOKEN_ENV_VAR, env)
        assert creds.resolve_token(flag, url) == expected

    def test_url_defaults_to_the_login(self, stored):
        assert creds.resolve_url(None) == SERVER
        assert creds.resolve_url("https://x") == "https://x"
        creds.clear_login()
        assert creds.resolve_url(None) is None


def _patch(monkeypatch, dotted, returns):
    spy = MagicMock(return_value=returns)
    monkeypatch.setattr(dotted, spy)
    return spy


def _called_url(spy):
    return spy.call_args.kwargs.get("url") or spy.call_args.args[0]


def _push_result():
    from researcher_profiles.client import PushPlan, PushResult

    return PushResult(
        plan=PushPlan(slug="s", exists=False),
        summary={"slug": "s", "name": "n", "level": "l", "indexed": False},
    )


class TestCliWiring:
    """The flags reach the library with the stored login filled in."""

    @pytest.mark.parametrize(
        ("argv", "target", "env", "url", "token", "exit_code"),
        [
            (["push", "{tmp}"], "push_profile", None, SERVER, "rpk_stored", 0),
            (["push", "{tmp}", "--url", OTHER_SERVER], "push_profile", None, OTHER_SERVER, None, 0),
            (["install", "a"], "install_profile", None, SERVER, "rpk_stored", 0),
            # an empty listing is a usage error (exit 2)
            (["listr"], "list_registry", None, SERVER, "rpk_stored", 2),
            # the env var keeps precedence for the URL, and the key stays home
            (["listr"], "list_registry", OTHER_SERVER, OTHER_SERVER, None, 2),
        ],
        ids=["push-default", "push-other-server", "install", "listr", "listr-registry-env"],
    )
    def test_cli_passes_the_login(
        self, stored, monkeypatch, tmp_path, argv, target, env, url, token, exit_code
    ):
        returns = {
            "push_profile": _push_result(),
            "install_profile": {"slug": "a", "status": "present", "path": "/x"},
            "list_registry": [],
        }[target]
        if env:
            monkeypatch.setenv("RESEARCHER_PROFILES_REGISTRY_URL", env)
        spy = _patch(monkeypatch, f"researcher_profiles.client.{target}", returns)
        argv = [str(tmp_path) if a == "{tmp}" else a for a in argv]
        assert main(argv) == exit_code
        assert _called_url(spy) == url
        assert spy.call_args.kwargs["token"] == token

    @pytest.mark.parametrize("argv", [["push", "{tmp}"], ["whoami"]])
    def test_needs_a_login(self, tmp_path, capsys, argv):
        assert main([str(tmp_path) if a == "{tmp}" else a for a in argv]) == 2
        assert "rp login" in capsys.readouterr().err

    def test_logout_of_an_older_login_only_forgets_it(self, stored, fake, capsys):
        assert main(["logout"]) == 0
        assert creds.load_login() is None
        assert fake.revoked == []
        captured = capsys.readouterr()
        assert "Privacy page" in captured.err
        assert main(["logout"]) == 0
        assert "not logged in" in capsys.readouterr().out

    def test_logout_revokes_at_the_server(self, fake, capsys):
        creds.save_login(OAUTH_LOGIN)
        assert main(["logout"]) == 0
        assert fake.revoked == [
            {"token": "rpr_live", "token_type_hint": "refresh_token", "client_id": "rp-cli"}
        ]
        assert creds.load_login() is None

    def test_logout_without_a_revocation_endpoint_still_forgets(self, fake, capsys):
        del fake.metadata["revocation_endpoint"]
        creds.save_login(OAUTH_LOGIN)
        assert main(["logout"]) == 0
        assert creds.load_login() is None
        assert "did not confirm" in capsys.readouterr().err


def _challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


class FakeServer:
    """RFC 8414 metadata plus the endpoints it names, enough to drive ``rp login``.

    The device flow: ``/oauth/device_authorization`` and the device grant at
    ``/oauth/token``. ``replies`` scripts the device polls: each pops the next
    entry, an OAuth ``error`` code (400), an int status (with an empty body),
    or ``"ok"`` (the approved 200). When it runs out, polls get ``"ok"``.

    The loopback flow: :meth:`browser` stands in for the system browser; the
    code it returns is traded at ``/oauth/token`` with its PKCE verifier.

    Refresh: each ``refresh_token`` grant rotates ``rpr_<n>`` and ``rpa_<n>``.
    ``whoami`` accepts only the newest access token.
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
        self.generation = 0
        self.live_refresh = "rpr_live"
        self.live_access = "rpa_live"
        self.refreshes: list[dict] = []
        self.revoked: list[dict] = []
        self.authorize: dict | None = None
        self.codes: dict[str, dict] = {}
        self.browser_answer: dict | None = None
        self.metadata: dict = {
            "issuer": SERVER,
            "authorization_endpoint": f"{SERVER}/oauth/authorize",
            "device_authorization_endpoint": f"{SERVER}/oauth/device_authorization",
            "token_endpoint": f"{SERVER}/oauth/token",
            "revocation_endpoint": f"{SERVER}/oauth/revoke",
        }

    def _mint(self) -> httpx.Response:
        self.generation += 1
        self.live_access = f"rpa_{self.generation}"
        self.live_refresh = f"rpr_{self.generation}"
        return httpx.Response(
            200,
            json={
                "access_token": self.live_access,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": self.live_refresh,
                "scope": "profiles:read profiles:push",
            },
        )

    def browser(self, url: str) -> bool:
        """Open ``url`` the way a browser would after the person clicks Allow."""
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{SERVER}/oauth/authorize"
        q = dict(parse_qsl(parts.query))
        self.authorize = q
        answer = self.browser_answer
        if answer is None:
            code = f"rpc_{len(self.codes)}"
            self.codes[code] = q
            answer = {"code": code, "state": q["state"], "iss": SERVER}
        answer = {k: (q["state"] if v == "{state}" else v) for k, v in answer.items()}
        target = q["redirect_uri"] + "?" + urlencode(answer)
        threading.Thread(target=lambda: _RealClient().get(target), daemon=True).start()
        return True

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        form = dict(parse_qsl(request.content.decode())) if request.method == "POST" else {}
        if path == "/.well-known/oauth-authorization-server" and request.method == "GET":
            return httpx.Response(200, json=self.metadata)
        if path == "/oauth/device_authorization" and request.method == "POST":
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            self.started.append(form)
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
        if path == "/oauth/token" and request.method == "POST":
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            assert form["client_id"] == "rp-cli"
            if form["grant_type"] == "refresh_token":
                self.refreshes.append(form)
                if form["refresh_token"] != self.live_refresh:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return self._mint()
            if form["grant_type"] == "authorization_code":
                q = self.codes.pop(form["code"], None)
                if (
                    q is None
                    or form["redirect_uri"] != q["redirect_uri"]
                    or _challenge(form["code_verifier"]) != q["code_challenge"]
                ):
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return self._mint()
            assert form == {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": "rpd_secret",
                "client_id": "rp-cli",
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
            return self._mint()
        if path == "/oauth/revoke" and request.method == "POST":
            self.revoked.append(form)
            return httpx.Response(200)
        if path == "/api/v1/manage/whoami":
            if request.headers.get("authorization") != f"Bearer {self.live_access}":
                return httpx.Response(401, json={"detail": "bad"})
            return httpx.Response(
                200,
                json={
                    "consumer": "agent_abc",
                    "scopes": ["push_own", "read"],
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
    """Run the device flow with no browser, recording every sleep."""
    slept: list[float] = []
    kw.setdefault("open_browser", False)
    kw.setdefault("device", True)
    result = creds.login(SERVER, sleep=slept.append, **kw)
    return result, slept


class TestDeviceFallbackSelection:
    @pytest.mark.parametrize(
        ("env", "platform", "device"),
        [
            ({"DISPLAY": ":0"}, "linux", False),
            ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", False),
            ({}, "linux", True),  # no display
            ({"DISPLAY": ":0", "SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}, "linux", True),
            ({"SSH_TTY": "/dev/pts/1"}, "darwin", True),
            ({}, "darwin", False),
            ({}, "win32", False),
        ],
        ids=["x11", "wayland", "linux-headless", "ssh", "ssh-mac", "mac", "windows"],
    )
    def test_prefer_device_flow(self, env, platform, device):
        assert creds.prefer_device_flow(env, platform) is device

    def test_ssh_picks_the_device_flow(self, fake, monkeypatch):
        monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
        opened = []
        result = creds.login(
            SERVER, sleep=lambda _s: None, opener=lambda u: opened.append(u) or True
        )
        assert result.access_token == "rpa_1"
        assert fake.started and fake.authorize is None
        assert opened == []  # nothing to open on the far side of SSH

    def test_no_browser_falls_back_to_the_device_flow(self, fake, capsys):
        result = creds.login(SERVER, sleep=lambda _s: None, opener=lambda _u: False)
        assert result.access_token == "rpa_1"
        assert fake.started
        assert "using a login code" in capsys.readouterr().err

    def test_no_authorization_endpoint_uses_the_device_flow(self, fake):
        del fake.metadata["authorization_endpoint"]
        result = creds.login(SERVER, sleep=lambda _s: None, opener=lambda _u: True)
        assert result.access_token == "rpa_1" and fake.started


class TestLoopbackFlow:
    def test_round_trip(self, fake, capsys):
        result = creds.login(SERVER, label="laptop", opener=fake.browser, timeout=10)
        assert result.url == SERVER and result.issuer == SERVER
        assert result.access_token == "rpa_1" and result.refresh_token == "rpr_1"
        assert abs(result.expires_at - (time.time() + 3600)) < 5
        q = fake.authorize
        assert q["client_id"] == "rp-cli"
        assert q["response_type"] == "code"
        assert q["scope"] == "profiles:push"
        assert q["code_challenge_method"] == "S256"
        assert len(q["code_challenge"]) == 43
        assert q["label"] == "laptop"
        redirect = urlsplit(q["redirect_uri"])
        assert redirect.scheme == "http" and redirect.hostname == "127.0.0.1"
        assert redirect.port and redirect.path == "/callback"
        assert fake.started == []  # no device flow
        assert "Opened your browser" in capsys.readouterr().err

    def test_each_login_uses_a_fresh_port_state_and_verifier(self, fake):
        creds.login(SERVER, opener=fake.browser, timeout=10)
        first = fake.authorize
        creds.login(SERVER, opener=fake.browser, timeout=10)
        second = fake.authorize
        for k in ("state", "code_challenge"):
            assert first[k] != second[k]

    @pytest.mark.parametrize(
        ("answer", "match"),
        [
            ({"error": "access_denied", "state": "{state}"}, "refused in the browser"),
            ({"code": "rpc_x", "state": "wrong"}, "different login attempt"),
            ({"code": "rpc_x", "state": "{state}", "iss": OTHER_SERVER}, "came from"),
            ({"error": "invalid_scope", "state": "{state}"}, "invalid_scope"),
            ({"code": "rpc_unknown", "state": "{state}"}, "invalid_grant"),
        ],
        ids=["denied", "state-mismatch", "iss-mismatch", "oauth-error", "bad-code"],
    )
    def test_refusals(self, fake, answer, match):
        fake.browser_answer = answer
        with pytest.raises(creds.LoginError, match=match):
            creds.login(SERVER, opener=fake.browser, timeout=10)

    def test_timeout(self, fake):
        with pytest.raises(creds.LoginError, match="timed out waiting for the browser"):
            creds.login(SERVER, opener=lambda _u: True, timeout=0.3)

    def test_rp_login_stores_the_tokens_and_greets_by_name(self, fake, monkeypatch, capsys):
        monkeypatch.setattr(creds.webbrowser, "open", fake.browser)
        assert main(["login", SERVER, "--label", "laptop"]) == 0
        stored = creds.load_login()
        assert stored.access_token == "rpa_1" and stored.refresh_token == "rpr_1"
        assert stored.issuer == SERVER and stored.expires_at
        assert stat.S_IMODE(creds.credentials_path().stat().st_mode) == 0o600
        assert "as Jane A. Doe" in capsys.readouterr().err
        assert main(["login", SERVER, "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"url": SERVER, "orcid": "0000-0002-1825-0097", "name": "Jane A. Doe"}


class TestRefresh:
    def _expired(self):
        login = creds.Login(
            url=SERVER,
            access_token="rpa_live",
            refresh_token="rpr_live",
            expires_at=int(time.time()) - 10,
            issuer=SERVER,
        )
        creds.save_login(login)
        return login

    def test_expired_token_is_refreshed_before_use(self, fake):
        self._expired()
        assert creds.resolve_token(None, SERVER) == "rpa_1"
        stored = creds.load_login()
        assert stored.access_token == "rpa_1" and stored.refresh_token == "rpr_1"
        assert stored.expires_at > time.time()
        assert fake.refreshes == [
            {"grant_type": "refresh_token", "refresh_token": "rpr_live", "client_id": "rp-cli"}
        ]
        # Fresh now: no second refresh.
        assert creds.resolve_token(None, SERVER) == "rpa_1"
        assert len(fake.refreshes) == 1

    def test_refresh_keeps_the_old_refresh_token_when_none_comes_back(self, fake):
        self._expired()
        real = fake._mint

        def mint():
            r = real()
            body = r.json()
            del body["refresh_token"]
            return httpx.Response(200, json=body)

        fake._mint = mint
        assert creds.resolve_token(None, SERVER) == "rpa_1"
        assert creds.load_login().refresh_token == "rpr_live"

    def test_a_refused_refresh_leaves_the_old_token(self, fake, capsys):
        self._expired()
        fake.live_refresh = "rpr_someone_else"
        assert creds.resolve_token(None, SERVER) == "rpa_live"
        assert "run rp login again" in capsys.readouterr().err
        assert creds.load_login().refresh_token == "rpr_live"

    def test_older_logins_are_never_refreshed(self, stored, fake):
        assert creds.resolve_token(None, SERVER) == "rpk_stored"
        assert creds.refresh_after_401("rpk_stored") is None
        assert fake.refreshes == []

    def test_whoami_refreshes_on_401_and_retries(self, fake, capsys):
        # Not expired by the clock, but the server says otherwise.
        creds.save_login(OAUTH_LOGIN)
        fake.live_access = "rpa_server_moved_on"
        assert main(["whoami"]) == 0
        assert "Jane A. Doe" in capsys.readouterr().out
        assert len(fake.refreshes) == 1
        assert creds.load_login().access_token == "rpa_1"

    def test_a_given_token_is_never_refreshed(self, fake, capsys):
        creds.save_login(OAUTH_LOGIN)
        fake.live_access = "rpa_server_moved_on"
        assert main(["whoami", "--token", "rpa_mine"]) == 1
        assert fake.refreshes == []

    def test_push_refreshes_on_401_and_retries(self, fake, monkeypatch, tmp_path):
        creds.save_login(OAUTH_LOGIN)
        spy = MagicMock(side_effect=[PermissionError("401"), _push_result()])
        monkeypatch.setattr("researcher_profiles.client.push_profile", spy)
        assert main(["push", str(tmp_path)]) == 0
        assert [c.kwargs["token"] for c in spy.call_args_list] == ["rpa_live", "rpa_1"]

    def test_another_rp_already_refreshed(self, fake):
        creds.save_login(OAUTH_LOGIN)
        newer = creds.Login(
            url=SERVER, access_token="rpa_newer", refresh_token="rpr_newer", issuer=SERVER
        )
        creds.save_login(newer)
        # The 401 was for the token we held before the other process saved.
        assert creds.refresh_after_401("rpa_live") == "rpa_newer"
        assert fake.refreshes == []

    def test_refresh_holds_a_lock(self, fake, monkeypatch):
        self._expired()
        seen = []
        real = creds._locked

        def spy(path):
            seen.append(path)
            return real(path)

        monkeypatch.setattr(creds, "_locked", spy)
        creds.resolve_token(None, SERVER)
        assert seen == [creds.credentials_path()]
        assert (creds.config_dir() / "credentials.json.lock").exists()
        assert os.stat(creds.config_dir() / "credentials.json.lock").st_mode & 0o077 == 0


class TestDeviceFlow:
    def test_polls_until_approved(self, fake, capsys):
        result, slept = _login(label="laptop")
        assert result.access_token == "rpa_1" and result.refresh_token == "rpr_1"
        assert result.issuer == SERVER
        assert fake.polls == 3
        assert fake.started == [
            {"client_id": "rp-cli", "scope": "profiles:push", "label": "laptop"}
        ]
        assert slept == [0, 0, 0]
        err = capsys.readouterr().err
        assert f"{FakeServer.VERIFY}?user_code=ABCD-EFGH" in err and "Code: ABCD-EFGH" in err

    @pytest.mark.parametrize(
        ("interval", "replies", "retry_after", "expected"),
        [
            (None, None, None, [5, 5, 5]),
            (
                0,
                ["authorization_pending", "slow_down", "authorization_pending", "slow_down"],
                None,
                [0, 0, 5, 5, 10],
            ),
            (0, [429, "authorization_pending"], None, [0, 5, 5]),
            (0, [429], "30", [0, 30]),
        ],
        ids=[
            "interval-default-5s",
            "slow-down-adds-5s-for-good",
            "429-is-slow-down",
            "429-honors-retry-after",
        ],
    )
    def test_sleep_schedule(self, fake, interval, replies, retry_after, expected):
        fake.interval = interval
        if replies:
            fake.replies = replies
        fake.retry_after = retry_after
        _, slept = _login()
        assert slept == expected

    @pytest.mark.parametrize(
        ("complete_uri", "opened"),
        [(True, [f"{FakeServer.VERIFY}?user_code=ABCD-EFGH"]), (False, [FakeServer.VERIFY])],
    )
    def test_open_browser(self, fake, monkeypatch, capsys, complete_uri, opened):
        # A desktop whose server offers only the device flow: open the link.
        del fake.metadata["authorization_endpoint"]
        fake.complete_uri = complete_uri
        got = []
        monkeypatch.setattr(creds.webbrowser, "open", got.append)
        _login(open_browser=True, device=None)
        assert got == opened
        assert "Code: ABCD-EFGH" in capsys.readouterr().err

    def test_explicit_device_opens_nothing(self, fake, monkeypatch):
        got = []
        monkeypatch.setattr(creds.webbrowser, "open", got.append)
        _login(open_browser=True, device=True)
        assert got == []

    @pytest.mark.parametrize(
        ("replies", "match", "polls"),
        [
            (["authorization_pending", "access_denied"], "refused", 2),
            (["expired_token"], "expired or was already used", 1),
            (["invalid_grant"], "expired or was already used", 1),
            (["unsupported_grant_type"], "login failed", 1),
            (["something_new"], "login failed", 1),
            ([500], "login failed", 1),
            ([404], "login failed", 1),
        ],
        ids=[
            "denied",
            "expired",
            "invalid-grant",
            "unsupported-grant",
            "unknown-code",
            "http-500",
            "http-404",
        ],
    )
    def test_poll_ends_in_error(self, fake, replies, match, polls):
        fake.replies = replies
        with pytest.raises(creds.LoginError, match=match):
            _login()
        assert fake.polls == polls

    def test_busy_start_is_retried_after_retry_after(self, fake, monkeypatch):
        real = fake.handle
        busy = [httpx.Response(503, headers={"Retry-After": "7"}), httpx.Response(429)]

        def handle(request):
            if request.url.path == "/oauth/device_authorization" and busy:
                return busy.pop(0)
            return real(request)

        fake.handle = handle
        result, slept = _login()
        assert result.token == "rpa_1"
        assert slept[:2] == [7, 5]  # Retry-After, then the default when absent
        assert len(fake.started) == 1

    @pytest.mark.parametrize(
        ("status", "match", "calls"),
        [
            (503, "refused \\(503", ["/oauth/device_authorization"] * creds.START_ATTEMPTS),
            (404, "does not offer command-line login", ["/oauth/device_authorization"]),
        ],
        ids=["always-busy", "device-endpoint-404-no-retry"],
    )
    def test_device_start_refused(self, monkeypatch, status, match, calls):
        seen = []
        meta = FakeServer().metadata

        def handle(request):
            if request.url.path == creds.METADATA_PATH:
                return httpx.Response(200, json=meta)
            seen.append(request.url.path)
            return httpx.Response(status)

        monkeypatch.setattr(
            httpx,
            "Client",
            lambda *a, **kw: _RealClient(base_url=SERVER, transport=httpx.MockTransport(handle)),
        )
        with pytest.raises(creds.LoginError, match=match):
            _login()
        assert seen == calls

    @pytest.mark.parametrize(
        ("metadata", "match"),
        [
            (None, "does not offer command-line login \\(no /.well-known"),
            ("<html>", "does not offer command-line login \\(no /.well-known"),
            ({"issuer": OTHER_SERVER}, "names issuer 'https://other.example.org'"),
            ({"device_authorization_endpoint": None}, "no device_authorization_endpoint"),
            ({"token_endpoint": ""}, "no token_endpoint"),
        ],
        ids=[
            "no-metadata",
            "html-page",
            "issuer-mismatch",
            "no-device-endpoint",
            "no-token-endpoint",
        ],
    )
    def test_discovery_refuses(self, fake, metadata, match):
        if not isinstance(metadata, dict):
            real = fake.handle
            page = httpx.Response(404) if metadata is None else httpx.Response(200, text=metadata)
            fake.handle = lambda r: page if r.url.path == creds.METADATA_PATH else real(r)
        else:
            fake.metadata.update(metadata)
        with pytest.raises(creds.LoginError, match=match):
            _login()
        assert fake.started == []

    def test_endpoints_come_from_metadata(self, fake):
        fake.metadata["device_authorization_endpoint"] = f"{SERVER}/elsewhere/device"
        real = fake.handle
        seen = []

        def handle(request):
            seen.append(request.url.path)
            if request.url.path == "/elsewhere/device":
                request.url = request.url.copy_with(path="/oauth/device_authorization")
            return real(request)

        fake.handle = handle
        result, _ = _login()
        assert result.token == "rpa_1"
        assert seen[:2] == [creds.METADATA_PATH, "/elsewhere/device"]

    def test_timeout(self, monkeypatch):
        server = FakeServer(replies=["authorization_pending"] * 10**3)
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: server.client())
        clock = iter(range(0, 10**6))
        monkeypatch.setattr(creds.time, "monotonic", lambda: next(clock))
        with pytest.raises(creds.LoginError, match="timed out"):
            creds.login(SERVER, open_browser=False, device=True, timeout=5, sleep=lambda _s: None)

    def test_rp_login_device_stores_and_whoami_reads(self, fake, monkeypatch, capsys):
        monkeypatch.setattr(creds.time, "sleep", lambda _s: None)
        assert main(["login", SERVER, "--device"]) == 0
        stored = creds.load_login()
        assert stored is not None and stored.token == "rpa_1"
        assert stat.S_IMODE(creds.credentials_path().stat().st_mode) == 0o600
        assert "logged in to" in capsys.readouterr().err

        assert main(["whoami"]) == 0
        out = capsys.readouterr().out
        assert "Jane A. Doe" in out and "jane-doe" in out

        assert main(["whoami", "--json"]) == 0
        rec = json.loads(capsys.readouterr().out)
        assert rec["url"] == SERVER and rec["scopes"] == ["push_own", "read"]

        # A later `rp login` with no server argument re-logs into the same server.
        fake.collected = False
        fake.polls = 0
        assert main(["login", "--device"]) == 0
        assert fake.started[-1]["label"]  # the hostname, by default

    def test_rp_login_reports_failure(self, fake, monkeypatch, capsys):
        fake.collected = True
        monkeypatch.setattr(creds.time, "sleep", lambda _s: None)
        assert main(["login", SERVER, "--device"]) == 1
        assert "login failed" in capsys.readouterr().err
        assert creds.load_login() is None


def _management_client() -> tuple[ManagementClient, MagicMock]:
    """A management client with its one session replaced by a request recorder."""
    client = ManagementClient(
        Credential(key="rpa_test", url="https://profiles.example.org", source="test")
    )
    session = MagicMock()
    client._session = session
    return client, session


class TestManagementClient:
    """The management client preserves the profile-service wire contract."""

    @pytest.mark.parametrize(
        "resource, method, args, kwargs, http_method, path, body",
        [
            ("identity", "whoami", (), {}, "get", "/api/v1/manage/agent/whoami", None),
            ("profile", "get", (SLUG,), {}, "get", f"/api/v1/profiles/{SLUG}?view=full", None),
            (
                "profile",
                "patch_metadata",
                (SLUG, {"name": "Jane Q. Doe"}),
                {"base_hash": "before"},
                "patch",
                f"/api/v1/profiles/{SLUG}/metadata",
                {"name": "Jane Q. Doe", "base_hash": "before"},
            ),
            (
                "profile",
                "patch_visibility",
                (SLUG,),
                {
                    "profile_visibility": "limited",
                    "artifacts": [{"role": "soul", "visibility": "private"}],
                },
                "patch",
                f"/api/v1/profiles/{SLUG}/visibility",
                {
                    "profile_visibility": "limited",
                    "artifacts": [{"role": "soul", "visibility": "private"}],
                },
            ),
            (
                "profile",
                "get_work",
                (SLUG, "doe2016example"),
                {},
                "get",
                f"/api/v1/profiles/{SLUG}/papers/doe2016example?view=full",
                None,
            ),
            (
                "profile",
                "patch_work",
                (SLUG, "doe2016example", {"doi": "10.1/x"}),
                {"base_version": "before"},
                "patch",
                f"/api/v1/profiles/{SLUG}/works/doe2016example",
                {"doi": "10.1/x", "base_version": "before"},
            ),
            (
                "profile",
                "add_work",
                (SLUG, {"paper_id": "doe2026new", "name": "A new work", "type": "authored"}),
                {},
                "post",
                f"/api/v1/profiles/{SLUG}/works",
                {"paper_id": "doe2026new", "name": "A new work", "type": "authored"},
            ),
        ],
        ids=[
            "whoami",
            "get",
            "patch-metadata",
            "patch-visibility",
            "get-work",
            "patch-work",
            "add-work",
        ],
    )
    def test_resource_methods_keep_their_request_shape(
        self, resource, method, args, kwargs, http_method, path, body
    ):
        client, session = _management_client()
        response = MagicMock(status_code=200, is_success=True)
        response.json.return_value = {"ok": True}
        getattr(session, http_method).return_value = response

        result = getattr(getattr(client, resource), method)(*args, **kwargs)

        request = getattr(session, http_method)
        url = f"https://profiles.example.org{path}"
        if body is None:
            request.assert_called_once_with(url)
        else:
            request.assert_called_once_with(url, json=body)
        assert result == {"ok": True}

    @pytest.mark.parametrize(
        "resource, method, args, kwargs",
        [
            ("identity", "whoami", (), {}),
            ("profile", "get", (SLUG,), {}),
            ("profile", "patch_metadata", (SLUG, {"name": "Jane"}), {}),
            ("profile", "add_work", (SLUG, {"paper_id": "doe2026new", "name": "New"}), {}),
            ("profile", "patch_visibility", (SLUG,), {}),
            ("profile", "get_work", (SLUG, "doe2016example"), {}),
            ("profile", "patch_work", (SLUG, "doe2016example", {"doi": "10.1/x"}), {}),
            ("profile", "delete_work", (SLUG, "doe2016example"), {}),
        ],
        ids=[
            "whoami",
            "get",
            "patch-metadata",
            "add-work",
            "patch-visibility",
            "get-work",
            "patch-work",
            "delete-work",
        ],
    )
    def test_resource_methods_raise_insufficient_access(self, resource, method, args, kwargs):
        client, session = _management_client()
        response = MagicMock(status_code=403, is_success=False)
        # The server's shape: FastAPI wraps the structured refusal in ``detail``.
        response.json.return_value = {
            "detail": {
                "error": "insufficient_access",
                "required": ["background", "summary"],
                "missing": ["background"],
                "needs_replace": [],
                "hint": "This needs Write on background.",
            }
        }
        session.get.return_value = response
        session.patch.return_value = response
        session.put.return_value = response
        session.post.return_value = response
        session.delete.return_value = response

        with pytest.raises(InsufficientAccessError) as error:
            getattr(getattr(client, resource), method)(*args, **kwargs)

        assert error.value.missing == ["background"]
        assert error.value.required == ["background", "summary"]
        assert error.value.detail == "This needs Write on background."

    def test_a_not_delegable_refusal_carries_its_hint(self):
        client, session = _management_client()
        response = MagicMock(status_code=403, is_success=False)
        response.json.return_value = {
            "detail": {
                "error": "not_delegable",
                "action": "visibility",
                "hint": "Apps and API keys may not make this change.",
            }
        }
        session.patch.return_value = response

        with pytest.raises(AgentAPIError) as error:
            client.profile.patch_visibility(SLUG, profile_visibility="limited")

        assert not isinstance(error.value, InsufficientAccessError)
        assert error.value.detail == "Apps and API keys may not make this change."
