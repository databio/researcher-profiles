"""Where the profile cache and the SQL profile store live.

Every command resolves the cache through :func:`resolve_profiles_root` and the
SQL store through :func:`resolve_database_url`. There is no configuration file:
a flag beats an environment variable.
"""

import os
from pathlib import Path

from ..env import check_retired_env_vars

__all__ = [
    "DATABASE_URL_ENV_VAR",
    "DEFAULT_CACHE_DIR",
    "PROFILES_ROOT_ENV_VAR",
    "ConfigError",
    "DatabaseUrlNotConfigured",
    "candidate_roots",
    "resolve_database_url",
    "resolve_profiles_root",
    "root_source",
]

#: Environment variable naming the local profile cache. Also the API server's
#: directory-mode setting.
PROFILES_ROOT_ENV_VAR = "RESEARCHER_PROFILES_ROOT"

#: Where profiles live when nothing else says otherwise.
DEFAULT_CACHE_DIR = "~/researcher-profiles"

#: Environment variable naming the SQL profile store.
DATABASE_URL_ENV_VAR = "RESEARCHER_PROFILES_DATABASE_URL"


class ConfigError(Exception):
    """A setting is missing or unusable."""


class DatabaseUrlNotConfigured(ConfigError):
    """No profile-store URL was supplied by any layer.

    A separate class so a CLI verb can say "set
    ``$RESEARCHER_PROFILES_DATABASE_URL`` or pass ``--database-url``".
    """


def resolve_profiles_root(explicit: str | Path | None = None) -> Path:
    """**The** profile cache in force. Every command must resolve it through here.

    Precedence, highest first:

    1. ``explicit``: a ``--root`` flag
    2. ``$RESEARCHER_PROFILES_ROOT``
    3. :data:`DEFAULT_CACHE_DIR`
    """
    check_retired_env_vars()

    if explicit:
        return Path(explicit).expanduser().resolve()

    from_env = os.environ.get(PROFILES_ROOT_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser().resolve()

    return Path(DEFAULT_CACHE_DIR).expanduser().resolve()


def root_source(explicit: str | Path | None = None) -> str:
    """Which layer :func:`resolve_profiles_root` took its answer from."""
    if explicit:
        return "--root"
    if os.environ.get(PROFILES_ROOT_ENV_VAR):
        return f"${PROFILES_ROOT_ENV_VAR}"
    return f"default {DEFAULT_CACHE_DIR}"


def candidate_roots(explicit: str | Path | None = None) -> list[Path]:
    """Every root in the resolution order that exists, highest precedence first.

    Deduplicated. Lets a caller warn when a slug also exists under a root it
    did not choose, since a stale second copy can be pushed over a whole one.
    """
    ordered: list[Path] = []
    for value in (explicit, os.environ.get(PROFILES_ROOT_ENV_VAR), DEFAULT_CACHE_DIR):
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        if path not in ordered and path.is_dir():
            ordered.append(path)
    return ordered


def resolve_database_url(explicit: str | None = None) -> str:
    """**The** SQL profile store in force. Every command must resolve it here.

    Precedence, highest first:

    1. ``explicit``: a ``--database-url`` flag
    2. ``$RESEARCHER_PROFILES_DATABASE_URL``

    No default: an invented database would let a misconfigured command write
    a store somewhere nobody meant. Raises :class:`DatabaseUrlNotConfigured`.
    """
    check_retired_env_vars()

    if explicit:
        return explicit

    from_env = os.environ.get(DATABASE_URL_ENV_VAR)
    if from_env:
        return from_env

    raise DatabaseUrlNotConfigured(
        "no profile-store database URL is configured. Supply one, highest "
        "precedence first:\n"
        "  1. --database-url postgresql://user@host/db\n"
        f"  2. ${DATABASE_URL_ENV_VAR}"
    )
