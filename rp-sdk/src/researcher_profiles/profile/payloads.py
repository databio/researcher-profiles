"""Turn a profile into the plain dicts that the HTTP API and the published site both serve.

These functions take a ``ResearcherProfile`` and return plain dicts matching
the Pydantic wire models in ``models/api.py``. They have exactly two callers,
and the point of the module is that both go through them: ``api/_projection.py``
validates these dicts into the wire models the HTTP routes return, and the
static publisher in ``publish/__init__.py`` renders them into ``index.html``,
``index.jsonld`` and ``SKILL.md``. That is what makes the invariant hold:
whatever a profile looks like over HTTP, it looks the same in the published
site.
"""

from typing import Any

import pydantic

from ..build_state import BuildState
from . import ResearcherProfile


def profile_summary_dict(prof: ResearcherProfile) -> dict[str, Any]:
    """Produce the dict matching ``ProfileSummary`` (GET /api/v1/profiles)."""
    md = prof.metadata
    # Corpus stats. Each is computed lazily off the loaded profile.
    # ``prof.papers`` and ``prof.summaries`` are already memoized on the
    # profile object, so repeated calls are cheap. A load failure propagates:
    # reporting a corrupt corpus as a healthy profile with zero papers is the
    # one answer nobody can act on.
    papers = prof.papers
    summaries = prof.summaries

    paper_count = len(papers)
    summary_count = sum(1 for p in papers if p.paper_id and p.paper_id in summaries)

    # "fulltext" = the paper has a downloadable / downloaded MD on disk.
    # We use the status field as the source of truth (set by whatever fetched
    # the full text). Counting ``full_text_link`` presence instead would
    # over-count: it would include papers we only have URLs for but never
    # fetched.
    # Download status and contamination are build state, so they come from
    # .build/<slug>/meta/build_state.json rather than the published paper
    # record. A profile published without its sidecar reports 0 for both,
    # which is correct: nobody served it that information.
    try:
        state = prof.build_state
    except (OSError, pydantic.ValidationError):
        state = None

    if state is None:
        fulltext_count = 0
        contaminated_count = 0
    else:
        fulltext_count = sum(1 for p in papers if state.status_of(p.paper_id) == "downloaded")
        contaminated_count = sum(1 for p in papers if state.is_contaminated(p.paper_id))
    fulltext_pct = (100.0 * fulltext_count / paper_count) if paper_count else 0.0

    return {
        "slug": prof.slug,
        "rid": getattr(prof, "rid", None),
        "name": md.name,
        "level": prof.level,
        "affiliation": md.affiliation,
        "field": md.field,
        "paper_count": paper_count,
        "summary_count": summary_count,
        "fulltext_pct": fulltext_pct,
        "contaminated_count": contaminated_count,
    }


def metadata_payload_dict(prof: ResearcherProfile) -> dict[str, Any]:
    """Project the on-disk JSON-LD document onto the (non-JSON-LD) wire shape.

    The wire contract speaks Python-ish field names, so this dumps by name
    rather than by alias; a client that wants the published JSON-LD bytes
    fetches ``/profiles/{slug}/profile.jsonld`` instead.
    """
    md = prof.metadata
    data = md.model_dump(mode="json", by_alias=False)
    data.pop("affiliation_id", None)
    data["license"] = data.pop("license_", None)
    data["expertise"] = list(md.expertise)
    data["scholar_url"] = md.scholar_url
    data["openalex_id"] = md.openalex_id
    return data


def profile_detail_dict(prof: ResearcherProfile) -> dict[str, Any]:
    """Produce the dict matching ``ProfileDetail`` (GET /api/v1/profiles/{slug})."""
    return {
        "slug": prof.slug,
        "rid": getattr(prof, "rid", None),
        "metadata": metadata_payload_dict(prof),
        "expertise": prof.expertise,
        "soul": prof.soul,
        "manifest": [m.model_dump(mode="json") for m in prof.manifest()],
    }


def paper_entries_list(
    prof: ResearcherProfile,
    *,
    build_state: BuildState | None = None,
    exclude_contaminated: bool = False,
    exclude_untitled: bool = False,
) -> list[dict[str, Any]]:
    """Produce the list matching ``PaperEntry[]`` (GET /profiles/{slug}/papers).

    When ``exclude_contaminated`` is True, papers flagged in the build state
    are silently dropped (the publisher's egress policy). The build state is
    resolved only for that branch. The API path serves the full list and
    would otherwise pay for a load it discards.
    """
    summaries = prof.summaries
    state = build_state
    if state is None and exclude_contaminated:
        try:
            state = prof.build_state
        except (OSError, pydantic.ValidationError):
            state = None

    out: list[dict[str, Any]] = []
    for p in prof.papers:
        pid = p.paper_id
        if exclude_contaminated and state and state.is_contaminated(pid):
            continue
        title = p.title
        if exclude_untitled and not (title and title.strip()):
            continue
        out.append(
            {
                "paper_id": pid,
                "title": title,
                "year": p.year,
                "journal": p.journal,
                "first_author": p.first_author,
                "full_text_link": p.full_text_link,
                "summary_available": bool(pid and pid in summaries),
            }
        )
    return out
