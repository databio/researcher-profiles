# snapgate.oauth_client

The client side of snapgate's OAuth server: it signs a command line, a
service or a local web app in to a server and keeps its access token fresh.
It replaces pasted API keys with sign-in based connections that renew
themselves.

It uses only the Python standard library and imports only its own sibling
modules, so this folder can be copied alone into any package. It ships under
the MIT license (`LICENSE`), so it can go inside public packages. It holds no
secrets.

**Never edit a copy.** Change it upstream in `webdev/snapgate`, then re-vendor.

## What is in it

| Module | What it does |
|---|---|
| `models.py` | `ClientConfig` (how one app signs in; `.route(url)`), `ServerMetadata`, `CredentialKey`, `Credential` (`.status_dict()`), `PendingAuthorization` |
| `discovery.py` | `discover(server, config, *, http)`: RFC 8414 metadata, with an RFC 9728 fallback (`config.probe_path`), fetched over `config.backchannel_url` when set; `normalize_server(url)` |
| `grants.py` | `begin_authorization` (the authorize URL), `resume_authorization` (rebuild a saved attempt), `complete_authorization` (check the browser's answer, trade the code), `refresh_credential`, `revoke_credential`, `parse_token_response`, `new_pkce` |
| `device.py` | RFC 8628 device login, client side: `start_device_authorization`, `poll_device_token` |
| `store.py` | the `CredentialStore` contract, `FileStore` (one JSON file, many logins), `MemoryStore`, `tombstone` |
| `source.py` | `TokenSource`: the one place a login is renewed, saved and ended: `access_token()`, `after_unauthorized(token)`, `call(fn)`, `save(cred)`, `sign_out()` |
| `loopback.py` | `LoopbackReceiver`: the `http://127.0.0.1:<port>/callback` listener |
| `cli.py` | `CliApp`, `login`, `logout`, `status` (the stored `Credential` or None), `print_token`, `add_login_flags`, `main` |
| `http.py` | the `Http` protocol, `HttpResponse`, `TransportError`, `UrllibHttp` (the default) |
| `errors.py` | `OAuthClientError` and its family (below) |
| `testing.py` | `FakeAuthorizationServer`, an in-memory server with snapgate's rules, for host tests |

Everything except `cli` and `testing` is re-exported from the package.

```python
from mypkg._vendor.snapgate_oauth_client import (
    ClientConfig,
    CredentialKey,
    FileStore,
    TokenSource,
    cli,
)

CONFIG = ClientConfig(
    client_id="lumenoia-cli",  # the server's NativeClient.client_id
    app_name="lmn",  # used in messages and the User-Agent
    scope="cli:login",  # None sends no scope
    resource_path="/api/v1",  # resource=<issuer>/api/v1 (RFC 8707); None sends none
    relogin_hint="Run `lmn login`.",
    probe_path="/api/v1/whoami",  # optional RFC 9728 fallback for a UI host
)
store = FileStore(Path.home() / ".config/lumenoia/credentials.json", login_command="lmn login")
token = TokenSource(CONFIG, store, CredentialKey("https://lumenoia-api.databio.org")).access_token()
```

## Three ways to embed it

1. **A command line or Python service.** Vendor the folder (below). Build a
   `cli.CliApp(config, FileStore(path), login_command, identify=...)`, and call
   `cli.login(app, server)`, `cli.logout`, `cli.status` from your own
   commands, or hand argv to `cli.main(app, argv, prog=...)`. Get tokens with
   `TokenSource(config, store, CredentialKey(server))`: `access_token()` before
   each call, `after_unauthorized(token)` after a 401, or `call(fn)` for both.
2. **A local web app with a Python backend.** Either run a `LoopbackReceiver`
   on a thread and return `pending.authorization_url` to the page, or own a
   `http://127.0.0.1:<port>/callback` route yourself: call
   `begin_authorization(meta, config, redirect_uri)`, keep
   `pending.to_dict()` (about 200 bytes: session cookie or database row)
   until the callback, then
   `complete_authorization(resume_authorization(meta, config, saved), query, config, http=...)`
   and save the credential with `TokenSource(config, store, key).save(cred)`.
3. **A non-Python app (HQ).** Never refresh on your own: two refreshers with
   one refresh token end the login. Run a host command line built with
   `cli.main(app)` and `token_command=True`: `<cli> token <server>` prints a
   fresh access token, and only it, on stdout. Cache it until
   `<cli> status <server> --json` says `access_expires_at`.

`token_command` is off by default. Leave it off when the login acts as the
person (lmn's does, approvals included): other programs must not copy it.

## Rules the client follows

They match snapgate's server (`snapgate/oauth.py`):

- PKCE S256 always. A fresh `state` per attempt, compared in constant time.
  `iss` (RFC 9207) must match when present, and must be present when the
  metadata says `authorization_response_iss_parameter_supported`.
- `discover` checks that `issuer` equals the origin it fetched the metadata
  from (issuers with a path are not supported), that there is a token
  endpoint, that endpoints are https (except on loopback hosts), and that S256
  is offered when methods are listed.
- The credential stores its `token_endpoint` and `revocation_endpoint`, so
  refresh and revoke need no discovery. If a server moves them, refresh fails
  and the person signs in again.
- A refresh token is presented only while holding the store's lock, after a
  fresh re-read, so two processes never spend the same token. A refresh never
  sends `scope`.
- A refresh that fails on the network is retried once, at once, with the same
  token. If the first try reached the server and rotated, the token is retired
  but inside its 120 s grace window, so the retry is accepted. Two tries at
  `timeout <= 50` s always fit inside 120 s; `ClientConfig` refuses a larger
  timeout. If both tries fail after the first one rotated, the next run (after
  120 s) shows a retired token and the server ends the login: the person sees
  `LoginEnded` and signs in again.
- `invalid_grant` on refresh means revoked, lapsed, or a replay. `TokenSource`
  writes a tombstone (`ended_at`, `ended_reason`) and raises `LoginEnded`, so a
  status page can say why the login died instead of failing silently.
- A 403 (for example `key_confined`) is never a reason to refresh.
- A token response's `account` object (a snapgate server names the person to
  its registered clients) is kept in `Credential.extra["account"]`.
- `ClientConfig(backchannel_url="http://mygrants:8128")` sends discovery,
  token and revoke calls over a private address (a docker network);
  `config.route(url)` swaps a URL's origin for it. The metadata must still
  name the public issuer, the stored credential keeps the public endpoints,
  and the browser keeps the public `authorization_endpoint`. Plain http is
  allowed for any host there: it names a network the operator chose.
- `token_auth_method` follows the secret: `"client_secret_basic"` with a
  `client_secret`, else `"none"` (name `"client_secret_post"` to use that).
- `LoopbackReceiver` binds 127.0.0.1 only (never `localhost`), serves only
  `GET /callback`, answers a different `state` with a 400 page and keeps
  waiting, and logs nothing.
- A new login revokes the old one only after it succeeded
  (`TokenSource.save`); `sign_out()` revokes (best effort) and forgets.
- Tokens never appear in a `repr`, a log line or an error message.

## Errors

`str(error)` is one plain sentence for a person; `error.server` names the server.

| Error | When |
|---|---|
| `DiscoveryError` | no or bad metadata, issuer mismatch, missing endpoint |
| `SignInNotNeeded` (a `DiscoveryError`) | the probe path answered 200: the server needs no sign-in |
| `LoginFailed` | the interactive flow failed: timeout, state mismatch, code refused |
| `LoginDenied` (a `LoginFailed`) | the person clicked Deny |
| `IssuerMismatch` (a `LoginFailed`) | the browser's answer named another server, or none when the server promised to (RFC 9207) |
| `LoginRequired` | no token without signing in again (base of the next two) |
| `NotSignedIn` | nothing stored for this server |
| `LoginEnded` | the server refused the refresh, or the entry is a tombstone (`.reason`, `.ended_at`) |
| `ServerUnavailable` | network error, 429 or 5xx (`.retry_after`); a stored login is kept |
| `ClientMisconfigured` | `invalid_client`, `invalid_target`, `invalid_scope`, `invalid_request`, ...: a bug in the app (`.error` is the server's code) |
| `CredentialStoreError` | the credentials file is unreadable or from an older version |

Token endpoint answers:

| Response | Error |
|---|---|
| 400 `invalid_grant` on refresh | `LoginEnded(reason="refused")` |
| 400 `invalid_grant` on code trade | `LoginFailed` |
| 400/401 `invalid_client`, `unauthorized_client`, `invalid_target`, `invalid_scope`, `invalid_request`, `unsupported_grant_type` | `ClientMisconfigured` |
| 429, 503, other 5xx | `ServerUnavailable(retry_after=...)` |
| anything else | `LoginFailed` (code trade) / `ServerUnavailable` (refresh) |

## The store contract

```python
class LockedEntry(Protocol):
    def get(self) -> Credential | None: ...
    def put(self, cred: Credential | None) -> None: ...  # None forgets it; durable at once


class CredentialStore(Protocol):
    def load(self, key: CredentialKey) -> Credential | None: ...  # unlocked read
    def locked(self, key: CredentialKey) -> AbstractContextManager[LockedEntry]: ...
```

A database store (a web app's own table) follows the same rules
(`snapgate.connect` keeps each person's connections in one):

- `locked(key)` keeps other processes out of that key for the whole block.
  For a database: its own session, `SELECT ... FOR UPDATE` on the row, closed
  (rolled back) when the block exits. Threads in one process are kept out by
  the caller's lock (`TokenSource(lock=...)`; `ConnectClient` always passes
  one). `FileStore` and `MemoryStore` also lock threads themselves.
- `put` is durable when it returns (a database store commits inside `put`),
  so a rotated token is saved before anyone uses it, and a tombstone survives
  the `LoginEnded` raised right after it. `put` is the last write in a locked
  block (a commit releases a Postgres row lock).
- `load` may run unlocked only if writes are atomic.
- A tombstone is a `Credential` with `ended_at`/`ended_reason` set and no
  tokens (`tombstone(cred, reason, at)`), so a status page can say why a
  login ended. `Credential.status_dict()` is every field but the tokens,
  plus `signed_in`.
- A store that cannot read a stored login raises `CredentialStoreError`.

`TokenSource(config, store, key, *, http=None, now=time.time, lock=None,
on_ended=None)` is the only code that writes to a store: `access_token()` and
`after_unauthorized(token)` refresh under the lock and save the new pair;
when a refresh ends the login they write the tombstone, call
`on_ended(tombstone)` once after the lock is released, and raise `LoginEnded`;
`save(cred)` stores a new login and then revokes the one it replaced;
`sign_out()` revokes and forgets.

`FileStore(path, login_command=...)` keeps many logins in one JSON file, keyed
by server (`"<owner>@<server>"` when a `CredentialKey` has an owner).
`FileStore.put(key, cred | None)` writes or forgets one (taking the lock
itself), and `FileStore.keys()` lists what is stored:

```json
{"version": 1, "logins": {"https://x.org": {"server": "https://x.org", "issuer": "...",
  "client_id": "...", "token_endpoint": "...", "revocation_endpoint": "...",
  "resource": "...", "scope": "...", "access_token": "...", "access_expires_at": 1767225600,
  "refresh_token": "sgr_...", "obtained_at": 1767222000, "refreshed_at": null,
  "extra": {"email": "..."}, "ended_at": null, "ended_reason": null}}}
```

The file is 0600 (its folder 0700 when created), written atomically (a 0600
temp file, `fsync`, `os.replace`). The lock is `fcntl.flock` on `<path>.lock`
plus a per-path thread lock (only the thread lock on Windows). A file that is
not JSON or has no `"version": 1` raises `CredentialStoreError` naming
`login_command`; a new login replaces it.

## Tests in a host

```python
from mypkg._vendor.snapgate_oauth_client.testing import FakeAuthorizationServer

fake = FakeAuthorizationServer(
    "https://api.example.org", client_id="lumenoia-cli", scope="cli:login", resource_path="/api/v1"
)
cred = cli.login(app, "https://api.example.org", opener=fake.opener(), http=fake)
```

`fake.opener()` is a stand-in browser that really GETs the loopback callback.
`fake.fail_next(...)`, `fake.approve_device()`, `fake.revoke_all()`,
`fake.access_ok(token)` and `fake.calls` cover the rest.

## Vendoring

Web apps get this folder for free: their `scripts/vendor-snapgate.sh` already
copies snapgate's subpackages, and it does nothing until imported. The web
front end (`snapgate/connect.py`: a "Connect" button for web apps) lives
outside this folder, in snapgate proper, and uses it.

A package that is not a snapgate app copies just this folder, byte for byte,
to `<package>/_vendor/snapgate_oauth_client/` with
`scripts/vendor-snapgate-oauth-client.sh` (`--check` compares the copy with
upstream). The host owns `_vendor/__init__.py` (a docstring only), commits the
copy, and excludes `_vendor/` from its own ruff check and format.

Rules for this folder (enforced by snapgate's `tests/oauth_client/test_standalone.py`):

- Import only the standard library and sibling modules (`from .x import y`).
  Never `from ..`, never `snapgate`, fastapi, httpx, requests or pydantic.
- Python 3.10, `from __future__ import annotations`.
- `snapgate/__init__.py` does not import `oauth_client`.
- `__version__` equals snapgate's.
- Every `.py` file starts with the `# SNAPGATE module - do not edit` header.
