"""The owner-scoped interactive edit surface: metadata (narrative included), works, visibility.

Every route here is a thin adapter over a function in
:mod:`researcher_profiles.api.service`: it builds the caller, turns the body
into arguments, and calls the function, which loads the stored profile, runs
the host's edit gate (``hooks.edit_gate``; the operator token on bare rp-sdk),
checks the version token and the write scope, writes, and records the edit.
Typed errors become HTTP replies in ``api._errors``.
"""

import logging
from typing import Optional

from fastapi import Depends

from ...models.api import (
    ArtifactTier,
    EditResult,
    MetadataPatch,
    SectionTierReport,
    VisibilityPatch,
    VisibilityReport,
    WorkPatch,
)
from ...privacy import (
    SECTION_FIELDS,
    ViewerTier,
    explain_tiers,
    section_tiers,
    tier_allows,
)
from ...schema import most_restrictive
from .. import service as svc
from .._projection import _is_hard_floor
from ..caller import Caller
from ..deps import get_caller, get_profile, get_service
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

    Only the fields present in the body are applied. ``soul`` replaces the
    "how I think" narrative (``personality/SOUL.md``); it is applied in the
    same write unit as the other fields, so the edit is atomic and the
    profile's ``content_hash`` moves once. Every profile field is
    editable except the locked ones
    (:data:`researcher_profiles.profile.edit.LOCKED_METADATA_FIELDS`: identity,
    code-computed facts, the manifest, bookkeeping, and visibility); the
    editable set is :data:`researcher_profiles.profile.edit.EDITABLE_METADATA_FIELDS`. The patch is re-validated against the profile schema before it
    is written; a patch that would produce an invalid document is a 400 and
    leaves the profile untouched.

    Send ``base_hash`` (the ``content_hash`` from the ``GET`` this edit was
    composed against) to get a **409** instead of a silent overwrite when
    somebody else wrote in the meantime. Omit it and the patch is
    last-writer-wins, unchanged from before.
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

    The granular write for a corpus record: a wrong ``doi`` on one paper is a
    one-field fix, and before this the only transport was a whole-profile push.
    Only the fields present in the body are applied, and only fields in the
    editable whitelist
    (:data:`researcher_profiles.profile.edit.EDITABLE_WORK_FIELDS`). The patched
    record is re-validated before it is written; a patch that would produce an
    invalid record is a 400 and leaves the corpus untouched.

    Send ``base_version`` (the paper's ``version`` from ``GET /papers`` or
    ``GET /papers/{paper_id}``) to get a **409** carrying the current version
    in ``X-RP-Paper-Version`` instead of overwriting somebody else's edit to
    this paper. The reply's ``version`` is the paper's new version.
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

    The body is a whole :class:`~researcher_profiles.schema.PaperRecord`
    carrying its ``paper_id``, annotated as a dict and parsed by ``add_work``:
    taking the on-disk model as the declared body would make a malformed
    record a 422 about the request shape instead of the 400 naming the
    offending field that every other edit route answers with. To change a work
    already there, ``PATCH /works/{paper_id}``.
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

    ``PATCH .../visibility`` existed with nothing to read it back: an owner
    could set a tier and had no way to learn what it resolved to, which is how
    the derivation rule stayed invisible. Everything here is a projection of
    ``privacy.explain_tiers`` and ``privacy.tier_allows``, so an interface
    renders consequences ("a stranger can see 0 of 63 items") without
    reimplementing a single comparison.
    """
    store = service.store
    prof = get_profile(slug, store)
    # Owner tooling: a caller who may read the profile whole is admitted too.
    svc.require_edit(service, caller, prof, read_ok=True, ref=slug)
    resolved = store.resolve_slug(slug)
    explain = explain_tiers(prof.metadata)
    floor = service.floor(caller, prof, slug)

    viewers: dict[str, ViewerTier] = {"anonymous": "public", "lab": "limited", "you": "private"}
    counts = dict.fromkeys(viewers, 0)
    artifacts: list[ArtifactTier] = []
    for entry in explain.values():
        floored = _is_hard_floor(entry.content_url)
        visible_to = []
        for label, tier in viewers.items():
            if not floored and tier_allows(tier, entry.effective):
                visible_to.append(label)
                counts[label] += 1
        artifacts.append(
            ArtifactTier(
                content_url=entry.content_url,
                role=entry.role,
                name=entry.name,
                paper_id=entry.paper_id,
                declared=entry.declared,
                effective=entry.effective,
                raised_by=list(entry.raised_by),
                visible_to=visible_to,
            )
        )
    artifacts.sort(key=lambda a: a.content_url)

    # One row per inline section, in SECTION_FIELDS order, carrying BOTH the
    # tier the owner declared and the tier that actually governs. The `soul`
    # section governs no inline field: it reaches personality/SOUL.md through
    # the manifest, so its `declared` folds in the soul artifact's own declared
    # tier and the read-back matches what actually gates the file.
    section_effective = section_tiers(prof.metadata)
    declared_map = {x.section: x.visibility for x in prof.metadata.section_visibility}
    soul_declared = [e.declared for e in explain.values() if e.role == "soul"]
    sections: list[SectionTierReport] = []
    for section in SECTION_FIELDS:
        declared = declared_map.get(section, "public")
        if section == "soul":
            declared = most_restrictive(declared, *soul_declared)
        effective = section_effective[section]
        sections.append(
            SectionTierReport(
                section=section,
                declared=declared,
                effective=effective,
                visible_to=[
                    label for label, tier in viewers.items() if tier_allows(tier, effective)
                ],
            )
        )

    return VisibilityReport(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        profile_visibility=prof.metadata.visibility,
        profile_floor=floor.tier,
        profile_floor_reason=floor.reason if floor.tier else None,
        artifacts=artifacts,
        sections=sections,
        counts=counts,
    )


@edit_router.patch("/profiles/{slug}/visibility", response_model=EditResult)
def patch_profile_visibility(
    slug: str,
    body: VisibilityPatch,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_caller),
) -> EditResult:
    """Set the profile-level default tier and/or per-artifact tiers.

    A ``role`` selector re-tiers every artifact with that role, and the reply
    says how many (``artifacts_changed``); the number is what makes a
    first-match-wins regression impossible to reintroduce quietly. Every role's
    tier is choosable, ``paper_fulltext`` included.

    ``base_hash`` works as on the metadata patch: supplied and stale, the
    request is a 409 and nothing is re-tiered.
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
