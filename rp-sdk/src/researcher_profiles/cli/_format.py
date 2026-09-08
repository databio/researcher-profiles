"""The format verbs: ``schema``, ``validate``, ``manifest``, ``where``, ``mint-local-id``.

These are the commands that answer questions about the on-disk contract rather
than about a corpus or a server: what the schema is, whether a directory passes
it, what its manifest says, and which directory an identifier resolves to.
"""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from ._shared import (
    _EG_ORCID,
    _EG_SLUG,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    _add_root,
    _no_such_profile,
    add_json,
    add_subcommand,
    resolve_profile_arg,
)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register this group's verbs on the root subparser action."""
    p_schema = add_subcommand(
        sub,
        "schema",
        "Export JSON Schema files for the profile format",
        "rp schema export schemas/",
        "rp schema export-wire rp-ui-lib/schemas/wire.schema.json",
    )
    schema_sub = p_schema.add_subparsers(dest="schema_cmd", required=True, metavar="<subcommand>")
    s_export = add_subcommand(
        schema_sub,
        "export",
        "Write <model>.schema.json files",
        "rp schema export schemas/",
    )
    s_export.add_argument("out_dir", help="Directory to write schema files into (e.g. schemas/)")
    s_wire = add_subcommand(
        schema_sub,
        "export-wire",
        "Write the combined HTTP wire-contract JSON Schema (for the TS browser)",
        "rp schema export-wire rp-ui-lib/schemas/wire.schema.json",
    )
    s_wire.add_argument(
        "out_file",
        help="Output file for the combined wire schema (e.g. rp-ui-lib/schemas/wire.schema.json)",
    )

    p_validate = add_subcommand(
        sub,
        "validate",
        "Validate a profile directory against the JSON Schema spec",
        f"rp validate {_EG_SLUG}",
        "rp validate ./profiles/voss-elena --json",
        extra="Exit: 0 when conformant, 4 on a conformance violation.\n",
    )
    p_validate.add_argument("profile", help="A profile directory, a rid, or a slug")
    _add_root(p_validate, "Profiles root (for a rid/slug)")
    add_json(p_validate, "Output as JSON instead of text")

    p_manifest = add_subcommand(
        sub,
        "manifest",
        "Show or regenerate a profile's manifest (hasPart/subjectOf)",
        f"rp manifest {_EG_SLUG}",
        f"rp manifest {_EG_SLUG} --check",
        "rp manifest ./profiles/voss-elena --write",
        extra="Exit: with --check, 4 when the manifest disagrees with the tree.\n",
    )
    p_manifest.add_argument("profile", help="A profile directory, a rid, or a slug")
    _add_root(p_manifest, "Profiles root (for a rid/slug)")
    p_manifest.add_argument(
        "--write", action="store_true", help="Write the regenerated manifest back"
    )
    p_manifest.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when the recorded manifest disagrees with the directory",
    )
    add_json(p_manifest, "Emit the manifest entries and any drift as JSON")

    p_where = add_subcommand(
        sub,
        "where",
        "Print the profile directory for a rid or a slug",
        f"rp where {_EG_SLUG}",
        f"rp where {_EG_ORCID}",
        f"rp where {_EG_SLUG} --json",
        extra=(
            "A directory name is a display handle, never an identity. `where`\n"
            "is how you go from either one to a path.\n"
        ),
    )
    p_where.add_argument("ref", help="A rid (ORCID or local:...) or a directory slug")
    _add_root(p_where)
    add_json(p_where, "Emit {ref, path, root} as JSON")

    p_mint = add_subcommand(
        sub,
        "mint-local-id",
        "Mint a local: rid for a researcher who has no ORCID",
        'rp mint-local-id --name "Elena Voss"',
    )
    p_mint.add_argument("--name", required=True, help="The researcher's name")


def _cmd_schema(args: argparse.Namespace) -> int:
    """Export JSON Schema files for the on-disk profile format."""
    if args.schema_cmd == "export":
        from ..schema_export import export_schemas

        written = export_schemas(args.out_dir)
        for pth in written:
            print(f"wrote {pth}")
        return EXIT_OK
    if args.schema_cmd == "export-wire":
        from ..schema_export import export_wire_schema

        pth = export_wire_schema(args.out_file)
        print(f"wrote {pth}")
        return EXIT_OK
    return EXIT_USAGE


def _cmd_validate(args: argparse.Namespace) -> int:
    """Validate a profile directory against the JSON Schema spec."""
    from ..validate import (
        format_text_report,
        report_to_dict,
        validate_profile_dir,
    )

    target = resolve_profile_arg("validate", args.profile, args.root)
    if target is None:
        return EXIT_USAGE
    report = validate_profile_dir(target)
    if args.as_json:
        print(json.dumps(report_to_dict(report), indent=2))
    else:
        print(format_text_report(report))
    # Exit 0 on ok, EXIT_VALIDATION on a conformance violation: the code
    # that says "the artifact is wrong" rather than "the command was".
    # Undeclared-terms-only is a pass with a warning banner.
    if report.ok:
        has_undeclared = any(a.undeclared for a in report.artifacts)
        if has_undeclared:
            print(
                "\nwarning: undeclared terms found (not a conformance failure)",
                file=sys.stderr,
            )
        return EXIT_OK
    print(
        f"Profile does not conform. Re-read the violations above, or see "
        f"them keyed by JSON pointer:\n"
        f"  rp validate {args.profile} --json",
        file=sys.stderr,
    )
    return EXIT_VALIDATION


def _cmd_manifest(args: argparse.Namespace) -> int:
    """Show, regenerate, or drift-check a profile's manifest."""
    from ..errors import ProfileLoadError
    from ..profile import ResearcherProfile
    from ..schema.manifest import manifest_drift

    target = resolve_profile_arg("manifest", args.profile, args.root)
    if target is None:
        return EXIT_USAGE
    try:
        prof = ResearcherProfile.from_files(target)
    except (ProfileLoadError, FileNotFoundError, NotADirectoryError) as e:
        print(f"rp manifest: cannot read {args.profile}: {e}", file=sys.stderr)
        print(
            "A manifest is derived from a loaded profile, so the directory "
            "has to hold a readable profile.jsonld.\n"
            "Resolve a slug or a rid to its directory first:\n"
            f"  rp where {args.profile}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    root = prof.require_directory("rp manifest")
    if args.write:
        parts = prof.build_manifest(write=True)
        target = root / "profile.jsonld"
        if args.as_json:
            print(
                json.dumps(
                    {"profile": str(root), "written": len(parts), "path": str(target)},
                    indent=2,
                )
            )
        else:
            print(f"wrote {len(parts)} manifest entries to {target}")
        return EXIT_OK
    entries = prof.manifest()
    drift = manifest_drift(root, entries)
    if args.as_json:
        print(
            json.dumps(
                {
                    "profile": str(root),
                    "entries": [{"role": e.role, "content_url": e.content_url} for e in entries],
                    "drift": {
                        "missing": list(drift["missing"]),
                        "stale": list(drift["stale"]),
                    },
                },
                indent=2,
            )
        )
    else:
        for entry in entries:
            print(f"{entry.role or '-':<16} {entry.content_url}")
    if drift["missing"] or drift["stale"]:
        # Always to stderr, including under --json: drift is a diagnosis
        # about the document, and mixing it into stdout would break `rp
        # manifest --json | jq`.
        for m in drift["missing"]:
            print(f"MISSING from manifest: {m}", file=sys.stderr)
        for m in drift["stale"]:
            print(f"STALE in manifest (no such file): {m}", file=sys.stderr)
        if args.check:
            print(
                "The recorded manifest disagrees with the directory. Fix it "
                "in place:\n"
                f"  rp manifest {args.profile} --write",
                file=sys.stderr,
            )
            return EXIT_VALIDATION
        return EXIT_OK
    return EXIT_OK


def _cmd_where(args: argparse.Namespace) -> int:
    """Print the profile directory a rid or slug resolves to."""
    from ..store import ProfileNotFoundError
    from ..store.config import resolve_profiles_root
    from ..store.files import FilesystemProfileStore

    root = resolve_profiles_root(args.root)
    try:
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            raise FileNotFoundError(f"profiles root does not exist: {root_path}")
        path = FilesystemProfileStore(root_path).path_for(args.ref)
    except (FileNotFoundError, ProfileNotFoundError, ValueError) as e:
        # Name the ref, the directory searched, and how that directory was
        # chosen. "not found: KeyError('x')" is true and useless: the usual
        # cause is a cache resolved from a setting the caller forgot about.
        lines = [f"rp where: nothing matches {args.ref!r}"]
        # A bare KeyError stringifies to the ref already printed above; only the
        # cases that say something new (a missing root, an unreadable
        # index) earn a second line.
        detail = str(e).strip("'\"")
        if detail and detail != args.ref:
            lines.append(f"  {e}")
        _no_such_profile(
            *lines,
            root=root,
            explicit=args.root,
            next_steps=(
                ("rp list", "what this cache actually holds"),
                (f"rp install {args.ref}", "pull it from a registry"),
            ),
        )
        return EXIT_USAGE
    if args.as_json:
        print(json.dumps({"ref": args.ref, "path": str(path), "root": str(root)}, indent=2))
    else:
        print(path)
    return EXIT_OK


def _cmd_mint_local_id(args: argparse.Namespace) -> int:
    """Mint and print a ``local:`` rid for a researcher with no ORCID."""
    from ..schema import mint_local_rid

    print(mint_local_rid(args.name))
    return EXIT_OK


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "schema": _cmd_schema,
    "validate": _cmd_validate,
    "manifest": _cmd_manifest,
    "where": _cmd_where,
    "mint-local-id": _cmd_mint_local_id,
}
