"""Exit codes, parser helpers, and the error reports every verb group reuses.

This is the leaf of the ``cli`` package: it imports nothing from its siblings,
so every group module can depend on it without a cycle. Only ``argparse``,
``json``, ``sys`` and ``pathlib`` are imported at module scope here, because
building the parser must not pay for anything heavier (see
``tests/test_guardrails.py``).
"""

import argparse
import sys
from pathlib import Path

#: The exit-code table, named once so the epilogs and the returns cannot drift.
#: ``EXIT_VALIDATION`` is the schema-conformance answer (``rp validate``): "the
#: artifact is wrong", as distinct from "the command was wrong" (2).
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
#: Copy-pasteable shapes beat `<placeholder>`: an agent that has never seen a
#: rid cannot tell from `<rid>` that an ORCID is accepted where a directory name
#: is, and that ambiguity is exactly what `rp where` exists for.
_EG_SLUG = "voss-elena"
_EG_ORCID = "0000-0002-1825-0097"


def _epilog(*examples: str, extra: str = "") -> str:
    """An ``Examples:`` block, optionally followed by more prose.

    Examples come before the flag list because that is the order a reader (human
    or not) actually uses them in: the first question is "what does an
    invocation of this look like", and the flag table only answers the second.
    """
    body = "Examples:\n" + "\n".join(f"  {line}" for line in examples) + "\n"
    return f"{body}\n{extra}" if extra else body


def add_subcommand(
    sub: argparse._SubParsersAction, name: str, summary: str, *examples: str, extra: str = ""
) -> argparse.ArgumentParser:
    """Register one subcommand with a description and worked examples.

    ``help`` and ``description`` are the same one-liner: the first
    appears in ``rp --help``'s command table, the second at the top of ``rp
    <cmd> --help``, and a reader who saw one and then the other should not have
    to reconcile two different sentences about the same command.
    """
    return sub.add_parser(
        name,
        help=summary,
        description=summary,
        epilog=_epilog(*examples, extra=extra),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def add_json(parser: argparse.ArgumentParser, help_text: str) -> None:
    """Register the one ``--json`` this tool has: ``store_true`` -> ``as_json``.

    Every command registers ``--json`` through this helper so the flag always
    lands on ``as_json``; a caller reading ``args.as_json`` never gets a silent
    ``False`` because one subcommand chose a different dest. Only the sentence
    saying what the JSON *is* varies per command, so that is the one parameter.
    """
    parser.add_argument("--json", action="store_true", dest="as_json", help=help_text)


def _add_root(parser: argparse.ArgumentParser, help_text: str = "Profiles root") -> None:
    """Register ``--root DIR``: the profile cache this command acts on."""
    parser.add_argument("--root", default=None, help=help_text)


def _add_token(parser: argparse.ArgumentParser) -> None:
    """Register ``--token``, which carries the same help at every site."""
    parser.add_argument("--token", default=None, help=_TOKEN_HELP)


def _add_url(parser: argparse.ArgumentParser, help_text: str) -> None:
    """Register ``--url``: the server or registry base URL to act against.

    The sentence saying which default applies varies per command, so that is
    the one parameter.
    """
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

    ``rp install`` writes to ``$RESEARCHER_PROFILES_ROOT/<slug>/`` and prints the slug,
    so a slug is the handle a caller has in hand when the next command runs.
    Reading the positional as a path only would make the one handle the tool
    just printed the one handle that does not work.

    The rule is the same one ``_resolve_profile_jsonld`` uses: a path that
    exists means itself, and only a name that is nothing on disk is looked up
    in the cache. So a local directory is never shadowed by a same-named entry
    in the cache, and a directory that exists but holds no profile still
    reaches the verb, which has a better sentence about it than this does.

    ``None`` when nothing matches, after writing the standard "not in this
    cache" report to stderr; the caller returns ``EXIT_USAGE``.
    """
    target = Path(ref).expanduser()
    if target.is_dir():
        return target
    resolved = profile_dir_for_ref(ref, root)
    if resolved is not None:
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


def _next_steps(*pairs: tuple[str, str]) -> str:
    """A ``Next:`` block of runnable commands, each with its reason.

    The commands carry values the caller typed, so their widths vary and
    hand-aligned comments come out ragged. Aligning here keeps the block
    scannable no matter what the slug was.
    """
    width = max(len(cmd) for cmd, _ in pairs)
    body = "\n".join(f"  {cmd:<{width}}   # {why}" for cmd, why in pairs)
    return f"Next:\n{body}"


def root_resolution_note(root: Path, *, explicit: str | None = None) -> str:
    """The "where did that directory come from" paragraph, written once.

    Every command that reads the cache resolves it through the same three-step
    order, so a "not found" that does not name the directory it looked in, and
    how that directory was chosen, sends the reader hunting for a setting they
    may not know exists. This is the second half of every such error.

    When ``--root`` was passed there is nothing to explain, so the order is
    omitted: repeating it would be telling somebody who typed the path where
    the path came from.
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

    Four commands answer the same question badly in four places: name what was
    typed, name the directory searched, say how that directory was chosen, and
    offer a runnable way out. Only the first and last vary per command, so
    those are the parameters. Prints nothing to stdout, so `--json` stays a
    clean document, and returns nothing. The exit code stays the caller's,
    because the same "not found" is usage error for `where`/`seek`, a
    non-`done` state for `status`, and not an error at all for `list`.
    """
    for line in lines:
        print(line, file=sys.stderr)
    print(root_resolution_note(root, explicit=explicit), file=sys.stderr)
    print(_next_steps(*next_steps), file=sys.stderr)


def _resolve_profile_jsonld(arg: str, root: str | None = None) -> tuple[Path, Path]:
    """Return ``(profile_jsonld_path, profile_dir)`` from a dir, a file, a rid or a slug.

    A path on disk answers first, so a local directory or an explicit
    ``.../profile.jsonld`` always means itself. Only when the argument names
    nothing on disk is it looked up in the cache, which is where ``rp install``
    leaves a profile under its slug. An unresolvable argument comes back as the
    path it looked like, so the caller's "no profile.jsonld at ..." error names
    what the user typed.
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
    """``(url, token)`` for a remote command: flags, then env, then the stored login.

    Lives here rather than in a verb group because ``rp push`` and ``rp whoami``
    are in different groups and must resolve a server the same way.
    """
    from .auth.credentials import resolve_token, resolve_url

    url = resolve_url(url_flag)
    return url, resolve_token(token_flag, url)
