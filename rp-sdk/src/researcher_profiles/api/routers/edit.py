"""The owner-scoped edit surface: metadata (narrative included), works, visibility.

Each route is a thin adapter over :mod:`researcher_profiles.api.service`, which
runs the edit gate, the version check and the write.
"""

import logging
from typing import Optional

from fastapi import Depends

from ...models.api import (
    EditResult,
    MetadataPatch,
    VisibilityPatch,
    VisibilityReport,
    WorkPatch,
)
from .. import service as svc
from ..caller import Caller
from ..deps import get_caller, get_service
from ..service import Service
from ._routers import edit_router

logger = logging.getLogger(__name__)


@edit_router.patch("/profiles/{slug}/metadata", response_model=EditResult)
def patch_profile_metadata(
    slug: str,
    body: MetadataPatch,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_caller),
) -> EditResult:
    """Patch owner-editable metadata, the narrative included: every content field.

    Only fields present in the body are applied, in one atomic write; ``soul``
    replaces ``personality/SOUL.md``. The editable set is
    :data:`researcher_profiles.profile.edit.EDITABLE_METADATA_FIELDS`. A patch
    that would make an invalid document is a 400 and changes nothing.

    Send ``base_hash`` (the ``content_hash`` the edit was composed against) to
    get a 409 instead of overwriting a concurrent write; without it, last
    writer wins.
    """
    patch = body.model_dump(exclude_unset=True, by_alias=False)
    base_hash = patch.pop("base_hash", None)
    return svc.edit_metadata(service, caller, slug, patch, base_hash=base_hash)


@edit_router.patch("/profiles/{slug}/works/{paper_id}", response_model=EditResult)
def patch_profile_work(
    slug: str,
    paper_id: str,
    body: WorkPatch,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_caller),
) -> EditResult:
    """Patch owner-editable fields of one work in ``sources/papers.jsonld``.

    Only fields in :data:`researcher_profiles.profile.edit.EDITABLE_WORK_FIELDS`
    are applied. An invalid result is a 400 and changes nothing.

    A stale ``base_version`` (the paper's ``version``) is a 409 carrying the
    current version in ``X-RP-Paper-Version``. The reply's ``version`` is the
    new one.
    """
    patch = body.model_dump(exclude_unset=True, by_alias=False)
    base_version = patch.pop("base_version", None)
    return svc.edit_work(service, caller, slug, paper_id, patch, base_version=base_version)


@edit_router.post("/profiles/{slug}/works", response_model=EditResult, status_code=201)
def add_profile_work(
    slug: str,
    body: dict,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_caller),
) -> EditResult:
    """Add one new work. Never overwrites: an existing ``paper_id`` is a 409.

    The body is a whole :class:`~researcher_profiles.schema.PaperRecord`, typed
    as a dict so a malformed record is a 400 naming the field, like every other
    edit route, not a 422.
    """
    return svc.add_work(service, caller, slug, body if isinstance(body, dict) else {})


@edit_router.delete("/profiles/{slug}/works/{paper_id}", response_model=EditResult)
def delete_profile_work(
    slug: str,
    paper_id: str,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_caller),
    base_version: Optional[str] = None,
) -> EditResult:
    """Remove one work from ``sources/papers.jsonld``.

    ``base_version`` (a query parameter) works as on the work patch: stale,
    the delete is a 409 and the work stays.
    """
    return svc.remove_work(service, caller, slug, paper_id, base_version=base_version)


@edit_router.get("/profiles/{slug}/visibility", response_model=VisibilityReport)
def get_profile_visibility(
    slug: str,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_caller),
) -> VisibilityReport:
    """What is published, to whom, and why: the read side of the tier API.

    A projection of ``privacy.explain_tiers``, so an interface can show
    consequences without comparing tiers itself.
    """
    return svc.get_visibility(service, caller, slug)


@edit_router.patch("/profiles/{slug}/visibility", response_model=EditResult)
def patch_profile_visibility(
    slug: str,
    body: VisibilityPatch,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_caller),
) -> EditResult:
    """Set the profile-level default tier and/or per-artifact tiers.

    A ``role`` selector re-tiers every artifact with that role and reports the
    count in ``artifacts_changed``. A stale ``base_hash`` is a 409.
    """
    return svc.set_visibility(
        service,
        caller,
        slug,
        profile_visibility=body.profile_visibility,
        artifacts=[a.model_dump(exclude_unset=True) for a in body.artifacts],
        sections=[x.model_dump(exclude_unset=True) for x in body.sections],
        base_hash=body.base_hash,
    )
