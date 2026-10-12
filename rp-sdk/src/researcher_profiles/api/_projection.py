"""The read projection shared by the routes: visibility gates and wire payloads.

The public names are re-exported from ``researcher_profiles.api`` so a host
projects through the same code as the SDK.
"""

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from email.utils import format_datetime

from ..models.api import (
    ProfileMetadataPayload,
    ProfileSummary,
)
from ..privacy import (
    ALWAYS_PRIVATE_PREFIXES,
    CHUNK_SOURCE_TYPE_ROLE,
    TierExplanation,
    ViewerTier,
    chunk_source_tiers,
    label_access_rights,
    project_document,
    tier_allows,
)
from ..profile.payloads import metadata_payload_dict, profile_summary_dict
from ..schema import (
    REGISTRY_ISSUED_PROOF_KINDS,
    ProfileDocument,
    Proof,
    most_restrictive,
    role_default_visibility,
    strip_registry_issued_proofs,
)
from ..schema.jsonld import canonical_dumps
from ..store import ProfileStore

logger = logging.getLogger(__name__)


def _profile_summary(prof, viewer: ViewerTier) -> ProfileSummary:
    """Validate ``payloads.profile_summary_dict`` (shared with the static publisher) into the wire model."""
    return ProfileSummary.model_validate(profile_summary_dict(prof, viewer))


def metadata_payload(
    prof, viewer: ViewerTier, *, proofs: Sequence[Proof] = ()
) -> ProfileMetadataPayload:
    """Validate the shared metadata projection into the wire model.

    ``proofs`` are the registry-issued proofs to attach to this read; any
    stored copy of such a proof is dropped.
    """
    return ProfileMetadataPayload.model_validate(metadata_payload_dict(prof, viewer, proofs=proofs))


# Registry-issued proofs are computed per read, never stored.


def _served(prof, viewer: ViewerTier, proofs: Sequence[Proof]) -> ProfileDocument:
    md = prof.metadata
    doc = project_document(md, viewer)
    return doc.model_copy(update={"proof": strip_registry_issued_proofs(doc.proof) + list(proofs)})


def served_document(service, prof, viewer: ViewerTier) -> ProfileDocument:
    """The document a registry serves: the stored record plus its registry proofs.

    Projected for ``viewer``. Stored registry-issued proofs are replaced by the
    hook's. Never mutates ``prof.metadata``, which the store caches.
    """
    return _served(prof, viewer, service.proofs(prof.metadata.rid))


def served_document_bytes(
    service, store: ProfileStore, prof, viewer: ViewerTier, slug: str
) -> bytes:
    """The ``profile.jsonld`` bytes to serve for ``prof`` to ``viewer``.

    When nothing needs projecting, the stored JSON comes back as written plus
    ``accessRights`` labels; otherwise the served document is re-serialized.
    Raises what ``store.document_bytes`` raises.
    """
    proofs = service.proofs(prof.metadata.rid)
    stored_registry_proof = any(p.kind in REGISTRY_ISSUED_PROOF_KINDS for p in prof.metadata.proof)
    if not prof.metadata.section_visibility and not proofs and not stored_registry_proof:
        # Re-dumping the model would reshape untouched fields.
        stored = json.loads(store.document_bytes(slug))
        label_access_rights(stored, prof.metadata)
        return canonical_dumps(stored).encode()
    doc = _served(prof, viewer, proofs)
    return canonical_dumps(doc.model_dump(mode="json")).encode()


# ---------------------------------------------------------------------------
# The two gates
#
# Every read passes through these, in this order:
#
#   1. the profile gate (``Service.visible``): whether this viewer may see
#      this profile at all.
#   2. the artifact gate: which of its pieces they may see.
#
# Failing the profile gate is a 404 with the same body a genuinely nonexistent
# slug produces. Never a 403: a 403 tells the caller the profile exists, which
# is the one bit a held-back profile is trying not to disclose.
#
# Both gates defer to ``privacy``; nothing here compares tiers itself.
# ---------------------------------------------------------------------------


#: Tier-projected responses depend on credentials, not only the URL. Without
#: this a shared cache could hand a stranger an owner's view.
VARY_ON_CREDENTIALS = "Authorization, Cookie"


#: Seconds a shared cache may hold the anonymous rendering of a public profile
#: document. This bounds how long an unpublished profile stays visible in caches.
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


def _is_hard_floor(content_url: str) -> bool:
    """Withheld from every viewer, owner included (spec section 4): build state and keys."""
    return any(content_url.startswith(prefix) for prefix in ALWAYS_PRIVATE_PREFIXES)


def artifact_visible(
    explain: dict[str, TierExplanation],
    prof_md,
    content_url: str,
    role: str | None,
    viewer: ViewerTier,
) -> bool:
    """Whether ``viewer`` receives the body backing ``content_url``.

    An artifact absent from the manifest takes the profile and role defaults
    (spec section 2), not invisibility, so a manifest-less profile does not look
    empty.
    """
    if _is_hard_floor(content_url):
        return False
    entry = explain.get(content_url)
    if entry is not None:
        return tier_allows(viewer, entry.effective)
    inherited = most_restrictive(prof_md.visibility, role_default_visibility(role))
    return tier_allows(viewer, inherited)


def withheld(explain: dict[str, TierExplanation], viewer: ViewerTier) -> list[str]:
    """The ``contentUrl``s this viewer did not receive.

    Named, not silently absent, so a client can tell "withheld" from "does not
    exist".
    """
    return sorted(
        url
        for url, entry in explain.items()
        if _is_hard_floor(url) or not tier_allows(viewer, entry.effective)
    )


def _allowed_source_types(prof_md, viewer: ViewerTier) -> list[str] | None:
    """Embedding-chunk ``source_type``s this viewer may be quoted, or ``None``.

    ``None`` means no restriction, so the search path is unfiltered for a
    viewer entitled to everything.
    """
    tiers = chunk_source_tiers(prof_md, [(st, "") for st in CHUNK_SOURCE_TYPE_ROLE])
    allowed = [st for (st, _), tier in tiers.items() if tier_allows(viewer, tier)]
    if len(allowed) == len(CHUNK_SOURCE_TYPE_ROLE):
        return None
    return allowed


def _visible_hits(prof_md, viewer: ViewerTier, hits) -> list:
    """Drop every chunk whose source tier exceeds ``viewer``.

    A chunk carries its source document's tier. Without this a ``match``
    consumer could read a ``private`` CV back chunk by chunk.
    """
    hits = list(hits or [])
    if not hits:
        return []
    keys = {(h.source_type, h.source_id) for h in hits}
    tiers = chunk_source_tiers(prof_md, keys)
    return [h for h in hits if tier_allows(viewer, tiers[(h.source_type, h.source_id)])]


def _content_hash(store: ProfileStore, ref: str) -> str | None:
    """This profile's ``content_hash``, or ``None`` if the store cannot say.

    Never raises: a missing digest means no concurrency token, not a failed read.
    """
    try:
        return store.content_hash(ref)
    except Exception:  # pragma: no cover - defensive; every shipped store answers
        logger.debug("content_hash unavailable for %r", ref, exc_info=True)
        return None
