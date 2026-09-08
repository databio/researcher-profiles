"""The `rp` root parser: the plugin hook, the verb-table merge, and ``main``.

The group modules (``_corpus``, ``_store``, ...) each expose ``add_parsers`` and
``COMMANDS``; this module owns only the root parser, the plugin hook, and the
merge of those tables. The package ``__init__`` re-exports :func:`main`,
:func:`build_parser` and :data:`CLI_PLUGIN_GROUP` from here.
"""

import argparse
import logging
from collections.abc import Callable

from . import _agent, _auth, _corpus, _format, _publish, _registry, _shared, _sign, _skill, _store
from ._shared import EXIT_ERROR

logger = logging.getLogger(__name__)

#: Entry-point group through which out-of-tree distributions add subcommands to
#: ``rp``. Each entry point loads to a ``register(subparsers)`` callable that
#: adds its parsers and marks each with a ``_plugin_handler`` default; ``main``
#: dispatches to that handler after parsing. This is the one mechanism by which
#: extra verbs re-enter ``rp`` without the SDK importing any out-of-tree code.
CLI_PLUGIN_GROUP = "researcher_profiles.cli_plugins"

#: The SDK's own verb groups, in the order ``rp --help`` lists them. Plugins
#: register between the two tuples, which is where the hook has always sat.
_GROUPS_BEFORE_PLUGINS = (_corpus, _store, _format, _publish, _registry, _auth, _skill)
_GROUPS_AFTER_PLUGINS = (_sign, _agent)
_GROUPS = _GROUPS_BEFORE_PLUGINS + _GROUPS_AFTER_PLUGINS

#: The SDK's own verbs: one name -> one handler returning an exit code.
#: ``sign`` and ``sign-verify`` share a handler because they share a document,
#: a key path and a JWK Set. Plugin verbs are not here: they arrive carrying a
#: ``_plugin_handler`` default and are dispatched before this table is read.
_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    name: handler for group in _GROUPS for name, handler in group.COMMANDS.items()
}


def _load_cli_plugins(sub: "argparse._SubParsersAction") -> None:
    """Register any installed CLI-plugin subcommands onto ``sub``.

    Failures are logged and swallowed per-plugin: a broken plugin must never
    take down the SDK's own commands.
    """
    from importlib.metadata import entry_points

    for ep in entry_points(group=CLI_PLUGIN_GROUP):
        # Boundary: out-of-tree code loaded by name. Anything it raises on
        # import or registration is that plugin's problem, not the CLI's.
        try:
            register = ep.load()
            register(sub)
        except Exception:  # pragma: no cover - a broken plugin is not fatal
            logger.warning("CLI plugin %r failed to register; skipping it", ep.name, exc_info=True)
            continue


def _version() -> str:
    """The installed distribution version, resolved lazily.

    ``importlib.metadata`` is imported here rather than at module scope because
    ``rp --version`` must be fast and every *other* invocation must not pay for
    it. See :class:`_VersionAction` for why ``action="version"`` will not do.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("researcher-profiles")
    except PackageNotFoundError:  # pragma: no cover - only when run from a tree
        return "0+unknown"


class _VersionAction(argparse.Action):
    """``--version``, computed when asked and not one microsecond earlier.

    argparse's built-in ``action="version"`` takes the version *string*, so the
    dist lookup would run while the parser is being built, on every ``rp``
    invocation, including the ones that never mention ``--version``. This
    package goes to some trouble to keep the cold path cheap (see the deferred
    imports in every handler); paying an ``importlib.metadata`` scan to answer
    ``rp where`` would quietly undo part of that.
    """

    def __init__(
        self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None
    ):
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            default=default,
            nargs=0,
            help=help or "Print the installed rp version and exit",
        )

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ARG002
        print(f"rp {_version()}")
        parser.exit()


_TOP_EPILOG = """\
Start here (the SDK / serving workflow):
  rp where {slug}             # path to a profile
  rp list                     # every profile in the local cache
  rp validate {slug}          # do the format contracts pass?
  rp render {slug}            # render a profile's index.html in place
  rp site ...                 # write the collection files for a set of profiles

Conventions:
  --json           machine-readable stdout; status and warnings go to stderr
  -n N             a COUNT. The dry run is --dry-run.
  --root DIR       the profile cache to act on, for any command that reads one

Environment:
  RESEARCHER_PROFILES_ROOT          Local profile cache [default: ~/researcher-profiles]
  RESEARCHER_PROFILES_DATABASE_URL  SQL store URL for the `rp db` commands
  RESEARCHER_PROFILES_REGISTRY_URL  Registry base URL(s), comma-separated, for
                                    `rp install` and `rp listr`
  RESEARCHER_PROFILES_TOKEN         Bearer token for a registry or a push target

Exit codes:
  0  ok
  1  general error
  2  invalid usage
  4  validation or contract failure
""".format(slug=_shared._EG_SLUG)


def build_parser(*, load_plugins: bool = True) -> argparse.ArgumentParser:
    """Build the full ``rp`` parser, installed CLI plugins included.

    Split out of :func:`main` so the parsing layer is inspectable without
    dispatching: the registered verbs, their defaults, and their ``dest``
    names are a public interface (agents read ``--help`` and copy a line), and
    the only other way to see them was to run a command and catch
    ``SystemExit``.

    ``load_plugins=False`` builds the SDK's own verbs only, skipping the
    ``researcher_profiles.cli_plugins`` entry-point group. That is how a caller
    inspects the SDK's frozen verb surface without it depending on which other
    distributions happen to be installed.
    """
    parser = argparse.ArgumentParser(
        prog="rp",
        # One line, and short enough to survive an 80-column terminal:
        # RawDescriptionHelpFormatter does not re-wrap it.
        description="Build, validate, and read AI-readable researcher profiles.",
        epilog=_TOP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action=_VersionAction)
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")

    for group in _GROUPS_BEFORE_PLUGINS:
        group.add_parsers(sub)
    # Any verbs registered by installed ``researcher_profiles.cli_plugins``
    # entry points, present exactly when those distributions are installed and
    # absent otherwise. This package imports no plugin code at module scope.
    if load_plugins:
        _load_cli_plugins(sub)
    for group in _GROUPS_AFTER_PLUGINS:
        group.add_parsers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv``, run one command, return its exit code."""
    args = build_parser().parse_args(argv)

    # A plugin verb carries its own handler and is dispatched first: the plugin
    # owns those names and this module must never learn them. Order matters:
    # a plugin that shadows an SDK verb wins, as it did before.
    plugin_handler = getattr(args, "_plugin_handler", None)
    if plugin_handler is not None:
        return plugin_handler(args)

    handler = _COMMANDS.get(args.cmd)
    if handler is None:
        # Unreachable: the subparser is `required=True` and argparse rejects an
        # unknown verb with exit 2 long before this. Kept as the same answer the
        # old chain's fallthrough gave.
        return EXIT_ERROR
    return handler(args)
