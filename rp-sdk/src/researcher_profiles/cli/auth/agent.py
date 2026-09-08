"""Agent credential resolution and management client for profile editing.

Resolution order for credentials:
1. Explicit ``key`` / ``url`` arguments to :func:`resolve_credential` (the
   ``rp agent`` and ``rp profile`` commands select a host with ``--host`` only)
2. ``RESEARCHER_PROFILES_AGENT_KEY`` / ``RESEARCHER_PROFILES_API_URL``
   environment variables
3. Nearest ``.env`` walked up from the working directory
4. ``$XDG_CONFIG_HOME/researcher-profiles/credentials.toml``
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
import tomllib

from ...env import check_retired_env_vars

AGENT_KEY_PREFIX = "rpa_"
CONFIG_FILENAME = "credentials.toml"
AGENT_KEY_ENV_VAR = "RESEARCHER_PROFILES_AGENT_KEY"
API_URL_ENV_VAR = "RESEARCHER_PROFILES_API_URL"
HOST_ENV_VAR = "RESEARCHER_PROFILES_AUTH_HOST"


class CredentialError(Exception):
    """Raised when no valid credential can be resolved."""


class AgentAPIError(Exception):
    """Raised on a non-2xx response from the profile service."""

    def __init__(self, status: int, detail: str, body: Any = None):
        self.status = status
        self.detail = detail
        self.body = body
        super().__init__(f"HTTP {status}: {detail}")


class InsufficientScopeError(AgentAPIError):
    """A 403 with the insufficient_scope body shape."""

    def __init__(self, status: int, detail: str, body: dict):
        self.missing = body.get("missing", [])
        self.hint = body.get("hint", "")
        super().__init__(status, detail, body)


@dataclass(frozen=True)
class Credential:
    key: str
    url: str
    source: str
    profile: Optional[str] = None
    owner: Optional[str] = None
    scopes: list[str] = field(default_factory=list)


def _find_dotenv() -> Optional[Path]:
    """Walk up from cwd looking for a ``.env`` file."""
    d = Path.cwd()
    while True:
        candidate = d / ".env"
        if candidate.is_file():
            return candidate
        parent = d.parent
        if parent == d:
            break
        d = parent
    return None


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE, no interpolation."""
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'\"")
        result[k] = v
    return result


def _config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "researcher-profiles"
    return Path.home() / ".config" / "researcher-profiles"


def _check_permissions(path: Path) -> None:
    """Refuse a credentials file with group/other bits set."""
    mode = path.stat().st_mode
    if mode & 0o077:
        raise CredentialError(
            f"Credentials file {path} has mode {oct(mode & 0o777)}. Run: chmod 600 {path}"
        )


def resolve_credential(
    host: str | None = None,
    key: str | None = None,
    url: str | None = None,
) -> Credential:
    """Resolve an agent credential through the four-step order.

    Raises ``CredentialError`` with a diagnostic message when nothing resolves.
    """
    check_retired_env_vars()

    looked_at: list[str] = []

    # 1. Explicit flags
    if key and url:
        return Credential(key=key, url=url, source="flags")

    # 2. Environment variables
    env_key = os.environ.get(AGENT_KEY_ENV_VAR)
    env_url = os.environ.get(API_URL_ENV_VAR)
    if env_key and env_url:
        return Credential(key=env_key, url=env_url, source="environment")
    if env_key:
        looked_at.append(f"{AGENT_KEY_ENV_VAR} (set, but {API_URL_ENV_VAR} missing)")
    if env_url:
        looked_at.append(f"{API_URL_ENV_VAR} (set, but {AGENT_KEY_ENV_VAR} missing)")

    # 3. .env file
    dotenv = _find_dotenv()
    if dotenv:
        looked_at.append(str(dotenv))
        env = _parse_dotenv(dotenv)
        dotenv_key = env.get(AGENT_KEY_ENV_VAR)
        dotenv_url = env.get(API_URL_ENV_VAR)
        if dotenv_key and dotenv_url:
            return Credential(key=dotenv_key, url=dotenv_url, source=str(dotenv))

    # 4. credentials.toml
    config_path = _config_dir() / CONFIG_FILENAME
    looked_at.append(str(config_path))
    if config_path.is_file():
        _check_permissions(config_path)
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        target = host or os.environ.get(HOST_ENV_VAR) or config.get("default")
        if target:
            hosts = config.get("hosts", {})
            entry = hosts.get(target, {})
            if entry.get("key") and entry.get("url"):
                return Credential(
                    key=entry["key"],
                    url=entry["url"],
                    source=f"{config_path} [hosts.{target}]",
                    profile=entry.get("profile"),
                    owner=entry.get("owner"),
                    scopes=entry.get("scopes", []),
                )

    raise CredentialError(
        "No agent credential found. Looked at: "
        + ", ".join(looked_at)
        + ". Mint one from your profile server's agent management page, or ask "
        "your profile's owner to mint one for you."
    )


class ManagementClient:
    """Authenticated gateway to the profile-management service.

    ``identity`` and ``profile`` expose the service's two remote resources while
    sharing this client's session, URL construction, and error translation.
    """

    def __init__(self, credential: Credential):
        self.credential = credential
        self._session = httpx.Client(
            headers={
                "Authorization": f"Bearer {credential.key}",
                "Accept": "application/json",
            }
        )
        self._base = credential.url.rstrip("/")
        self.identity = IdentityClient(self)
        self.profile = ProfileClient(self)

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _handle_response(self, resp: httpx.Response) -> Any:
        if resp.status_code == 401:
            try:
                detail = resp.json().get("detail", resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            raise AgentAPIError(401, f"Authentication failed: {detail}")

        if resp.status_code == 403:
            try:
                body = resp.json()
                detail = body if isinstance(body, dict) else {"detail": body}
            except ValueError:
                detail = {"detail": resp.text}
            if isinstance(detail, dict) and detail.get("error") == "insufficient_scope":
                raise InsufficientScopeError(403, detail.get("hint", str(detail)), detail)
            raise AgentAPIError(403, str(detail.get("detail", detail)), detail)

        if resp.status_code == 409:
            try:
                detail = resp.json().get("detail", resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            raise AgentAPIError(409, f"Conflict: {detail}")

        if not resp.is_success:
            try:
                detail = resp.json().get("detail", resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            raise AgentAPIError(resp.status_code, detail)

        if resp.status_code == 204:
            return None
        return resp.json()

    def _get(self, path: str) -> Any:
        return self._handle_response(self._session.get(self._url(path)))

    def _patch(self, path: str, body: dict[str, Any]) -> Any:
        return self._handle_response(self._session.patch(self._url(path), json=body))

    def _put(self, path: str, body: dict[str, Any]) -> Any:
        return self._handle_response(self._session.put(self._url(path), json=body))


class IdentityClient:
    """Remote identity introspection resource."""

    def __init__(self, client: ManagementClient):
        self._client = client

    def whoami(self) -> dict:
        return self._client._get("/api/manage/agent/whoami")

    def scopes(self) -> dict:
        return self._client._get("/api/manage/agent/scopes")


class ProfileClient:
    """Remote profile read and editing resource."""

    def __init__(self, client: ManagementClient):
        self._client = client

    def get(self, slug: str) -> dict:
        return self._client._get(f"/api/v1/profiles/{slug}")

    def patch_metadata(self, slug: str, patch: dict, base_hash: str | None = None) -> dict:
        body = dict(patch)
        if base_hash:
            body["base_hash"] = base_hash
        return self._client._patch(f"/api/v1/profiles/{slug}/metadata", body)

    def put_soul(self, slug: str, soul: str, base_hash: str | None = None) -> dict:
        body: dict[str, Any] = {"soul": soul}
        if base_hash:
            body["base_hash"] = base_hash
        return self._client._put(f"/api/v1/profiles/{slug}/soul", body)

    def patch_visibility(
        self,
        slug: str,
        profile_visibility: str | None = None,
        artifacts: list[dict] | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if profile_visibility is not None:
            body["profile_visibility"] = profile_visibility
        if artifacts is not None:
            body["artifacts"] = artifacts
        return self._client._patch(f"/api/v1/profiles/{slug}/visibility", body)


__all__ = [
    "AGENT_KEY_PREFIX",
    "AgentAPIError",
    "CONFIG_FILENAME",
    "Credential",
    "CredentialError",
    "IdentityClient",
    "InsufficientScopeError",
    "ManagementClient",
    "ProfileClient",
    "resolve_credential",
]
