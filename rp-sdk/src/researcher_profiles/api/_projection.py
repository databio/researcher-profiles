"""The read projection every route module shares.

The projection is the gates that decide whether a viewer may see a profile and
which of its pieces, and the functions that turn a loaded profile into a wire
payload. The three ``APIRouter`` objects the route modules attach to live
beside this, in :mod:`.routers._routers`.

Four of these are the hooks a hosting service composes against, and they are
re-exported without an underscore from ``researcher_profiles.api``:
``artifact_visible``, ``invalidate_after_write``, ``metadata_payload`` and
``withheld``. Everything else here is internal to the route modules.
"""

import logging
from datetime import datetime, timezone
from email.utils import format_datetime

from fastapi import HTTPException, Request

from ..models.api import (
    ProfileMetadataPayload,
    ProfileSummary,
)
from ..privacy import (
    ALWAYS_RESTRICTED_PREFIXES,
    CHUNK_SOURCE_TYPE_ROLE,
    TierExplanation,
    ViewerTier,
    chunk_source_tiers,
    profile_visible,
    tier_allows,
)
from ..profile.payloads import metadata_payload_dict, profile_summary_dict
from ..schema import (
    ALWAYS_RESTRICTED_ROLES,
    most_restrictive,
    role_default_visibility,
)
from ..store import ProfileStore
from ..utils.paths import STORE_CACHE_DIRNAME
from .deps import (
    get_profile_tier_floor,
    viewer_tier_for,
)

logger = logging.getLogger(__name__)


def invalidate_after_write(request: Request, store: ProfileStore, slug: str) -> None:
    """Drop every cache that could still reflect the pre-edit profile.

    The cached profile object and the store's on-disk ``.cache`` memos
    (centroids/topics/graph): the exact set ``put_profile`` invalidates after a
    tarball push, factored out so the granular edit endpoints stay consistent
    with it. ``store.evict`` also bumps the store's write generation, which is
    the whole in-process invalidation for ranking: the analytics snapshot is
    stamped with that generation and rebuilds itself.

    **Cache invalidation only.** Dependent-state maintenance does not belong
    here: this function runs after the write is durable, inside a swallowing
    ``try/except``, and only for routes that call it. That job lives on the
    write hooks, ``ResearcherProfile.add_pre_commit_hook``
    (see :meth:`researcher_profiles.profile.ResearcherProfile.write_unit`),
    where it runs inside the write, before commit, and is not swallowed.

    Failures here stay swallowed: a stale-cache rebuild is cheap and
    re-eviction is idempotent.
    """
    store.evict(slug)
    # The derived graph follows the same rule: drop the in-process snapshot and the
    # on-disk cache so the next graph query rebuilds over the new corpus. A full
    # rebuild (not an incremental patch) is the source of truth, so a stale
    # graph can never survive a push, metadata patch, or visibility change.
    request.app.state.graph = None
    # A rootless store materializes into a temp directory that `get_graph`
    # (see api/deps.py `materialize_store_to_tempdir`) caches on
    # `_registry_tempdir` and reuses across requests. Dropping the graph
    # snapshot above is not enough on such a store: the next rebuild would
    # re-read the same stale temp dir and never see this write. Drop it so the
    # next graph query re-exports the live corpus. /match no longer needs this:
    # it ranks over the store's own vector rows, which this write already
    # updated.
    tempdir = getattr(request.app.state, "_registry_tempdir", None)
    if tempdir is not None:
        cleanup = getattr(tempdir, "cleanup", None)
        if callable(cleanup):
            try:
                cleanup()
            except OSError:
                pass
        request.app.state._registry_tempdir = None
    # This loop needs a filesystem root. The serve-time derived caches under
    # the profile's ``.cache/`` and the store's ``.cache/`` are not covered by ``ArtifactStorage``
    # (regenerable binary artifacts, sqlite handles: different semantics), so
    # they are not fixed here. A store that is not a directory has none of
    # them, which is what ``root is None`` means.
    root = store.root
    if root is None:
        return
    # These names are defined by ``analytics.centroids.CentroidManager.cache_path``,
    # ``analytics.match.MatchManager.topics_cache_path``, and ``graph.cache.graph_db_path``;
    # kept literal here because this path must work without importing them.
    for cache_name in ("centroids.npz", "topics.json", "graph.sqlite"):
        fp = root / STORE_CACHE_DIRNAME / cache_name
        if fp.exists():
            try:
                fp.unlink()
            except OSError:
                pass


def _profile_summary(prof) -> ProfileSummary:
    """Validate the shared summary projection into the wire model.

    The projection itself lives in ``payloads.profile_summary_dict``, which the
    static publisher renders from too, so a profile looks the same over HTTP as
    in the published site.
    """
    return ProfileSummary.model_validate(profile_summary_dict(prof))


def metadata_payload(prof) -> ProfileMetadataPayload:
    """Validate the shared metadata projection into the wire model.

    Part of the read-projection hooks re-exported from
    ``researcher_profiles.api``: a hosting service composing its own surface
    projects a profile's metadata through this, so its answer and the SDK's
    cannot drift. The projection is ``payloads.metadata_payload_dict``.
    """
    return ProfileMetadataPayload.model_validate(metadata_payload_dict(prof))


# ---------------------------------------------------------------------------
# The two gates
#
# Every read on ``public_router`` passes through these, in this order:
#
#   1. the profile gate: whether this viewer may see this profile at all.
#   2. the artifact gate: which of its pieces they may see.
#
# Failing the profile gate is a 404 with the same body a genuinely nonexistent
# slug produces. Never a 403: a 403 tells the caller the profile exists, which
# is the one bit a held-back profile is trying not to disclose.
#
# Both gates are projections of ``privacy.explain_tiers`` / ``privacy.tier_allows``.
# Nothing here compares two tiers itself.
# ---------------------------------------------------------------------------


#: Every tier-projected response is a function of the caller's credentials, not
#: only of its URL. Without this a shared cache that stored one caller's copy
#: would serve it to the next caller, which on the one route with a TTL means
#: handing a stranger an owner's view.
VARY_ON_CREDENTIALS = "Authorization, Cookie"


#: How long a shared cache may hold the anonymous rendering of a public
#: profile document. This is the bound on unpublishing: an owner who withdraws
#: consent is invisible to the origin on the next request, and to a shared
#: cache within this many seconds. Sixty is the number we are choosing; it is
#: short enough that "I unpublished and it is still up" is not a support
#: question, and long enough to absorb a crawl.
PUBLIC_DOCUMENT_MAX_AGE = 60


def _http_date(iso: str | None) -> str | None:
    """Convert an ISO 8601 ``dateModified`` to an HTTP-date for ``Last-Modified``."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return format_datetime(dt, usegmt=True)
    except (ValueError, TypeError):
        return None


def _profile_missing(ref: str) -> HTTPException:
    """The 404 for "no such profile" and for "not for you": byte-identical."""
    return HTTPException(status_code=404, detail=f"profile {ref!r} not found")


def _gate_profile(request: Request, prof, viewer: ViewerTier, ref: str) -> None:
    """Raise the not-found 404 unless ``viewer`` may see this profile."""
    floor = get_profile_tier_floor(request, prof, ref)
    if not profile_visible(prof.metadata, viewer, floor=floor.tier):
        raise _profile_missing(ref)


def _is_hard_floor(content_url: str, role: str | None) -> bool:
    """Withheld from every viewer, owner included (spec section 4).

    Copyrighted full text is a legal floor; ``.cache/``/``.keys/`` is build-local
    derived state and key material, not a servable artifact.
    """
    if role in ALWAYS_RESTRICTED_ROLES:
        return True
    return any(content_url.startswith(prefix) for prefix in ALWAYS_RESTRICTED_PREFIXES)


def artifact_visible(
    explain: dict[str, TierExplanation],
    prof_md,
    content_url: str,
    role: str | None,
    viewer: ViewerTier,
) -> bool:
    """Whether ``viewer`` receives the body backing ``content_url``.

    An artifact absent from the manifest has declared no tier of its own, so
    the profile default and its role default govern it (spec section 2): the
    same answer ``explain_tiers`` would give if it were listed. Treating an
    unlisted artifact as invisible instead would make a profile built before
    manifests were written look empty rather than public.
    """
    if _is_hard_floor(content_url, role):
        return False
    entry = explain.get(content_url)
    if entry is not None:
        return tier_allows(viewer, entry.effective)
    inherited = most_restrictive(prof_md.visibility, role_default_visibility(role))
    return tier_allows(viewer, inherited)


def withheld(explain: dict[str, TierExplanation], viewer: ViewerTier) -> list[str]:
    """The ``contentUrl``s this viewer did not receive.

    Named, not silently absent: a client has to be able to tell "withheld" from
    "does not exist", and an owner previewing as a stranger has to be able to
    see the shape of what the stranger is missing.
    """
    return sorted(
        url
        for url, entry in explain.items()
        if _is_hard_floor(url, entry.role) or not tier_allows(viewer, entry.effective)
    )


def _allowed_source_types(prof_md, viewer: ViewerTier) -> list[str] | None:
    """Embedding-chunk ``source_type``s this viewer may be quoted, or ``None``.

    ``None`` means "no restriction" (every known source type is allowed), so
    the search path is byte-identical to the unfiltered one for a viewer
    entitled to everything. Resolved with :func:`privacy.chunk_source_tiers`,
    the same function the flat exporter uses; there is no second allowlist.
    """
    tiers = chunk_source_tiers(prof_md, [(st, "") for st in CHUNK_SOURCE_TYPE_ROLE])
    allowed = [st for (st, _), tier in tiers.items() if tier_allows(viewer, tier)]
    if len(allowed) == len(CHUNK_SOURCE_TYPE_ROLE):
        return None
    return allowed


def _ranked_visible(request: Request, prof, viewer: ViewerTier) -> tuple[bool, ViewerTier]:
    """``(may this viewer be told about it, their tier for it)``. Fail-closed.

    ``viewer`` is the caller's baseline; the tier that governs is resolved per
    profile, because a grant is held on one profile and not on the rest.
    """
    md = getattr(prof, "metadata", None)
    if md is None:
        return False, viewer
    slug = getattr(prof, "slug", None)
    per_profile = viewer_tier_for(request, slug)
    floor = get_profile_tier_floor(request, prof, slug)
    return profile_visible(md, per_profile, floor=floor.tier), per_profile


def _visible_hits(prof_md, viewer: ViewerTier, hits) -> list:
    """Drop every chunk whose source tier exceeds ``viewer``.

    A chunk is derived from a document, so it carries that document's tier.
    This is the general derivation rule, applied to the served sqlite index exactly as
    ``embeddings/flat.py`` applies it to the exported one. Without this a
    ``match``-scoped consumer reads a researcher's CV back verbatim, chunk by
    chunk, from a profile whose CV is ``restricted``.
    """
    hits = list(hits or [])
    if not hits:
        return []
    keys = {(h.source_type, h.source_id) for h in hits}
    tiers = chunk_source_tiers(prof_md, keys)
    return [h for h in hits if tier_allows(viewer, tiers[(h.source_type, h.source_id)])]


# ---------------------------------------------------------------------------
# Owner-scoped interactive edits (edit_router; gated by require_owner)
# ---------------------------------------------------------------------------


def _content_hash(store: ProfileStore, ref: str) -> str | None:
    """This profile's ``content_hash``, or ``None`` if the store cannot say.

    Never raises. A store that cannot produce the digest is a store that cannot
    detect a conflict, and that is a reason to serve the profile without a
    concurrency token, not a reason to fail the read.
    """
    try:
        return store.content_hash(ref)
    # Boundary: a store that cannot digest is a store that cannot detect a conflict.
    except Exception:  # pragma: no cover - defensive; every shipped store answers
        logger.debug("content_hash unavailable for %r", ref, exc_info=True)
        return None
