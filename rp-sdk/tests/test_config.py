"""The two settings resolvers in ``config.py``.

Every command must resolve the same directory and the same store; three resolvers once
made two commands on one machine report on two different trees.
"""

from pathlib import Path

import pytest

from researcher_profiles.env import RetiredEnvVarError
from researcher_profiles.store.config import (
    DATABASE_URL_ENV_VAR,
    DEFAULT_CACHE_DIR,
    PROFILES_ROOT_ENV_VAR,
    DatabaseUrlNotConfigured,
    resolve_database_url,
    resolve_profiles_root,
)

# The package-wide ``_isolated_operator_config`` fixture hides the ambient root and store URL.

DEFAULT = object()


class TestResolveProfilesRoot:
    @pytest.mark.parametrize(
        "env,explicit,expected",
        [
            ("from-env", "explicit", "explicit"),
            ("from-env", None, "from-env"),
            (None, None, DEFAULT),
        ],
        ids=["explicit-beats-env", "env-beats-default", "built-in-default"],
    )
    def test_precedence(self, tmp_path, monkeypatch, env, explicit, expected):
        if env is not None:
            monkeypatch.setenv(PROFILES_ROOT_ENV_VAR, str(tmp_path / env))
        want = Path(DEFAULT_CACHE_DIR) if expected is DEFAULT else tmp_path / expected
        got = resolve_profiles_root(tmp_path / explicit if explicit else None)
        assert got == want.expanduser().resolve()


class TestResolveDatabaseUrl:
    @pytest.mark.parametrize(
        "explicit,expected",
        [
            ("sqlite:///explicit.db", "sqlite:///explicit.db"),
            (None, "sqlite:///from-env.db"),
        ],
        ids=["explicit-beats-env", "env-when-nothing-passed"],
    )
    def test_precedence(self, monkeypatch, explicit, expected):
        monkeypatch.setenv(DATABASE_URL_ENV_VAR, "sqlite:///from-env.db")
        assert resolve_database_url(explicit) == expected

    def test_raises_rather_than_guessing_a_store(self):
        # There is no default: a guessed database is a whole
        # profile store written somewhere nobody meant.
        with pytest.raises(DatabaseUrlNotConfigured) as e:
            resolve_database_url()
        assert "--database-url" in str(e.value)
        assert DATABASE_URL_ENV_VAR in str(e.value)


class TestRetiredEnvVars:
    """A resolver refuses to run rather than silently ignore an old name."""

    @pytest.mark.parametrize(
        "old_name,value,resolver,new_name",
        [
            ("RP_PROFILES_ROOT", "/tmp/somewhere", resolve_profiles_root, PROFILES_ROOT_ENV_VAR),
            ("RP_DATABASE_URL", "sqlite:///old.db", resolve_database_url, DATABASE_URL_ENV_VAR),
            (
                "RESEARCHER_PROFILES_DIR",
                "/tmp/somewhere",
                resolve_profiles_root,
                PROFILES_ROOT_ENV_VAR,
            ),
        ],
    )
    def test_refuses_a_retired_name(self, monkeypatch, old_name, value, resolver, new_name):
        monkeypatch.setenv(old_name, value)
        with pytest.raises(RetiredEnvVarError) as e:
            resolver()
        assert old_name in str(e.value)
        assert new_name in str(e.value)
