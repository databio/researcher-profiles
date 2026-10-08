"""Passages: the parts of one paper, or one profile, that answer a query.

On ``public_router``, not behind the ``match`` scope, for three reasons: they
search inside one profile the caller can already read; every snippet they
return is text the caller can already fetch through the content, summary and
text routes; and they never rank one profile against another. Cross-profile
matching stays on the ``match`` scope.
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

    Gated like the paper list: the profile gate, then the works artifact
    (``sources/papers.jsonld``); a paper this caller cannot see is the same
    404 as one that does not exist. Full text is searched by keyword only and
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
