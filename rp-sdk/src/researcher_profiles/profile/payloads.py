"""Turn a profile into the plain dicts that the HTTP API and the published site both serve.

The dicts match the wire models in ``models/api.py``. Both the API and the
static publisher go through these functions, so a profile looks the same over
HTTP and on the published site.
"""

from collections.abc import Sequence
from typing import Any

import pydantic

from ..build_state import BuildState
from ..privacy import ViewerTier, project_document
from ..schema import strip_registry_issued_proofs
from . import ResearcherProfile


def profile_summary_dict(prof: ResearcherProfile, viewer: ViewerTier) -> dict[str, Any]:
    """Produce the dict matching ``ProfileSummary`` (GET /api/v1/profiles).

    ``viewer`` has no default: a summary carries inline document fields, so a
    forgotten tier would publish them at every tier.
    """
    md = project_document(prof.metadata, viewer)
    # A load failure propagates: a corrupt corpus must not look like a healthy
    # profile with zero papers.
    papers = prof.papers
    summaries = prof.summaries

    paper_count = len(papers)
    summary_count = sum(1 for p in papers if p.paper_id and p.paper_id in summaries)

    # "fulltext" counts downloaded papers, not ``full_text_link`` presence,
    # which would include links never fetched. Download status and
    # contamination are build state; without the sidecar both report 0.
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
        # From the projected document, so a withheld Clinical section never
        # shows through as true.
        "clinical": bool(md.therapeutic_areas),
    }


def metadata_payload_dict(
    prof: ResearcherProfile, viewer: ViewerTier, *, proofs: Sequence[Any] = ()
) -> dict[str, Any]:
    """Project the on-disk JSON-LD document onto the (non-JSON-LD) wire shape.

    Dumps by field name, not alias; the JSON-LD bytes are at
    ``/profiles/{slug}/profile.jsonld``.

    ``viewer`` is required on purpose: a default would make a forgotten tier
    silently expose the whole document.

    ``proofs`` are the registry-issued proofs the serving registry attaches.
    Any registry-issued proof in the stored document is dropped first: only
    the registry computes those.
    """
    md = project_document(prof.metadata, viewer)
    data = md.model_dump(mode="json", by_alias=False)
    data["proof"] = strip_registry_issued_proofs(data.get("proof") or []) + [
        p.model_dump(mode="json", by_alias=False) for p in proofs
    ]
    data.pop("affiliation_id", None)
    data["license"] = data.pop("license_", None)
    data["expertise"] = list(md.expertise)
    data["scholar_url"] = md.scholar_url
    data["openalex_id"] = md.openalex_id
    return data


def profile_detail_dict(prof: ResearcherProfile, viewer: ViewerTier) -> dict[str, Any]:
    """Produce the dict matching ``ProfileDetail``: the static ``profile.json`` view."""
    return {
        "slug": prof.slug,
        "rid": getattr(prof, "rid", None),
        "metadata": metadata_payload_dict(prof, viewer),
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
    """Produce the list matching ``PaperEntry[]``: the static ``papers.json`` view.

    When ``exclude_contaminated`` is True, papers flagged in the build state
    are dropped (the publisher's egress policy).
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
