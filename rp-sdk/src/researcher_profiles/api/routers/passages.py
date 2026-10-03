"""Passages: the parts of one paper, or one profile, that answer a query.

On ``public_router``, not behind the ``match`` scope, for three reasons: they
search inside one profile the caller can already read; every snippet they
return is text the caller can already fetch through the content, summary and
text routes; and they never rank one profile against another. Cross-profile
matching stays on the ``match`` scope.
"""

from fastapi import Depends, HTTPException, Request

from ...models.api import PassageList, PassageRequest
from ...privacy import ViewerTier, explain_tiers
from ...store import ProfileStore
from .._passages import paper_passages, profile_passages
from .._projection import _gate_profile, _profile_missing, artifact_visible
from ..deps import get_profile, get_store, get_viewer_tier
from ._routers import public_router


@public_router.post(
    "/profiles/{slug}/papers/{paper_id}/passages",
    response_model=PassageList,
    response_model_exclude_none=True,
)
def find_paper_passages(
    slug: str,
    paper_id: str,
    body: PassageRequest,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> PassageList:
    """The passages of one paper (full text, summary, abstract) that best answer ``query``.

    Gated like the paper list: the profile gate, then the works artifact
    (``sources/papers.jsonld``); a paper this caller cannot see is the same
    404 as one that does not exist. Full text is searched by keyword only and
    only when this caller may read it; ``searched`` and ``note`` say so.
    ``k`` defaults to 3 and is clamped to 10.
    """
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    if not artifact_visible(
        explain_tiers(prof.metadata), prof.metadata, "sources/papers.jsonld", "works", viewer
    ):
        raise _profile_missing(slug)
    record = next((p for p in prof.papers if p.paper_id == paper_id), None)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no paper {paper_id!r} in profile {slug!r}")
    return paper_passages(request, store, prof, viewer, record, body.query, body.k)


@public_router.post(
    "/profiles/{slug}/passages",
    response_model=PassageList,
    response_model_exclude_none=True,
)
def find_profile_passages(
    slug: str,
    body: PassageRequest,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> PassageList:
    """The passages of one profile (narrative, CV, web pages, grants) that answer ``query``.

    Only sources this caller may read are searched. A source with no stored
    vectors (on the SQL store, every private one) is searched by keyword only,
    and ``note`` names it. ``k`` defaults to 3 and is clamped to 10.
    """
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    return profile_passages(request, store, prof, viewer, body.query, body.k)
