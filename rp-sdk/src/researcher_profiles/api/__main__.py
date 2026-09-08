"""CLI entry point: ``python -m researcher_profiles.api``."""

import argparse
import os
import sys

import uvicorn

from ..env import RetiredEnvVarError
from ..store import build_store
from ..store.config import DATABASE_URL_ENV_VAR, PROFILES_ROOT_ENV_VAR
from .app import create_app
from .deps import configure_logging


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="researcher-profiles HTTP API")
    p.add_argument(
        "--profiles-dir",
        default=None,
        help=f"Directory containing one subdirectory per profile (default: ${PROFILES_ROOT_ENV_VAR}).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help=(
            "Serve a SQL profile store instead of a directory (needs the `sql` "
            f"extra). Wins over --profiles-dir when both are given. "
            f"(default: ${DATABASE_URL_ENV_VAR})"
        ),
    )
    p.add_argument(
        "--host",
        default=os.environ.get("RESEARCHER_PROFILES_HOST", "127.0.0.1"),
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("RESEARCHER_PROFILES_PORT", "8109")),
    )
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    configure_logging(verbose=args.verbose)
    token = os.environ.get("RESEARCHER_PROFILES_TOKEN") or None
    # build_store() is the one place the flag-then-env, database-over-directory
    # composition rule lives (see its docstring); a bare CLI flag left unset
    # here falls through to it reading the environment itself, so this and
    # `create_app`'s own env-driven default can never disagree.
    try:
        store = build_store(database_url=args.database_url, profiles_dir=args.profiles_dir)
    except (ValueError, RetiredEnvVarError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    app = create_app(store, token=token)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
