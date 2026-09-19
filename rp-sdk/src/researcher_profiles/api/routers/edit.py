"""The owner-scoped interactive edit surface: metadata, soul, works, visibility.

Every route here hangs on ``edit_router``, which carries a router-level
``Depends(require_owner)``. ``deps.require_owner`` takes ``slug`` as a required
parameter, so every route on this router must keep a ``{slug}`` path parameter
or FastAPI would surface ``slug`` as a required query parameter instead.
"""

import logging

from fastapi import Depends, HTTPException, Request

from ...models.api import (
    ArtifactTier,
    EditResult,
    MetadataPatch,
    SectionTierReport,
    SoulUpdate,
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
from ...profile.edit import EditError, WorkNotFoundError
from ...schema import PaperRecord, most_restrictive
from ...store import ProfileStore
from .._projection import (
    _content_hash,
    _is_hard_floor,
    invalidate_after_write,
)
from ..deps import (
    check_write_scope,
    get_profile,
    get_profile_tier_floor,
    get_store,
)
from ._routers import edit_router

logger = logging.getLogger(__name__)


def _check_base_hash(store: ProfileStore, ref: str, base_hash: str | None) -> None:
    """Refuse an edit composed against a version other than the current one.

    Opt-in by construction: a caller that sends no ``base_hash`` gets
    last-writer-wins, which is what a single-owner CLI wants and what every
    existing caller already relies on. A caller that does send one is asking to
    be told, and the grant model admits an ``editor`` alongside the owner, so
    "told" has to mean refused: a form that silently overwrites a co-editor's
    paragraph is the failure this exists to prevent.

    The digest spans the document and the SOUL together (see
    ``ResearcherProfile.content_hash``), so metadata and soul writes
    conflict with each other rather than each keeping a private clock.
    """
    if base_hash is None:
        return
    current = _content_hash(store, ref)
    if current is None or current == base_hash:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            "this profile changed since you loaded it "
            f"(current content_hash {current}); reload it and re-apply your edit"
        ),
        headers={"X-RP-Content-Hash": current},
    )


@edit_router.patch("/profiles/{slug}/metadata", response_model=EditResult)
def patch_profile_metadata(
    slug: str,
    body: MetadataPatch,
    request: Request,
    store: ProfileStore = Depends(get_store),
) -> EditResult:
    """Patch owner-editable metadata (name/affiliation/field/expertise/links).

    Only the fields present in the body are applied, and only fields in the
    editable whitelist (:data:`researcher_profiles.profile.edit.EDITABLE_METADATA_FIELDS`)
    are accepted. The patch is re-validated against the profile schema before it
    is written; a patch that would produce an invalid document is a 400 and
    leaves the profile untouched.

    Send ``base_hash`` (the ``content_hash`` from the ``GET`` this edit was
    composed against) to get a **409** instead of a silent overwrite when
    somebody else wrote in the meantime. Omit it and the patch is
    last-writer-wins, unchanged from before.
    """
    prof = get_profile(slug, store)
    patch = body.model_dump(exclude_unset=True, by_alias=False)
    # Neither of these is a metadata field. ``slug`` is the route parameter and
    # renaming is not an edit; ``base_hash`` is the concurrency token. Both are
    # popped before the whitelist check, or the whitelist would reject the
    # caller's own bookkeeping.
    patch.pop("slug", None)
    base_hash = patch.pop("base_hash", None)
    _check_base_hash(store, store.resolve_slug(slug), base_hash)
    check_write_scope(request, "metadata", {"fields": sorted(patch)})
    try:
        prof.edit.patch_metadata(patch)
    except EditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    resolved = store.resolve_slug(slug)
    invalidate_after_write(request, store, resolved)
    return EditResult(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        updated=sorted(patch.keys()),
        content_hash=_content_hash(store, resolved),
    )


@edit_router.put("/profiles/{slug}/soul", response_model=EditResult)
def put_profile_soul(
    slug: str,
    body: SoulUpdate,
    request: Request,
    store: ProfileStore = Depends(get_store),
) -> EditResult:
    """Replace the SOUL/persona narrative (``personality/SOUL.md``)."""
    prof = get_profile(slug, store)
    _check_base_hash(store, store.resolve_slug(slug), body.base_hash)
    check_write_scope(request, "soul", {})
    try:
        prof.edit.set_soul(body.soul)
    except EditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    resolved = store.resolve_slug(slug)
    invalidate_after_write(request, store, resolved)
    return EditResult(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        updated=["soul"],
        content_hash=_content_hash(store, resolved),
    )


def _work_edit_error(e: EditError) -> HTTPException:
    """The status an :class:`EditError` from a work edit deserves.

    404 when the corpus has no such ``paper_id`` (the caller addressed nothing),
    400 for every other refusal (the caller addressed the right record and sent
    something it may not send).
    """
    status = 404 if isinstance(e, WorkNotFoundError) else 400
    return HTTPException(status_code=status, detail=str(e))


@edit_router.get("/profiles/{slug}/works/{paper_id}", response_model=PaperRecord)
def get_profile_work(
    slug: str,
    paper_id: str,
    store: ProfileStore = Depends(get_store),
) -> PaperRecord:
    """One work's whole record, as an editor needs to see it before patching.

    ``GET /profiles/{slug}/papers`` serves a reading list and drops the fields
    this surface edits (``citation``, ``access``, ``summary``, ...), so an
    owner had no way to read back what they were about to change. Owner-scoped
    like the rest of this router, and the full record is exactly what that
    scope already grants.
    """
    prof = get_profile(slug, store)
    for record in prof.papers:
        if record.paper_id == paper_id:
            return record
    raise HTTPException(
        status_code=404, detail=f"no work with paper_id {paper_id!r} in this profile"
    )


@edit_router.patch("/profiles/{slug}/works/{paper_id}", response_model=EditResult)
def patch_profile_work(
    slug: str,
    paper_id: str,
    body: WorkPatch,
    request: Request,
    store: ProfileStore = Depends(get_store),
) -> EditResult:
    """Patch owner-editable fields of one work in ``sources/papers.jsonld``.

    The granular write for a corpus record: a wrong ``doi`` on one paper is a
    one-field fix, and before this the only transport was a whole-profile push.
    Only the fields present in the body are applied, and only fields in the
    editable whitelist
    (:data:`researcher_profiles.profile.edit.EDITABLE_WORK_FIELDS`). The patched
    record is re-validated before it is written; a patch that would produce an
    invalid record is a 400 and leaves the corpus untouched.

    ``base_hash`` works as on the metadata patch. Note what it covers: the
    digest spans the profile document and the SOUL (see
    :func:`_check_base_hash`), so it catches a concurrent *profile* edit, not a
    concurrent edit to a different work.
    """
    prof = get_profile(slug, store)
    patch = body.model_dump(exclude_unset=True, by_alias=False)
    # ``base_hash`` is the concurrency token, not a field of the work. Popped
    # before the whitelist check, or the whitelist would reject the caller's own
    # bookkeeping; ``paper_id`` never appears here because it is the route
    # parameter and re-keying a record is not a patch.
    base_hash = patch.pop("base_hash", None)
    _check_base_hash(store, store.resolve_slug(slug), base_hash)
    check_write_scope(request, "works", {"paper_id": paper_id, "fields": sorted(patch)})
    try:
        prof.edit.patch_work(paper_id, patch)
    except EditError as e:
        raise _work_edit_error(e) from e
    resolved = store.resolve_slug(slug)
    invalidate_after_write(request, store, resolved)
    return EditResult(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        updated=sorted(patch.keys()),
        content_hash=_content_hash(store, resolved),
    )


@edit_router.put("/profiles/{slug}/works/{paper_id}", response_model=EditResult)
def put_profile_work(
    slug: str,
    paper_id: str,
    body: dict,
    request: Request,
    store: ProfileStore = Depends(get_store),
) -> EditResult:
    """Add one work, or replace the record already under this ``paper_id``.

    The body is a whole :class:`~researcher_profiles.schema.PaperRecord`, but
    it is annotated as a dict and parsed by ``add_work``: taking the on-disk
    model as the declared body would make a malformed record a 422 about the
    request shape instead of the 400 naming the offending field that every
    other edit route answers with.

    The path's ``paper_id`` wins over whatever the body carries, so a record
    can never be filed under a name other than the one it was addressed by.
    """
    prof = get_profile(slug, store)
    check_write_scope(request, "works", {"paper_id": paper_id, "fields": ["*"]})
    try:
        prof.edit.add_work({**body, "paper_id": paper_id})
    except EditError as e:
        raise _work_edit_error(e) from e
    resolved = store.resolve_slug(slug)
    invalidate_after_write(request, store, resolved)
    return EditResult(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        updated=[paper_id],
        content_hash=_content_hash(store, resolved),
    )


@edit_router.delete("/profiles/{slug}/works/{paper_id}", response_model=EditResult)
def delete_profile_work(
    slug: str,
    paper_id: str,
    request: Request,
    store: ProfileStore = Depends(get_store),
) -> EditResult:
    """Remove one work from ``sources/papers.jsonld``."""
    prof = get_profile(slug, store)
    check_write_scope(request, "works", {"paper_id": paper_id, "fields": []})
    try:
        prof.edit.remove_work(paper_id)
    except EditError as e:
        raise _work_edit_error(e) from e
    resolved = store.resolve_slug(slug)
    invalidate_after_write(request, store, resolved)
    return EditResult(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        updated=[paper_id],
        content_hash=_content_hash(store, resolved),
    )


@edit_router.get("/profiles/{slug}/visibility", response_model=VisibilityReport)
def get_profile_visibility(
    slug: str,
    request: Request,
    store: ProfileStore = Depends(get_store),
) -> VisibilityReport:
    """What is published, to whom, and why: the read side of the tier API.

    ``PATCH .../visibility`` existed with nothing to read it back: an owner
    could set a tier and had no way to learn what it resolved to, which is how
    the derivation rule stayed invisible. Everything here is a projection of
    ``privacy.explain_tiers`` and ``privacy.tier_allows``, so an interface
    renders consequences ("a stranger can see 0 of 63 items") without
    reimplementing a single comparison.
    """
    prof = get_profile(slug, store)
    resolved = store.resolve_slug(slug)
    explain = explain_tiers(prof.metadata)
    floor = get_profile_tier_floor(request, prof, slug)

    viewers: dict[str, ViewerTier] = {"anonymous": "public", "lab": "internal", "you": "restricted"}
    counts = dict.fromkeys(viewers, 0)
    artifacts: list[ArtifactTier] = []
    for entry in explain.values():
        floored = _is_hard_floor(entry.content_url, entry.role)
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
                locked=entry.locked,
                lock_reason=entry.lock_reason,
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
    request: Request,
    store: ProfileStore = Depends(get_store),
) -> EditResult:
    """Set the profile-level default tier and/or per-artifact tiers.

    A ``role`` selector re-tiers every artifact with that role, and the reply
    says how many (``artifacts_changed``); the number is what makes a
    first-match-wins regression impossible to reintroduce quietly.

    Asking to raise a legally-floored artifact (``paper_fulltext``) is a 400
    quoting the floor, not a success followed by a silent re-pin.

    ``base_hash`` works as on the metadata patch: supplied and stale, the
    request is a 409 and nothing is re-tiered.
    """
    prof = get_profile(slug, store)
    _check_base_hash(store, store.resolve_slug(slug), body.base_hash)
    artifacts = [a.model_dump(exclude_unset=True) for a in body.artifacts]
    sections = [s.model_dump(exclude_unset=True) for s in body.sections]
    check_write_scope(
        request,
        "visibility",
        {
            "slug": slug,
            "profile_visibility": body.profile_visibility,
            "artifacts": artifacts,
            "sections": sections,
        },
    )
    try:
        _doc, changed = prof.edit.set_visibility(
            profile_visibility=body.profile_visibility,
            artifacts=artifacts or None,
            sections=sections or None,
        )
    except EditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    invalidate_after_write(request, store, store.resolve_slug(slug))
    updated = []
    if body.profile_visibility is not None:
        updated.append("visibility")
    if artifacts:
        updated.append("artifacts")
    if sections:
        updated.append("sections")
    resolved = store.resolve_slug(slug)
    return EditResult(
        slug=resolved,
        rid=getattr(prof, "rid", None),
        updated=updated,
        artifacts_changed=changed,
        content_hash=_content_hash(store, resolved),
    )
