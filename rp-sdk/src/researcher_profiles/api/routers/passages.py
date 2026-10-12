"""Passages: the parts of one paper, or one profile, that answer a query.

Not behind the ``match`` scope: they search one readable profile, return only
text the caller could already fetch, and never rank profiles against each other.
"""

from fastapi import Depends

from ...models.api import PassageList, PassageRequest
from .. import service as svc
from ..caller import Caller
from ..deps import get_read_caller, get_service
from ..service import Service
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
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> PassageList:
    """The passages of one paper (full text, summary, abstract) that best answer ``query``.

    Gated like the paper list; a hidden paper is the same 404 as a missing one.
    Full text is searched by keyword only and
    only when this caller may read it; ``searched`` and ``note`` say so.
    ``k`` defaults to 3 and is clamped to 10.
    """
    return svc.find_paper_passages(service, caller, slug, paper_id, body.query, body.k)


@public_router.post(
    "/profiles/{slug}/passages",
    response_model=PassageList,
    response_model_exclude_none=True,
)
def find_profile_passages(
    slug: str,
    body: PassageRequest,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> PassageList:
    """The passages of one profile (narrative, CV, web pages, grants) that answer ``query``.

    Only sources this caller may read are searched. A source with no stored
    vectors (on the SQL store, every private one) is searched by keyword only,
    and ``note`` names it. ``k`` defaults to 3 and is clamped to 10.
    """
    return svc.find_profile_passages(service, caller, slug, body.query, body.k)
