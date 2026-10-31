"""``rp login`` / ``logout`` / ``whoami`` and the stored-credential precedence.

``rp login`` is a thin adapter over snapgate's shared OAuth client (vendored at
``researcher_profiles._vendor.snapgate_oauth_client``), signing in as the
registered OAuth client ``rp-cli``. These tests check rp's wiring against the
client's ``FakeAuthorizationServer`` (snapgate's rules, plus the device flow
Prosopia offers); the client's own mechanics (PKCE, the loopback listener,
locking, refresh rotation, the device grant) are tested in snapgate. The file
also pins the agent ``ManagementClient`` wire contract from
``cli/auth/agent.py``.
"""

import json
import stat
import time
from unittest.mock import MagicMock

import httpx
import pytest

from researcher_profiles._vendor.snapgate_oauth_client import Credential, CredentialKey
from researcher_profiles._vendor.snapgate_oauth_client import cli as oauth_cli
from researcher_profiles._vendor.snapgate_oauth_client import source as oauth_source
from researcher_profiles._vendor.snapgate_oauth_client.testing import FakeAuthorizationServer
from researcher_profiles.cli import main
from researcher_profiles.cli.auth import credentials as creds
from researcher_profiles.cli.auth.agent import (
    AgentAPIError,
    CredentialError,
    InsufficientAccessError,
    ManagementClient,
    resolve_credential,
)
from researcher_profiles.cli.auth.agent import (
    Credential as AgentCredential,
)

SERVER = "https://people.example.org"
OTHER_SERVER = "https://other.example.org"
SLUG = "jane-doe"
_RealClient = httpx.Client
WHOAMI = {
    "consumer": "agent_abc",
    "scopes": ["push_own", "read"],
    "owner": {"orcid": "0000-0002-1825-0097", "name": "Jane A. Doe"},
    "profiles": [{"slug": "jane-doe", "rid": "0000-0002-1825-0097", "role": "owner"}],
}


def _save(server=SERVER, access="rpa_stored", obtained_at=None, **over):
    """A stored login for ``server``, written straight to the store."""
    now = int(time.time())
    values = dict(
        server=server,
        issuer=server,
        client_id="rp-cli",
        token_endpoint=f"{server}/oauth/token",
        revocation_endpoint=f"{server}/oauth/revoke",
        resource=None,
        scope="profiles:push",
        access_token=access,
        access_expires_at=now + 3600,
        refresh_token="rpr_stored",
        obtained_at=now if obtained_at is None else obtained_at,
    )
    values.update(over)
    cred = Credential(**values)
    creds.store().put(CredentialKey(server), cred)
    return cred


def _stored(server=SERVER):
    return creds.store().load(CredentialKey(server))


@pytest.fixture
def stored():
    return _save()


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


class TestPrecedence:
    @pytest.mark.parametrize(
        ("env", "flag", "url", "expected"),
        [
            ("env-token", "flag-token", SERVER, "flag-token"),
            ("env-token", None, SERVER, "env-token"),
            (None, None, SERVER, "rpa_stored"),
            (None, None, SERVER + "/", "rpa_stored"),
            (None, None, SERVER + "/api/v1/profiles", "rpa_stored"),
            (None, None, None, "rpa_stored"),
            (None, None, OTHER_SERVER, None),  # a stored login never goes to another server
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

    def test_url_defaults_to_the_most_recent_login(self, stored):
        assert creds.resolve_url(None) == SERVER
        assert creds.resolve_url("https://x") == "https://x"
        _save(OTHER_SERVER, access="rpa_other", obtained_at=stored.obtained_at + 10)
        assert creds.resolve_url(None) == OTHER_SERVER
        creds.store().put(CredentialKey(OTHER_SERVER), None)
        creds.store().put(CredentialKey(SERVER), None)
        assert creds.resolve_url(None) is None

    def test_each_login_goes_to_its_own_server(self, stored):
        _save(OTHER_SERVER, access="rpa_other")
        assert creds.resolve_token(None, SERVER) == "rpa_stored"
        assert creds.resolve_token(None, OTHER_SERVER) == "rpa_other"
        assert creds.resolve_token(None, "https://third.example.org") is None


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
            (["push", "{tmp}"], "push_profile", None, SERVER, "rpa_stored", 0),
            (["push", "{tmp}", "--url", OTHER_SERVER], "push_profile", None, OTHER_SERVER, None, 0),
            (["install", "a"], "install_profile", None, SERVER, "rpa_stored", 0),
            # an empty listing is a usage error (exit 2)
            (["listr"], "list_registry", None, SERVER, "rpa_stored", 2),
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


@pytest.fixture
def fake(monkeypatch):
    """A Prosopia-like server (loopback and device flow), wired in as the OAuth
    client's HTTP; the system browser is its stand-in; ``whoami`` (httpx) answers
    its live access tokens, except any in ``fake.rejected``."""
    server = FakeAuthorizationServer(
        SERVER, client_id="rp-cli", scope="profiles:push", device=True, iss=True
    )
    server.device_interval = 0
    server.approve_device(after_polls=1)
    server.rejected = set()
    monkeypatch.setattr(oauth_cli, "UrllibHttp", lambda *a, **k: server)
    monkeypatch.setattr(oauth_source, "UrllibHttp", lambda *a, **k: server)
    monkeypatch.setattr(oauth_cli.webbrowser, "open", server.opener())

    def handle(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if request.url.path != "/api/v1/manage/whoami":
            return httpx.Response(404, json={"detail": "no such route"})
        if not server.access_ok(token) or token in server.rejected:
            return httpx.Response(401, json={"detail": "bad"})
        return httpx.Response(200, json=WHOAMI)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **kw: _RealClient(base_url=SERVER, transport=httpx.MockTransport(handle)),
    )
    return server


def _sent(fake, call):
    return [form for c, form in fake.calls if c == call]


def _refreshes(fake):
    return [f for f in _sent(fake, "POST /oauth/token") if f.get("grant_type") == "refresh_token"]


class TestLoginWiring:
    def test_login_sends_rps_client_scope_and_label_and_no_resource(self, fake, capsys):
        assert main(["login", SERVER + "/", "--label", "laptop"]) == 0
        (q,) = _sent(fake, "GET /oauth/authorize")
        assert q["client_id"] == "rp-cli" and q["scope"] == "profiles:push"
        assert q["label"] == "laptop" and "resource" not in q
        (trade,) = _sent(fake, "POST /oauth/token")
        assert trade["grant_type"] == "authorization_code" and "resource" not in trade
        assert _sent(fake, "POST /oauth/device") == []
        login = _stored()
        assert login.client_id == "rp-cli" and login.resource is None
        assert login.extra == {"orcid": "0000-0002-1825-0097", "name": "Jane A. Doe"}
        assert stat.S_IMODE(creds.credentials_path().stat().st_mode) == 0o600
        err = capsys.readouterr().err
        assert "as Jane A. Doe" in err and str(creds.credentials_path()) in err

    def test_label_defaults_to_the_hostname(self, fake):
        assert main(["login", SERVER]) == 0
        (q,) = _sent(fake, "GET /oauth/authorize")
        assert q["label"] == creds._default_label()

    @pytest.mark.parametrize("how", ["--device", "ssh"])
    def test_device_flag_and_ssh_pick_the_device_flow(self, fake, monkeypatch, capsys, how):
        opened = []
        monkeypatch.setattr(oauth_cli.webbrowser, "open", opened.append)
        argv = ["login", SERVER]
        if how == "ssh":
            monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
        else:
            argv.append("--device")
        assert main(argv + ["--label", "box"]) == 0
        (start,) = _sent(fake, "POST /oauth/device")
        assert start == {"client_id": "rp-cli", "scope": "profiles:push", "label": "box"}
        assert _sent(fake, "GET /oauth/authorize") == [] and opened == []
        assert "Code: ABCD-EFGH" in capsys.readouterr().err
        assert _stored().extra["name"] == "Jane A. Doe"

    def test_json_output(self, fake, capsys):
        assert main(["login", SERVER, "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"url": SERVER, "orcid": "0000-0002-1825-0097", "name": "Jane A. Doe"}

    def test_login_with_no_argument_logs_in_again_to_the_same_server(self, fake):
        assert main(["login", SERVER]) == 0
        first = _stored()
        assert main(["login"]) == 0
        assert _stored().access_token != first.access_token
        assert not fake.access_ok(first.access_token)  # the old login, revoked after

    def test_a_refused_login_reports_and_keeps_nothing(self, fake, monkeypatch, capsys):
        monkeypatch.setattr(oauth_cli.webbrowser, "open", fake.opener(decision="deny"))
        assert main(["login", SERVER]) == 1
        assert "login failed" in capsys.readouterr().err
        assert _stored() is None

    def test_logout_revokes_and_removes(self, fake, capsys):
        assert main(["login", SERVER]) == 0
        token = _stored().refresh_token
        assert main(["logout"]) == 0
        assert _sent(fake, "POST /oauth/revoke") == [
            {"token": token, "token_type_hint": "refresh_token", "client_id": "rp-cli"}
        ]
        assert _stored() is None and fake.live_connections() == []
        assert f"logged out of {SERVER}" in capsys.readouterr().out
        assert main(["logout"]) == 0
        assert "not logged in" in capsys.readouterr().out

    def test_logout_the_server_did_not_confirm(self, fake, capsys):
        from researcher_profiles._vendor.snapgate_oauth_client import TransportError

        assert main(["login", SERVER]) == 0
        fake.fail_next(TransportError)
        assert main(["logout", SERVER]) == 0
        assert _stored() is None
        assert "Privacy page" in capsys.readouterr().err

    def test_whoami_refreshes_on_401_and_retries(self, fake, capsys):
        assert main(["login", SERVER]) == 0
        old = _stored().access_token
        fake.rejected.add(old)  # not expired by the clock, but the server says otherwise
        assert main(["whoami"]) == 0
        assert "Jane A. Doe" in capsys.readouterr().out
        assert len(_refreshes(fake)) == 1 and _stored().access_token != old

    def test_push_refreshes_on_401_through_with_token_refresh(self, fake, monkeypatch, tmp_path):
        assert main(["login", SERVER]) == 0
        old = _stored().access_token
        spy = MagicMock(side_effect=[PermissionError("401"), _push_result()])
        monkeypatch.setattr("researcher_profiles.client.push_profile", spy)
        assert main(["push", str(tmp_path)]) == 0
        new = _stored().access_token
        assert [c.kwargs["token"] for c in spy.call_args_list] == [old, new] and new != old
        (refresh,) = _refreshes(fake)
        assert "scope" not in refresh and "resource" not in refresh

    def test_a_given_token_is_never_refreshed(self, fake, capsys):
        assert main(["login", SERVER]) == 0
        assert main(["whoami", "--token", "rpa_mine"]) == 1
        assert _refreshes(fake) == []

    def test_a_login_that_ended_is_one_plain_error(self, fake, capsys):
        _save(access_expires_at=int(time.time()) - 10)  # expired; the server knows no such token
        assert main(["whoami"]) == 1
        err = capsys.readouterr().err
        assert err.startswith("rp: Your rp login for https://people.example.org has ended")
        assert "Run `rp login` again." in err
        assert _stored().ended_reason == "refused"

    @pytest.mark.parametrize(
        "old",
        [
            {"url": SERVER, "token": "rpk_old", "orcid": "x", "name": "J"},
            {"url": SERVER, "access_token": "rpa_x", "refresh_token": "rpr_x", "expires_at": 1},
        ],
        ids=["rpk-key-file", "previous-rp-login"],
    )
    def test_an_old_format_file_says_run_rp_login(self, fake, tmp_path, capsys, old):
        path = creds.credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(old))
        assert main(["push", str(tmp_path)]) == 1
        assert "Run `rp login` to replace it." in capsys.readouterr().err
        assert main(["login", SERVER]) == 0  # which replaces it
        assert _stored().extra["name"] == "Jane A. Doe"


def _management_client() -> tuple[ManagementClient, MagicMock]:
    """A management client with its one session replaced by a request recorder."""
    client = ManagementClient(
        AgentCredential(key="rpa_test", url="https://profiles.example.org", source="test")
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
