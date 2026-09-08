"""Privacy tiers, the derivation rule, and ``.publishignore`` generation.

Privacy is data *declared in the profile* (a tier on every artifact plus a
profile-level default) and *expressed in the layout* (a ``.publishignore``
exclude list), so a dumb sync is a correct sync: no registry-side
``.visibility.json`` sidecar, no Python egress filter.

The single authority is :func:`effective_tiers`: the effective tier of an
artifact is the most restrictive of the profile default, the artifact's own
tier, and the tiers of everything it was derived from. Everything calls it:
a sync's ``.publishignore``, the embedding-chunk exporter, the validator.
There is no second implementation.
"""

from dataclasses import dataclass, field
from typing import Iterable

from .schema import (
    _VISIBILITY_ORDER,
    ALWAYS_RESTRICTED_ROLES,
    ProfileDocument,
    Visibility,
    most_restrictive,
    role_default_visibility,
)

#: The most permissive tier a caller may be shown. Same value space as
#: :data:`~researcher_profiles.schema.Visibility`, read in the other direction:
#: on an artifact it says "this may go no further than X", on a viewer it says
#: "this caller may see as far as X". Anonymous is not a special case. It is
#: the viewer whose tier is ``public``.
ViewerTier = Visibility

#: The sentence an owner is shown when they try to raise a legally-floored
#: artifact. Written once, here, so the API refusal, the tier explanation, and
#: any UI all say the same words. A floor explained two ways reads as a bug.
FULLTEXT_LOCK_REASON = (
    "Extracted full text of a copyrighted paper. This is a legal floor, not a "
    "preference, and cannot be raised above restricted by anyone, including you."
)

#: Map an embedding chunk's ``source_type`` to the manifest ``role`` whose
#: default tier governs it. For every source the two names coincide today; the
#: map is written explicitly so the chunk -> role -> tier derivation is
#: auditable rather than incidental. ``cv``/``web``/``grant`` resolve to
#: ``restricted`` by :data:`schema.ROLE_DEFAULT_VISIBILITY`; the rest are public.
CHUNK_SOURCE_TYPE_ROLE: dict[str, str] = {
    "soul": "soul",
    "expertise": "expertise",
    "paper_summary": "paper_summary",
    "paper_abstract": "paper_abstract",
    "cv": "cv",
    "web": "web",
    "grant": "grant",
}

#: Directory prefixes (relative to the profile content root) excluded from a
#: public sync unconditionally: profile-adjacent serve-time caches and local key
#: material. ``.cache/`` holds derived caches (``embeddings.sqlite``,
#: ``topics.json``, …) that are regenerable and never part of the public record;
#: ``embeddings.sqlite`` is still reachable by an authorized (``restricted``-tier)
#: consumer through the manifest, which governs restricted retrieval separately
#: from this public-sync exclude list. Build-session bookkeeping is not a
#: concern here: it lives in the build root outside the content tree entirely.
ALWAYS_RESTRICTED_PREFIXES: tuple[str, ...] = (".cache/", ".keys/")


def _own_tier(part) -> Visibility:
    """An artifact's own tier: its declared ``visibility``, floored by role.

    A legally-restricted role (full text) is ``restricted`` no matter what the
    artifact declared. (The model already enforces this; belt and braces.)
    """
    if part.role in ALWAYS_RESTRICTED_ROLES:
        return "restricted"
    return part.visibility


def tier_allows(viewer: ViewerTier, artifact: Visibility) -> bool:
    """True when a viewer entitled to ``viewer`` may see ``artifact``.

    The one place two tiers are compared. Every route, host, and template
    calls this rather than re-deriving the rule.
    """
    return _VISIBILITY_ORDER.index(artifact) <= _VISIBILITY_ORDER.index(viewer)


def narrow_viewer(*viewers: ViewerTier) -> ViewerTier:
    """The least permissive of the given viewer tiers: a cap, never a widening.

    Not :func:`~researcher_profiles.schema.most_restrictive`, which is the rule
    for artifacts. The two read the tier scale in opposite directions: on an
    artifact a higher tier means fewer people may see it, on a viewer a higher
    tier means they may see more. Capping a viewer therefore takes the minimum,
    and using the artifact rule here would have handed an owner previewing as a
    stranger their own unrestricted view, the exact lie a preview exists to
    prevent.
    """
    rank = min(_VISIBILITY_ORDER.index(v) for v in viewers)
    return _VISIBILITY_ORDER[rank]


@dataclass(frozen=True)
class TierExplanation:
    """Why one artifact resolves to the tier it does.

    The decision and its explanation are one computation (see
    :func:`explain_tiers`), so the sentence an owner reads can never describe a
    rule other than the one that ran.
    """

    content_url: str
    role: str | None = None
    name: str | None = None
    paper_id: str | None = None
    #: What is written on the ``ArtifactRef`` (the role default already applied).
    declared: Visibility = "public"
    #: After the legal floor, the profile default, and the derivation rule.
    effective: Visibility = "public"
    #: The role carries a legal floor: no one, owner included, may raise it.
    locked: bool = False
    #: A full sentence, shown verbatim to a human. ``None`` unless ``locked``.
    lock_reason: str | None = None
    #: Concrete causes that held the artifact above its declared tier: the
    #: specific source, not a restatement of the rule.
    raised_by: list[str] = field(default_factory=list)


def explain_tiers(profile: ProfileDocument) -> dict[str, TierExplanation]:
    """``{contentUrl: TierExplanation}`` for every manifest artifact.

    Effective tier = most restrictive of the profile default, the artifact's
    own tier (floored by role), and the tiers of everything in its
    ``derivedFrom`` (resolved by ``paperId`` or by ``role``). Alongside each
    decision this records which of those causes is holding the artifact where
    it is, so an interface can name the cause on the row rather than explaining
    the rule in prose.
    """
    parts = list(profile.has_part) + list(profile.subject_of)
    by_paper: dict[str, list] = {}
    by_role: dict[str, list] = {}
    for p in parts:
        if p.paper_id:
            by_paper.setdefault(p.paper_id, []).append(p)
        if p.role:
            by_role.setdefault(p.role, []).append(p)

    default = profile.visibility
    out: dict[str, TierExplanation] = {}
    for p in parts:
        own = _own_tier(p)
        locked = p.role in ALWAYS_RESTRICTED_ROLES
        # (tier, phrase) for every cause that could be holding this artifact up.
        causes: list[tuple[Visibility, str]] = []
        if locked:
            causes.append(("restricted", "a legal floor on paper full text (restricted)"))
        causes.append((default, f"the profile default ({default})"))
        role_default = role_default_visibility(p.role)
        if role_default != "public":
            causes.append((role_default, f"the role default for {p.role} ({role_default})"))
        for ref in p.derived_from or []:
            for src in by_paper.get(ref, []) + by_role.get(ref, []):
                src_tier = _own_tier(src)
                causes.append((src_tier, f"derived from {src.content_url} ({src_tier})"))

        effective = most_restrictive(own, *(t for t, _ in causes))
        # Only causes sitting AT the effective tier are holding it there; a
        # cause below it explains nothing.
        raised_by = (
            [phrase for tier, phrase in causes if tier == effective]
            if effective != "public"
            else []
        )
        out[p.content_url] = TierExplanation(
            content_url=p.content_url,
            role=p.role,
            name=p.name,
            paper_id=p.paper_id,
            declared=p.visibility,
            effective=effective,
            locked=locked,
            lock_reason=FULLTEXT_LOCK_REASON if locked else None,
            raised_by=raised_by,
        )
    return out


def effective_tiers(profile: ProfileDocument) -> dict[str, Visibility]:
    """Return ``{contentUrl: effective_tier}`` for every manifest artifact.

    A thin projection of :func:`explain_tiers`. The two cannot disagree because
    there is only one of them.
    """
    return {k: v.effective for k, v in explain_tiers(profile).items()}


def profile_visible(
    profile: ProfileDocument,
    viewer: ViewerTier,
    *,
    floor: Visibility | None = None,
) -> bool:
    """Whether ``viewer`` may see this profile at all: the first of two gates.

    ``floor`` is a host-supplied constraint ("regardless of what this document
    declares, it may not go above X here"): a registry pins an unclaimed profile
    to ``internal`` this way, because nobody has consented to publish it. Folded
    in with :func:`~researcher_profiles.schema.most_restrictive`, so a floor can
    only ever narrow.
    """
    return tier_allows(viewer, most_restrictive(profile.visibility, floor))


def publishignore_lines(profile: ProfileDocument) -> list[str]:
    """Lines for ``.publishignore``: every path whose effective tier > public.

    Includes manifest artifacts above ``public`` plus the always-restricted
    directory prefixes (``.cache/``, ``.keys/``) and ``.publishignore`` itself.
    Sorted and de-duplicated so the file diffs cleanly.
    """
    lines: set[str] = set(ALWAYS_RESTRICTED_PREFIXES)
    lines.add(".publishignore")
    for content_url, tier in effective_tiers(profile).items():
        if tier != "public":
            lines.add(content_url)
    return sorted(lines)


def render_publishignore(profile: ProfileDocument) -> str:
    """The ``.publishignore`` file body (a trailing newline, stable order)."""
    return "\n".join(publishignore_lines(profile)) + "\n"


def is_publishable(content_url: str, effective: dict[str, Visibility]) -> bool:
    """True if ``content_url``'s effective tier is ``public``."""
    return effective.get(content_url, "public") == "public"


def chunk_source_tiers(
    profile: ProfileDocument | None,
    chunk_keys: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], Visibility]:
    """Effective tier for each ``(source_type, source_id)`` chunk source.

    A chunk is derived from a source document, so its effective privacy tier is
    that of its source: the general derivation rule, not a special-cased
    allowlist. This resolves each chunk source with the same primitives
    :func:`effective_tiers` uses: the profile-level default floored with the
    role default for the source type
    (:func:`schema.role_default_visibility`). ``cv``/``web``/``grant`` chunks
    come back ``restricted``; ``expertise``/``soul``/``paper_summary``/
    ``paper_abstract`` come back ``public`` (unless the whole profile is held
    back by its default). The embedding exporter drops every key whose tier
    exceeds ``public`` via :func:`drop_above_public`.

    ``profile`` may be ``None`` (an unloadable ``profile.jsonld``), in which
    case the profile default is treated as ``public`` and only the role
    defaults do the filtering.
    """
    default = getattr(profile, "visibility", "public") or "public"
    out: dict[tuple[str, str], Visibility] = {}
    for source_type, source_id in chunk_keys:
        role = CHUNK_SOURCE_TYPE_ROLE.get(source_type, source_type)
        out[(source_type, source_id)] = most_restrictive(default, role_default_visibility(role))
    return out


def drop_above_public(
    items: Iterable[tuple[str, Visibility]],
) -> list[str]:
    """Given ``(id, tier)`` pairs, return the ids whose tier is ``public``.

    The embedding-chunk exporter uses this to drop any row whose source tier
    exceeds ``public``, the general form of the former ``PUBLISHED_SOURCE_TYPES``
    allowlist.
    """
    return [ident for ident, tier in items if tier == "public"]


__all__ = [
    "ALWAYS_RESTRICTED_PREFIXES",
    "CHUNK_SOURCE_TYPE_ROLE",
    "FULLTEXT_LOCK_REASON",
    "TierExplanation",
    "ViewerTier",
    "explain_tiers",
    "narrow_viewer",
    "profile_visible",
    "tier_allows",
    "chunk_source_tiers",
    "drop_above_public",
    "effective_tiers",
    "is_publishable",
    "publishignore_lines",
    "render_publishignore",
]
