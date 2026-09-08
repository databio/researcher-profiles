"""The two settings resolvers in ``config.py``.

The property that matters is not any single precedence rule but that **every
command resolves the same directory and the same store**: before
:func:`resolve_profiles_root` existed there were three resolvers, and two
commands on one machine reported on two different trees.
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

# The package-wide ``_isolated_operator_config`` fixture already hides the
# ambient root and store URL from every test here.


# The profiles root


class TestResolveProfilesRoot:
    def test_explicit_beats_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv(PROFILES_ROOT_ENV_VAR, str(tmp_path / "from-env"))
        assert resolve_profiles_root(tmp_path / "explicit") == (
            (tmp_path / "explicit").expanduser().resolve()
        )

    def test_environment_beats_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv(PROFILES_ROOT_ENV_VAR, str(tmp_path / "from-env"))
        assert resolve_profiles_root() == (tmp_path / "from-env").resolve()

    def test_falls_back_to_the_built_in_default(self):
        assert resolve_profiles_root() == Path(DEFAULT_CACHE_DIR).expanduser().resolve()


# The store URL


class TestResolveDatabaseUrl:
    def test_explicit_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv(DATABASE_URL_ENV_VAR, "sqlite:///from-env.db")
        assert resolve_database_url("sqlite:///explicit.db") == "sqlite:///explicit.db"

    def test_environment_is_used_when_nothing_is_passed(self, monkeypatch):
        monkeypatch.setenv(DATABASE_URL_ENV_VAR, "sqlite:///from-env.db")
        assert resolve_database_url() == "sqlite:///from-env.db"

    def test_raises_rather_than_guessing_a_store(self):
        # There is no default: a guessed database is a whole
        # profile store written somewhere nobody meant.
        with pytest.raises(DatabaseUrlNotConfigured) as e:
            resolve_database_url()
        assert "--database-url" in str(e.value)
        assert DATABASE_URL_ENV_VAR in str(e.value)


# A retired ``RP_*`` name is never silently ignored


class TestRetiredEnvVars:
    """A resolver refuses to run rather than silently ignore an old name.

    Without this, ``RP_PROFILES_ROOT``/``RP_DATABASE_URL`` would just look
    unset after the rename and the caller would fall through to a default (or
    a generic "not configured" error) with no clue that the value it *did*
    export was the problem.
    """

    def test_resolve_profiles_root_refuses_a_retired_name(self, monkeypatch):
        monkeypatch.setenv("RP_PROFILES_ROOT", "/tmp/somewhere")
        with pytest.raises(RetiredEnvVarError) as e:
            resolve_profiles_root()
        assert "RP_PROFILES_ROOT" in str(e.value)
        assert PROFILES_ROOT_ENV_VAR in str(e.value)

    def test_resolve_database_url_refuses_a_retired_name(self, monkeypatch):
        monkeypatch.setenv("RP_DATABASE_URL", "sqlite:///old.db")
        with pytest.raises(RetiredEnvVarError) as e:
            resolve_database_url()
        assert "RP_DATABASE_URL" in str(e.value)
        assert DATABASE_URL_ENV_VAR in str(e.value)

    def test_the_merged_alias_is_retired_too(self, monkeypatch):
        monkeypatch.setenv("RESEARCHER_PROFILES_DIR", "/tmp/somewhere")
        with pytest.raises(RetiredEnvVarError) as e:
            resolve_profiles_root()
        assert PROFILES_ROOT_ENV_VAR in str(e.value)
