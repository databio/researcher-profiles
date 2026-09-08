"""``rp index`` / ``export-embeddings`` / ``export`` / ``search`` / ``rank-works``.

The verbs that read one profile's corpus: build its embedding index, write the
servable flat form, render it as a knowledge-base blob, query it, and rank new
candidate works against it.
"""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from ._shared import (
    _EG_SLUG,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION,
    _add_root,
    add_json,
    add_subcommand,
    resolve_profile_arg,
)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register this group's verbs on the root subparser action."""
    p_index = add_subcommand(
        sub,
        "index",
        "Build the embedding index for a profile",
        f"rp index {_EG_SLUG}",
        f"rp index {_EG_SLUG} --force",
        "rp index ./profiles/voss-elena --backend st:all-MiniLM-L6-v2",
    )
    p_index.add_argument("profile", help="A profile directory, a rid, or a slug")
    _add_root(p_index, "Profiles root (for a rid/slug)")
    p_index.add_argument("--force", action="store_true", help="Drop and rebuild")
    p_index.add_argument("--backend", default=None, help="Backend spec, e.g. st:all-MiniLM-L6-v2")

    p_export = add_subcommand(
        sub,
        "export-embeddings",
        "Write the servable flat embedding form (embeddings/) from the sqlite index",
        f"rp export-embeddings {_EG_SLUG}",
        "rp export-embeddings ./profiles/voss-elena",
    )
    p_export.add_argument("profile", help="A profile directory, a rid, or a slug")
    _add_root(p_export, "Profiles root (for a rid/slug)")

    p_kb = add_subcommand(
        sub,
        "export",
        "Render a profile as one text blob (plus metadata) for a knowledge base",
        f"rp export {_EG_SLUG}",
        f"rp export {_EG_SLUG} --json",
        f"rp export {_EG_SLUG} --json -o {_EG_SLUG}.export.json",
        extra=(
            "Prints the rendered markdown, or with --json the whole export bundle\n"
            "(identity, links, the corpus DOI list, the text, and a content hash a\n"
            "knowledge base can upsert on).\n\n"
            "Exit: 0 ok, 1 the profile could not be loaded, 4 the profile is not\n"
            "public and --allow-nonpublic was not given.\n"
        ),
    )
    p_kb.add_argument("profile", help="A profile directory, a rid, or a slug")
    _add_root(p_kb, "Profiles root (for a rid/slug)")
    add_json(p_kb, "Emit the export bundle as JSON")
    p_kb.add_argument("-o", "--out", default=None, metavar="FILE", help="Write instead of print")
    p_kb.add_argument(
        "--max-papers", type=int, default=None, metavar="N", help="Cap papers in the blob"
    )
    p_kb.add_argument(
        "--char-budget", type=int, default=None, metavar="N", help="Soft cap on blob characters"
    )
    p_kb.add_argument("--no-soul", action="store_true", help="Omit personality/SOUL.md")
    p_kb.add_argument(
        "--no-expertise-doc", action="store_true", help="Omit personality/expertise.md"
    )
    p_kb.add_argument(
        "--explore-base",
        default=None,
        metavar="URL",
        help="Base URL of a profile browser app (default: none, so the bundle carries no explore_url)",
    )
    p_kb.add_argument(
        "--profile-url", default=None, metavar="URL", help="Override the published profile URL"
    )
    p_kb.add_argument(
        "--allow-nonpublic",
        action="store_true",
        help="Export a profile whose visibility is above public",
    )

    p_search = add_subcommand(
        sub,
        "search",
        "Search one profile's embedding index",
        f'rp search {_EG_SLUG} "chromatin accessibility"',
        'rp search ./profiles/voss-elena "single cell" -k 10',
        'rp search ./profiles/voss-elena "methods" --type paper_summary --json',
    )
    p_search.add_argument("profile", help="A profile directory, a rid, or a slug")
    p_search.add_argument("query", help="Free-text query")
    _add_root(p_search, "Profiles root (for a rid/slug)")
    p_search.add_argument(
        "-k", type=int, default=5, metavar="N", help="Hits to return (default: 5)"
    )
    p_search.add_argument(
        "--type",
        default=None,
        metavar="TYPE",
        help=(
            "Filter on source_type: paper_summary, paper_abstract, expertise, "
            "soul, grant, cv, or web"
        ),
    )
    add_json(p_search, "Emit the hits as a JSON array")

    p_rank_works = add_subcommand(
        sub,
        "rank-works",
        "Rank new candidate works against a profile's embedding vector",
        f"rp rank-works {_EG_SLUG} --openalex --since 2026-07-01",
        f"rp rank-works {_EG_SLUG} --works candidates.json -k 10 --json",
        extra=(
            "Candidates come from --openalex (fetched live, seeded by the\n"
            "profile's subfields/interests and citation neighborhood) or from\n"
            "--works FILE (a JSON array of raw OpenAlex works or PaperRecord\n"
            "dicts). Needs a built embedding index (rp index) and the vectors\n"
            "extra; --openalex additionally needs the client extra (httpx).\n"
        ),
    )
    p_rank_works.add_argument("profile", help="A profile directory, a rid, or a slug")
    _add_root(p_rank_works, "Profiles root (for a rid/slug)")
    p_rank_works.add_argument(
        "--since",
        default=None,
        metavar="YYYY-MM-DD",
        help="Earliest publication date (default: 30 days back)",
    )
    p_rank_works.add_argument(
        "--openalex", action="store_true", help="Fetch candidates from the OpenAlex API"
    )
    p_rank_works.add_argument(
        "--works",
        default=None,
        metavar="FILE",
        help="JSON array of candidate works (raw OpenAlex works or PaperRecord dicts)",
    )
    p_rank_works.add_argument(
        "-k", type=int, default=10, metavar="N", help="Works to return (default: 10)"
    )
    p_rank_works.add_argument(
        "--kind",
        default="centroid",
        choices=("centroid", "summary", "expertise"),
        help="Which profile vector to rank against (default: centroid)",
    )
    p_rank_works.add_argument(
        "--threshold", type=float, default=None, metavar="X", help="Drop works scoring below X"
    )
    p_rank_works.add_argument(
        "--mailto", default=None, metavar="EMAIL", help="OpenAlex polite-pool contact"
    )
    p_rank_works.add_argument(
        "--exclude-types",
        default=None,
        metavar="T1,T2",
        help=(
            "Comma-separated OpenAlex work types to drop from --openalex "
            "candidates (default: non-paper deposit types, software, book, etc.)"
        ),
    )
    p_rank_works.add_argument(
        "--all-types",
        action="store_true",
        help="Keep every work type (disable the deposit-type filter)",
    )
    add_json(p_rank_works, "Emit the ranked works as a JSON array")


def _cmd_index(args: argparse.Namespace) -> int:
    """Build or update a profile's embedding index."""
    from ..embeddings import build_index
    from ..profile import ResearcherProfile

    target = resolve_profile_arg("index", args.profile, args.root)
    if target is None:
        return EXIT_USAGE
    profile = ResearcherProfile.from_files(target)
    try:
        report = build_index(profile, force=args.force, backend=args.backend)
    except ValueError as e:
        # `--backend` is free text, so a typo lands here rather than in
        # argparse. Name the accepted spellings: the error alone says what
        # is wrong and not what to write instead.
        print(f"rp index: {e}", file=sys.stderr)
        print(
            "A backend spec is 'kind:model', where kind is one of:\n"
            "  st:<model>      sentence-transformers, run locally\n"
            "  fastembed:<model>  ONNX via fastembed, run locally, no torch\n"
            "  openai:<model>  OpenAI embeddings\n"
            "  voyage:<model>  Voyage embeddings\n"
            "  rp index <profile> --backend st:all-MiniLM-L6-v2",
            file=sys.stderr,
        )
        return EXIT_USAGE
    print(
        f"backend={report.backend_name} added={report.added} updated={report.updated} "
        f"skipped={report.skipped} removed={report.removed} duration={report.duration_s}s"
    )
    return EXIT_OK


def _cmd_export_embeddings(args: argparse.Namespace) -> int:
    """Write the servable flat embedding form from the sqlite index."""
    from ..embeddings import write_flat_export

    target = resolve_profile_arg("export-embeddings", args.profile, args.root)
    if target is None:
        return EXIT_USAGE
    result = write_flat_export(target)
    if result is None:
        print("no flat form written (no sqlite index, or no public rows survive the tier rule)")
        return EXIT_OK
    print(
        f"wrote {result.index_path} backend={result.backend_spec} "
        f"count={result.count} dim={result.dim} dropped={result.dropped}"
    )
    return EXIT_OK


def _cmd_export(args: argparse.Namespace) -> int:
    """Render a profile as one text blob (or a JSON bundle) for a knowledge base."""
    from ..errors import ProfileError
    from ..profile import ResearcherProfile
    from ..profile.export import (
        ExportOptions,
        ExportVisibilityError,
        build_export_bundle,
        render_export_text,
    )
    from ..schema.jsonld import canonical_dumps

    target = resolve_profile_arg("export", args.profile, args.root)
    if target is None:
        return EXIT_USAGE
    try:
        profile = ResearcherProfile.from_files(target)
    except (OSError, ProfileError) as e:
        print(f"rp export: {e}", file=sys.stderr)
        return EXIT_ERROR

    fields: dict[str, object] = {}
    if args.max_papers is not None:
        fields["max_papers"] = args.max_papers
    if args.char_budget is not None:
        fields["char_budget"] = args.char_budget
    if args.no_soul:
        fields["include_soul"] = False
    if args.no_expertise_doc:
        fields["include_expertise_doc"] = False
    if args.explore_base is not None:
        fields["explore_base"] = args.explore_base
    if args.profile_url is not None:
        fields["profile_url"] = args.profile_url
    if args.allow_nonpublic:
        fields["allow_nonpublic"] = True
    options = ExportOptions(**fields)

    try:
        if args.as_json:
            bundle = build_export_bundle(profile, options)
            payload = canonical_dumps(bundle.model_dump(mode="json"))
        else:
            payload = render_export_text(profile, options)
    except ExportVisibilityError as e:
        # A contract refusal, not a crash: the profile said it is not public.
        print(f"rp export: {e}", file=sys.stderr)
        print(
            "Pass --allow-nonpublic when the destination is authorized to "
            "hold this profile's tier.",
            file=sys.stderr,
        )
        return EXIT_VALIDATION

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
        print(f"wrote {out_path}", file=sys.stderr)
        return EXIT_OK
    print(payload, end="" if payload.endswith("\n") else "\n")
    return EXIT_OK


def _cmd_search(args: argparse.Namespace) -> int:
    """Query one profile's embedding index."""
    from ..embeddings import IndexNotBuiltError, SqliteEmbeddingIndex

    target = resolve_profile_arg("search", args.profile, args.root)
    if target is None:
        return EXIT_USAGE
    idx = SqliteEmbeddingIndex(target)
    filt = {"source_type": args.type} if args.type else None
    try:
        hits = idx.search(args.query, k=args.k, filter=filt)
    except IndexNotBuiltError as e:
        print(f"rp search: {e}", file=sys.stderr)
        print(
            f"Build one first:\n  rp index {args.profile}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.as_json:
        print(
            json.dumps(
                [
                    {
                        "score": h.score,
                        "source_type": h.source_type,
                        "source_id": h.source_id,
                        "chunk_index": h.chunk_index,
                        "section": h.section,
                        "text": h.text,
                    }
                    for h in hits
                ],
                indent=2,
            )
        )
        return EXIT_OK
    for h in hits:
        print(f"[{h.score:.3f}] {h.source_type}:{h.source_id}#{h.chunk_index}  {h.section or ''}")
        preview = h.text.replace("\n", " ")
        print(f"   {preview[:200]}")
    return EXIT_OK


class _CliError(Exception):
    """A message already written for stderr, plus the exit code to return.

    Lets the ``rank-works`` phases fail from inside a helper without every one
    of them returning ``list | int`` and every caller re-checking the type.
    """

    def __init__(self, code: int, message: str, *hints: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = hints

    def report(self) -> int:
        """Print the message and any hints to stderr, and return the exit code.

        An empty message means the caller already wrote its own report (as
        ``resolve_profile_arg`` does), so printing here would add a blank line
        to a finished error.
        """
        if self.message:
            print(self.message, file=sys.stderr)
        for hint in self.hints:
            print(hint, file=sys.stderr)
        return self.code


def _rank_works_profile(args: argparse.Namespace):
    """Load the profile named by a directory, a rid, or a slug."""
    from ..errors import ProfileError
    from ..profile import ResearcherProfile

    target = resolve_profile_arg("rank-works", args.profile, args.root)
    if target is None:
        raise _CliError(EXIT_USAGE, "")
    try:
        return ResearcherProfile.from_files(target), target
    except (OSError, ProfileError) as e:
        raise _CliError(EXIT_ERROR, f"rp rank-works: {e}") from e


def _works_from_file(path: str) -> list:
    """Parse ``--works FILE``: raw OpenAlex works, PaperRecord dicts, or both."""
    from ..openalex import parse_work
    from ..schema import PaperRecord

    try:
        raw_list = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise _CliError(EXIT_USAGE, f"rp rank-works: cannot read {path}: {e}") from e
    works = []
    for d in raw_list:
        # A raw OpenAlex work carries fields a PaperRecord never does.
        if "authorships" in d or "abstract_inverted_index" in d:
            rec = parse_work(d)
            if rec is not None:
                works.append(rec)
        else:
            works.append(PaperRecord.model_validate(d))
    return works


def _works_from_openalex(args: argparse.Namespace, prof) -> list:
    """Fetch ``--openalex`` candidates, seeded from the profile's own topics."""
    from datetime import date, timedelta

    from ..openalex import fetch_new_works, profile_query_terms

    since = args.since or (date.today() - timedelta(days=30)).isoformat()
    terms = profile_query_terms(prof)
    if not terms["topics"] and not terms["seed_work_ids"]:
        raise _CliError(
            EXIT_ERROR,
            "rp rank-works: this profile has no subfields/interests or OpenAlex "
            "work ids to query with",
        )
    if args.all_types:
        exclude_types: set[str] | None = set()
    elif args.exclude_types:
        exclude_types = {t.strip() for t in args.exclude_types.split(",") if t.strip()}
    else:
        exclude_types = None  # fetch_new_works applies its default set
    try:
        return fetch_new_works(
            since=since,
            topics=terms["topics"],
            seed_work_ids=terms["seed_work_ids"],
            mailto=args.mailto,
            exclude_types=exclude_types,
        )
    except ImportError as e:
        raise _CliError(EXIT_ERROR, f"rp rank-works: {e}") from e
    # Boundary: a live third-party HTTP API. Anything it raises, including a
    # transport error from whichever client is installed, is reported as a
    # failed fetch rather than a traceback.
    except Exception as e:
        raise _CliError(EXIT_ERROR, f"rp rank-works: OpenAlex fetch failed: {e}") from e


def _rank_works_candidates(args: argparse.Namespace, prof) -> list:
    """Candidate works from ``--works`` or ``--openalex``, whichever was named."""
    if args.works:
        return _works_from_file(args.works)
    if args.openalex:
        return _works_from_openalex(args, prof)
    raise _CliError(
        EXIT_USAGE,
        "rp rank-works: pass --openalex to fetch candidates, or --works FILE to rank a local list",
    )


def _render_ranked_works(ranked, args: argparse.Namespace) -> int:
    """Print the ranked works as JSON or as one scored line each."""
    if args.as_json:
        print(json.dumps([r.to_dict() for r in ranked], indent=2))
        return EXIT_OK
    if not ranked:
        print("no candidate works survived ranking", file=sys.stderr)
        return EXIT_OK
    for r in ranked:
        w = r.work
        bits = [b for b in (w.journal, w.doi or w.openalex_id) if b]
        suffix = f"  ({'; '.join(bits)})" if bits else ""
        print(f"[{r.score:.3f}] {w.year or '----'}  {w.title}{suffix}")
        if r.evidence:
            print(f"   topics: {', '.join(r.evidence)}")
    return EXIT_OK


def _cmd_rank_works(args: argparse.Namespace) -> int:
    """Rank candidate works against a profile's embedding vector."""
    try:
        prof, target = _rank_works_profile(args)
        works = _rank_works_candidates(args, prof)
    except _CliError as e:
        return e.report()

    try:
        from ..embeddings.rank import rank_works_against_profile
    except ImportError:
        print(
            "rp rank-works: ranking requires the 'vectors' and 'st' extras; "
            "see the install instructions in the README.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    from ..embeddings import IndexNotBuiltError

    try:
        ranked = rank_works_against_profile(
            prof,
            works,
            k=args.k,
            kind=args.kind,
            threshold=args.threshold,
        )
    except IndexNotBuiltError as e:
        print(f"rp rank-works: {e}", file=sys.stderr)
        print(f"Build one first:\n  rp index {target}", file=sys.stderr)
        return EXIT_USAGE
    return _render_ranked_works(ranked, args)


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "index": _cmd_index,
    "export-embeddings": _cmd_export_embeddings,
    "export": _cmd_export,
    "search": _cmd_search,
    "rank-works": _cmd_rank_works,
}
