"""``rp login`` / ``logout`` / ``whoami`` and the stored-credential precedence.

The device flow (RFC 8414 metadata, then RFC 8628: the device authorization
endpoint and the token endpoint it names) is
exercised against a stubbed server (an ``httpx`` mock transport), so this
covers the CLI's side of the protocol: start, poll through pending /
slow_down / 429 until approved or refused, store 0600, without a real server.
The file also pins the agent ``ManagementClient`` wire contract from
``cli/auth/agent.py``.
"""

import json
import stat
from unittest.mock import MagicMock
from urllib.parse import parse_qsl

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
    login = creds.Login(url=SERVER, token="rpk_stored", orcid="0000-0002-1825-0097", name="Jane")
    creds.save_login(login)
    return login


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
        from researcher_profiles.client import PushPlan, PushResult

        returns = {
            "push_profile": PushResult(
                plan=PushPlan(slug="s", exists=False),
                summary={"slug": "s", "name": "n", "level": "l", "indexed": False},
            ),
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

    def test_logout(self, stored, capsys):
        assert main(["logout"]) == 0
        assert creds.load_login() is None
        assert main(["logout"]) == 0
        assert "not logged in" in capsys.readouterr().out


class FakeServer:
    """Enough of RFC 8414 metadata and the RFC 8628 endpoints it names
    (``/oauth/device_authorization`` + ``/oauth/token``) to drive ``rp login``.

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
        self.metadata: dict = {
            "issuer": SERVER,
            "device_authorization_endpoint": f"{SERVER}/oauth/device_authorization",
            "token_endpoint": f"{SERVER}/oauth/token",
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/oauth-authorization-server" and request.method == "GET":
            return httpx.Response(200, json=self.metadata)
        if path == "/oauth/device_authorization" and request.method == "POST":
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
        if path == "/oauth/token" and request.method == "POST":
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
        if path == "/api/v1/manage/whoami":
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
        fake.complete_uri = complete_uri
        got = []
        monkeypatch.setattr(creds.webbrowser, "open", got.append)
        _login(open_browser=True)
        assert got == opened
        assert "Code: ABCD-EFGH" in capsys.readouterr().err

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
        assert result.token == "rpk_minted"
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
        assert result.token == "rpk_minted"
        assert seen[:2] == [creds.METADATA_PATH, "/elsewhere/device"]

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
