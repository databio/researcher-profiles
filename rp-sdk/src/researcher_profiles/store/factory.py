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

    Deliberately no fallback to :data:`~.config.DEFAULT_CACHE_DIR`: a server
    pointed at a guessed directory is worse than one that refuses to start.
    Raises ``ValueError`` when nothing is configured. Fails loudly on retired
    env var names (:func:`researcher_profiles.env.check_retired_env_vars`).
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
