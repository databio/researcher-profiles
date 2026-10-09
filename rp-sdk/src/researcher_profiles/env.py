"""Refuse to start when a retired environment-variable name is set.

Every variable the SDK reads uses the ``RESEARCHER_PROFILES_`` prefix. An old
name would otherwise be silently ignored. For a credential or host selector
that is a security problem: resolution falls through to the next source (a
``.env`` file, a stored login) and the process quietly authenticates as
someone else. Checked once at each process start.
"""

import os

#: Old env var name -> its replacement. ``RP_TEST_DATABASE_URL`` is a
#: test-only gate, so it is deliberately absent.
RETIRED_ENV_VARS: dict[str, str] = {
    "RP_PROFILES_ROOT": "RESEARCHER_PROFILES_ROOT",
    "RESEARCHER_PROFILES_DIR": "RESEARCHER_PROFILES_ROOT",
    "RP_DATABASE_URL": "RESEARCHER_PROFILES_DATABASE_URL",
    "RP_API_URL": "RESEARCHER_PROFILES_API_URL",
    "RP_AGENT_KEY": "RESEARCHER_PROFILES_AGENT_KEY",
    "RP_EMBEDDING_DEVICE": "RESEARCHER_PROFILES_EMBEDDING_DEVICE",
    "RP_HOST": "RESEARCHER_PROFILES_AUTH_HOST",
    "RP_REGISTRY_URL": "RESEARCHER_PROFILES_REGISTRY_URL",
}


class RetiredEnvVarError(Exception):
    """One or more retired environment variable names are set."""


def check_retired_env_vars() -> None:
    """Raise :class:`RetiredEnvVarError` naming every retired variable set and its replacement."""
    hits = [name for name in RETIRED_ENV_VARS if os.environ.get(name)]
    if not hits:
        return
    lines = [f"  {name} -> {RETIRED_ENV_VARS[name]}" for name in hits]
    raise RetiredEnvVarError(
        "retired environment variable(s) set; rename to the replacement shown:\n" + "\n".join(lines)
    )


__all__ = ["RETIRED_ENV_VARS", "RetiredEnvVarError", "check_retired_env_vars"]
