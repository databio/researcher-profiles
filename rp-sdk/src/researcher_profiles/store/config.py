"""Where the profile cache and the SQL profile store live.

Two settings, two environment variables, two resolvers. Every command that
reads the cache goes through :func:`resolve_profiles_root`, and every command
that touches the SQL store goes through :func:`resolve_database_url`, so
neither question can be answered two different ways in two different verbs.

There is no configuration file. A flag beats an environment variable, and for
the profiles root a built-in default beats nothing at all.
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
    "resolve_database_url",
    "resolve_profiles_root",
]

#: Environment variable naming the local profile cache. Also the API server's
#: directory-mode setting (formerly the separate ``RESEARCHER_PROFILES_DIR``):
#: one directory, one name, for every command and every host.
PROFILES_ROOT_ENV_VAR = "RESEARCHER_PROFILES_ROOT"

#: Where profiles live when nothing else says otherwise.
DEFAULT_CACHE_DIR = "~/researcher-profiles"

#: Environment variable naming the SQL profile store.
DATABASE_URL_ENV_VAR = "RESEARCHER_PROFILES_DATABASE_URL"


class ConfigError(Exception):
    """A setting is missing or unusable."""


class DatabaseUrlNotConfigured(ConfigError):
    """No profile-store URL was supplied by any layer.

    A separate class because it is actionable in one specific way: set
    ``$RESEARCHER_PROFILES_DATABASE_URL`` or pass ``--database-url``, and a CLI
    verb should be able to say exactly that without string-matching a generic
    config error.
    """


def resolve_profiles_root(explicit: str | Path | None = None) -> Path:
    """**The** profile cache in force. Every command must resolve it through here.

    Precedence, highest first:

    1. ``explicit``: a ``--root`` flag
    2. ``$RESEARCHER_PROFILES_ROOT``
    3. :data:`DEFAULT_CACHE_DIR`

    A cache with more than one answer to "where am I" is not a cache.
    """
    check_retired_env_vars()

    if explicit:
        return Path(explicit).expanduser().resolve()

    from_env = os.environ.get(PROFILES_ROOT_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser().resolve()

    return Path(DEFAULT_CACHE_DIR).expanduser().resolve()


def resolve_database_url(explicit: str | None = None) -> str:
    """**The** SQL profile store in force. Every command must resolve it here.

    Precedence, highest first:

    1. ``explicit``: a ``--database-url`` flag
    2. ``$RESEARCHER_PROFILES_DATABASE_URL``

    There is no third step. A profiles root has a correct
    built-in default (``~/researcher-profiles``); a database does not, and
    inventing one (a stray SQLite file in the working directory) would let a
    misconfigured command silently write a whole profile store somewhere
    nobody meant. Raises :class:`DatabaseUrlNotConfigured` instead.
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
