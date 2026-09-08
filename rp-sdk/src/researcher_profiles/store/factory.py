"""``build_store``: the one composition rule mapping config to a store backend."""

import os
from typing import Optional

from ..env import check_retired_env_vars
from .config import DATABASE_URL_ENV_VAR, PROFILES_ROOT_ENV_VAR
from .files import FilesystemProfileStore
from .protocol import ProfileStore


def build_store(
    *,
    database_url: Optional[str] = None,
    profiles_dir: Optional[str] = None,
) -> ProfileStore:
    """Build the store an operator's configuration describes.

    Precedence, highest first, database over directory at each step:

    1. ``database_url`` / ``profiles_dir`` passed explicitly
    2. ``$RESEARCHER_PROFILES_DATABASE_URL`` / ``$RESEARCHER_PROFILES_ROOT``

    This is exactly one composition rule, shared by ``create_app``'s
    env-driven default (:func:`researcher_profiles.api.app._build_default_app`)
    and ``python -m researcher_profiles.api``, so the two can never disagree:
    neither caller reads either environment variable itself, both just call
    this (with, at most, a CLI flag as ``database_url``/``profiles_dir``) and
    let it decide.

    Deliberately does not fall back to :data:`~.config.DEFAULT_CACHE_DIR`
    (contrast :func:`~.config.resolve_profiles_root`, which does, for the
    CLI's local cache): a server silently pointed at a guessed directory is a
    worse failure than a server that refuses to start, and unlike the CLI's
    profiles root, nothing here has a "correct" guess. Raises ``ValueError``
    when neither a database URL nor a profiles directory is configured by any
    means.

    Also the fail-loud guard for retired ``RP_*`` env var names
    (:func:`researcher_profiles.env.check_retired_env_vars`): a caller that
    still exports ``RP_DATABASE_URL`` would otherwise see it silently ignored
    and this function fall through to "not configured" with no clue why.
    """
    check_retired_env_vars()

    if database_url is None:
        database_url = os.environ.get(DATABASE_URL_ENV_VAR)
    if profiles_dir is None:
        profiles_dir = os.environ.get(PROFILES_ROOT_ENV_VAR)

    if database_url:
        from .sql import SqlProfileStore

        return SqlProfileStore(database_url)
    if profiles_dir:
        return FilesystemProfileStore(profiles_dir)
    raise ValueError(
        "build_store needs either database_url or profiles_dir, explicitly or "
        f"via ${DATABASE_URL_ENV_VAR} / ${PROFILES_ROOT_ENV_VAR}"
    )
