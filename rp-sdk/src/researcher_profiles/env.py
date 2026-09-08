"""Retired environment-variable names: the ``RETIRED_TERMS`` idiom, for env vars.

The SDK settled on one prefix, ``RESEARCHER_PROFILES_``, for every environment
variable it reads. The older ``RP_*`` names (and one same-concept alias,
``RESEARCHER_PROFILES_DIR``) are gone, with no dual-read shim: an operator who
still exports an old name would otherwise see it silently ignored, the setting
quietly reverting to its Python default. For a database URL that means a
`RuntimeError` naming what *is* missing, which is at least loud. For a
credential (``RP_AGENT_KEY`` / ``RP_API_URL``) or a host selector
(``RP_HOST``), silent fallthrough to the *next* item in the resolution
order (a ``.env`` file, a stored login, a different ``[hosts.*]`` block) is
worse: the process starts fine and quietly authenticates as someone else.

This mirrors :data:`researcher_profiles.validate.RETIRED_TERMS` /
:func:`researcher_profiles.validate.retired_terms`: one dict of old name ->
replacement, checked unconditionally, independent of whether the old name
happens to still "mean" something to the code that reads it. Called once at
each real process start (``rp`` CLI's :func:`researcher_profiles.cli.main`,
the API's ``python -m researcher_profiles.api``, and the ``uvicorn``
module-import default app), not from inside every individual resolver.
"""

import os

#: Old env var name -> its replacement under the ``RESEARCHER_PROFILES_``
#: prefix. ``RP_TEST_DATABASE_URL`` (a pytest-only integration-test gate, never
#: read by any shipped process) is deliberately absent: nothing "starts" for
#: it to guard.
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
    """Refuse to proceed if any retired env var name (see :data:`RETIRED_ENV_VARS`)
    is set.

    Raises :class:`RetiredEnvVarError`, naming every retired variable found and
    its replacement, so a startup failure points at the fix instead of leaving
    the operator to guess why a value they *did* export was ignored.
    """
    hits = [name for name in RETIRED_ENV_VARS if os.environ.get(name)]
    if not hits:
        return
    lines = [f"  {name} -> {RETIRED_ENV_VARS[name]}" for name in hits]
    raise RetiredEnvVarError(
        "retired environment variable(s) set; rename to the replacement shown:\n" + "\n".join(lines)
    )


__all__ = ["RETIRED_ENV_VARS", "RetiredEnvVarError", "check_retired_env_vars"]
