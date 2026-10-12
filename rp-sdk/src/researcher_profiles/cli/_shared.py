"""Exit codes, parser helpers, and the error reports every verb group reuses.

The leaf of the ``cli`` package: it imports nothing from its siblings, and
only stdlib modules at module scope, so building the parser stays cheap.
"""

import argparse
import sys
from pathlib import Path

#: The exit-code table. ``EXIT_VALIDATION`` means "the artifact is wrong", as
#: distinct from "the command was wrong" (2).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_VALIDATION = 4

_TOKEN_HELP = (
    "Bearer token (default: RESEARCHER_PROFILES_TOKEN env var, else the key "
    "stored by `rp login` for this server)"
)
_CREDENTIALS_HINT = "~/.config/researcher-profiles/credentials.json"

#: A concrete slug and a concrete ORCID, used throughout the examples.
_EG_SLUG = "voss-elena"
_EG_ORCID = "0000-0002-1825-0097"


def _epilog(*examples: str, extra: str = "") -> str:
    """An ``Examples:`` block, optionally followed by more prose."""
    body = "Examples:\n" + "\n".join(f"  {line}" for line in examples) + "\n"
    return f"{body}\n{extra}" if extra else body


def add_subcommand(
    sub: argparse._SubParsersAction, name: str, summary: str, *examples: str, extra: str = ""
) -> argparse.ArgumentParser:
    """Register one subcommand with a description and worked examples.

    ``help`` (in ``rp --help``) and ``description`` (in ``rp <cmd> --help``)
    are the same one-liner.
    """
    return sub.add_parser(
        name,
        help=summary,
        description=summary,
        epilog=_epilog(*examples, extra=extra),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def add_json(parser: argparse.ArgumentParser, help_text: str) -> None:
    """Register ``--json`` as ``store_true`` on dest ``as_json``, for every command alike."""
    parser.add_argument("--json", action="store_true", dest="as_json", help=help_text)


def _add_root(parser: argparse.ArgumentParser, help_text: str = "Profiles root") -> None:
    """Register ``--root DIR``: the profile cache this command acts on."""
    parser.add_argument("--root", default=None, help=help_text)


def _add_token(parser: argparse.ArgumentParser) -> None:
    """Register ``--token``, which carries the same help at every site."""
    parser.add_argument("--token", default=None, help=_TOKEN_HELP)


def _add_url(parser: argparse.ArgumentParser, help_text: str) -> None:
    """Register ``--url``: the server or registry base URL to act against."""
    parser.add_argument("--url", default=None, help=help_text)


def _add_host(parser: argparse.ArgumentParser) -> None:
    """Register ``--host``: which entry of credentials.toml to authenticate as."""
    parser.add_argument("--host", help="Host name in credentials.toml")


def profile_dir_for_ref(ref: str, root: str | None) -> Path | None:
    """Resolve a rid or slug to a directory, the way ``rp where`` does.

    ``None`` when there is no root to look in, or nothing matches. The caller
    keeps the literal path it was given so the error names what the user typed.
    """
    from ..store import ProfileNotFoundError
    from ..store.config import resolve_profiles_root
    from ..store.files import FilesystemProfileStore

    the_root = Path(resolve_profiles_root(root)).expanduser()
    if not the_root.is_dir():
        return None
    try:
        return FilesystemProfileStore(the_root).path_for(ref)
    except (ProfileNotFoundError, ValueError, OSError):
        return None


def resolve_profile_arg(verb: str, ref: str, root: str | None = None) -> Path | None:
    """A ``profile`` positional -> a directory, from a path, a rid, or a slug.

    Same rule as ``_resolve_profile_jsonld``: a path that exists means itself,
    so a local directory is never shadowed by a same-named cache entry.

    ``None`` when nothing matches, after writing the standard "not in this
    cache" report to stderr; the caller returns ``EXIT_USAGE``.
    """
    target = Path(ref).expanduser()
    if target.is_dir():
        # The user typed a path. Echoing it back tells them nothing they did
        # not just write.
        return target
    resolved = profile_dir_for_ref(ref, root)
    if resolved is not None:
        _announce_resolution(ref, resolved, root)
        return resolved
    from ..store.config import resolve_profiles_root

    print(f"rp {verb}: no profile at {ref!r}", file=sys.stderr)
    print(root_resolution_note(resolve_profiles_root(root), explicit=root), file=sys.stderr)
    print(
        _next_steps(
            (f"rp where {ref}", "resolve a rid or slug to a path"),
            ("rp list", "what this cache holds"),
        ),
        file=sys.stderr,
    )
    return None


def _announce_resolution(ref: str, resolved: Path, root: str | None) -> None:
    """Name the directory a slug or rid resolved to, on stderr, on success.

    So a command never silently reads a stale copy under a forgotten root.
    stderr keeps a `--json` stdout parseable. A slug that also exists under
    another root gets a warning: a second copy is legal, just rarely intended.
    """
    from ..store.config import candidate_roots, root_source

    print(f"profile: {resolved}  (root from {root_source(root)})", file=sys.stderr)
    others = [
        candidate
        for candidate in candidate_roots(root)
        if candidate != resolved.parent and (candidate / resolved.name / "profile.jsonld").is_file()
    ]
    for other in others:
        print(
            f"warning: {ref!r} also exists under {other}; using {resolved} "
            f"(from {root_source(root)})",
            file=sys.stderr,
        )


def _next_steps(*pairs: tuple[str, str]) -> str:
    """A ``Next:`` block of runnable commands, each with its reason, aligned."""
    width = max(len(cmd) for cmd, _ in pairs)
    body = "\n".join(f"  {cmd:<{width}}   # {why}" for cmd, why in pairs)
    return f"Next:\n{body}"


def root_resolution_note(root: Path, *, explicit: str | None = None) -> str:
    """The "where did that directory come from" paragraph for a not-found error.

    The resolution order is omitted when ``--root`` was passed.
    """
    if explicit:
        return f"Looked in: {root} (from --root)"
    return (
        f"Looked in: {root}\n"
        "Resolution order, highest first:\n"
        "  1. --root DIR\n"
        "  2. $RESEARCHER_PROFILES_ROOT\n"
        "  3. ~/researcher-profiles"
    )


def _no_such_profile(
    *lines: str,
    root: Path,
    explicit: str | None,
    next_steps: tuple[tuple[str, str], ...],
) -> None:
    """The whole "that is not in this cache" report, on stderr.

    Names what was typed, the directory searched and how it was chosen, and a
    runnable way out. Prints nothing to stdout. The exit code is the caller's,
    since "not found" means different things per command.
    """
    for line in lines:
        print(line, file=sys.stderr)
    print(root_resolution_note(root, explicit=explicit), file=sys.stderr)
    print(_next_steps(*next_steps), file=sys.stderr)


def _resolve_profile_jsonld(arg: str, root: str | None = None) -> tuple[Path, Path]:
    """Return ``(profile_jsonld_path, profile_dir)`` from a dir, a file, a rid or a slug.

    A path on disk answers first; only a name that is nothing on disk is
    looked up in the cache. An unresolvable argument comes back as the path it
    looked like, so the caller's error names what the user typed.
    """
    p = Path(arg).expanduser()
    if p.is_dir():
        return p / "profile.jsonld", p
    if p.exists():
        return p, p.parent
    resolved = profile_dir_for_ref(arg, root)
    if resolved is not None:
        return resolved / "profile.jsonld", resolved
    return p, p.parent


def _remote_target(url_flag: str | None, token_flag: str | None) -> tuple[str | None, str | None]:
    """``(url, token)`` for a remote command: flags, then env, then the stored login."""
    from .auth.credentials import resolve_token, resolve_url

    url = resolve_url(url_flag)
    return url, resolve_token(token_flag, url)
