"""Privacy tiers, the derivation rule, and the document projection.

Privacy is data *declared in the profile*: a tier on every artifact, a tier on
every inline section, and a profile-level default. Nothing on disk encodes an
audience. Every egress (the HTTP routes, the viewer archive, ``rp publish``)
projects the profile for one viewer tier at the moment it leaves.

The single authority is :func:`effective_tiers`: the effective tier of an
artifact is the most restrictive of the profile default, the artifact's own
tier, and the tiers of everything it was derived from. Everything calls it:
the static export, the embedding-chunk exporter, the validator. There is no
second implementation.
"""

from dataclasses import dataclass, field
from typing import Iterable

from .schema import (
    _VISIBILITY_ORDER,
    ACCESS_RIGHTS_IRI,
    ACCESS_RIGHTS_KEY,
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

SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    "summary": ("summary",),
    "expertise": ("expertise",),
    "focus": ("subfields", "interests", "not_interests", "research_interests"),
    "methods": ("methodological_commitments",),
    #: SOUL is an artifact, not an inline field, so it governs no document
    #: keys: the tier reaches ``personality/SOUL.md`` through the manifest.
    #: It is listed anyway so :func:`section_tiers` reports a row for it and an
    #: owner-facing privacy control can offer SOUL beside the other sections
    #: instead of silently omitting the one section they most expect to see.
    "soul": (),
    "clinical": ("therapeutic_areas",),
    "site_capabilities": ("site_capabilities",),
    "regulatory_experience": ("regulatory_experience",),
    "contact": ("email",),
    "background": ("training", "career", "job_title", "affiliation"),
}

#: Map an embedding chunk's ``source_type`` to the manifest ``role`` whose
#: default tier governs it. For every source the two names coincide today; the
#: map is written explicitly so the chunk -> role -> tier derivation is
#: auditable rather than incidental. ``cv``/``web``/``grant`` resolve to
#: ``private`` by :data:`schema.ROLE_DEFAULT_VISIBILITY`; the rest are public.
CHUNK_SOURCE_TYPE_ROLE: dict[str, str] = {
    "soul": "soul",
    "expertise": "expertise",
    "paper_summary": "paper_summary",
    "paper_abstract": "paper_abstract",
    "cv": "cv",
    "web": "web",
    "grant": "grant",
}

#: Directory prefixes (relative to the profile content root) that no export
#: ships at any tier: profile-adjacent serve-time caches and local key
#: material. ``.cache/`` holds derived caches (``embeddings.sqlite``,
#: ``topics.json``, ...) that are regenerable and never part of the published
#: record. Build-session bookkeeping is not a concern here: it lives in the
#: build root outside the content tree entirely.
ALWAYS_PRIVATE_PREFIXES: tuple[str, ...] = (".cache/", ".keys/")


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


def section_tiers(profile: ProfileDocument) -> dict[str, Visibility]:
    """Effective tier of every declared inline section."""
    declared = {x.section: x.visibility for x in profile.section_visibility}
    return {
        section: most_restrictive(profile.visibility, declared.get(section, "public"))
        for section in SECTION_FIELDS
    }


def project_document(profile: ProfileDocument, viewer: ViewerTier) -> ProfileDocument:
    """Remove inline fields whose section tier exceeds ``viewer``.

    The whole-document counterpart of :func:`effective_tiers`: that one decides
    which *files* a viewer may fetch, this one decides which *fields inside the
    document* they may read. Both are called from the shared payload
    projection, so an inline section held back over HTTP is held back in the
    published site, the archive and the export too.
    """
    tiers = section_tiers(profile)
    projected = profile.model_copy(deep=True)
    for section, fields in SECTION_FIELDS.items():
        if not tier_allows(viewer, tiers[section]):
            for name in fields:
                value = getattr(profile, name, None)
                setattr(projected, name, [] if isinstance(value, list) else None)
    _stamp_access_rights(projected)
    return projected


def access_rights(profile: ProfileDocument) -> dict[str, str]:
    """``contentUrl`` -> the EU access-right IRI of that entry's effective tier.

    What a served manifest entry's ``accessRights`` (``dcterms:accessRights``)
    says, so DCAT tools read the tier without rp's own vocabulary. Derived at
    egress and dropped on load (:class:`~researcher_profiles.schema.ArtifactRef`),
    so it is never stored and cannot go stale. A ``derivedFrom`` cycle has no
    effective tier: the answer is then empty, entries carry no label rather than
    a wrong one, and ``rp validate`` reports the cycle.
    """
    try:
        effective = effective_tiers(profile)
    except DerivationCycleError:
        return {}
    return {url: ACCESS_RIGHTS_IRI[tier] for url, tier in effective.items()}


def label_access_rights(document: dict, profile: ProfileDocument) -> None:
    """Add ``accessRights`` to every manifest entry of a serialized ``document``.

    For egress that serves the stored JSON rather than a re-dumped model.
    """
    rights = access_rights(profile)
    for slot in ("hasPart", "subjectOf"):
        for entry in document.get(slot) or []:
            iri = rights.get(entry.get("contentUrl"))
            if iri is not None:
                entry[ACCESS_RIGHTS_KEY] = iri


def _stamp_access_rights(profile: ProfileDocument) -> None:
    """:func:`label_access_rights` for a model: set on each entry's extras."""
    rights = access_rights(profile)
    for ref in [*profile.has_part, *profile.subject_of]:
        iri = rights.get(ref.content_url)
        if iri is not None and ref.__pydantic_extra__ is not None:
            ref.__pydantic_extra__[ACCESS_RIGHTS_KEY] = iri


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
    #: After the profile default and the derivation rule.
    effective: Visibility = "public"
    #: Concrete causes that held the artifact above its declared tier: the
    #: specific source, not a restatement of the rule.
    raised_by: list[str] = field(default_factory=list)
    #: ``derivedFrom`` entries that name no artifact in this manifest. A
    #: dependency nobody can resolve is not a dependency at ``public``: it is a
    #: hole in the derivation graph, reported by :func:`derivation_errors` and
    #: by ``rp validate``.
    unresolved: list[str] = field(default_factory=list)


class DerivationCycleError(ValueError):
    """``derivedFrom`` forms a cycle, so no artifact in it has a tier.

    The derivation rule is defined over a DAG: an artifact is at least as
    private as everything it came from. A cycle makes that rule
    self-referential, and guessing a tier for a document whose own declaration
    is incoherent is exactly the silent widening privacy projection exists to
    prevent. Raised rather than swallowed: every caller
    (the static export, the manifest read, the preview) would otherwise
    publish a tier nothing supports.
    """


def _derivation_graph(profile: ProfileDocument) -> tuple[list, dict[str, list], dict[str, list]]:
    """The manifest plus its ``paperId`` and ``role`` resolution indexes."""
    parts = list(profile.has_part) + list(profile.subject_of)
    by_paper: dict[str, list] = {}
    by_role: dict[str, list] = {}
    for p in parts:
        if p.paper_id:
            by_paper.setdefault(p.paper_id, []).append(p)
        if p.role:
            by_role.setdefault(p.role, []).append(p)
    return parts, by_paper, by_role


def explain_tiers(profile: ProfileDocument) -> dict[str, TierExplanation]:
    """``{contentUrl: TierExplanation}`` for every manifest artifact.

    Effective tier = most restrictive of the profile default, the artifact's
    own declared tier, and the *effective* tiers of everything in its
    ``derivedFrom`` (resolved by ``paperId`` or by ``role``). Effective, not
    declared: the restriction is transitive, so a public summary of a public
    digest of a private CV is private. Resolution is a memoized
    depth-first walk, and a cycle raises :class:`DerivationCycleError` rather
    than settling on whichever tier the walk happened to reach first.

    Alongside each decision this records which of those causes is holding the
    artifact where it is, so an interface can name the cause on the row rather
    than explaining the rule in prose, plus any ``derivedFrom`` entry that
    resolved to nothing.
    """
    parts, by_paper, by_role = _derivation_graph(profile)
    default = profile.visibility

    #: contentUrl -> effective tier, filled in as the walk returns.
    memo: dict[str, Visibility] = {}
    #: Causes and unresolved references per artifact, recorded by the same
    #: walk that decides the tier, so the sentence and the decision are one
    #: computation.
    notes: dict[str, tuple[list[tuple[Visibility, str]], list[str]]] = {}

    def resolve(part, visiting: tuple[str, ...]) -> Visibility:
        url = part.content_url
        if url in memo:
            return memo[url]
        if url in visiting:
            chain = " -> ".join((*visiting[visiting.index(url) :], url))
            raise DerivationCycleError(
                f"derivedFrom forms a cycle: {chain}. An artifact cannot be "
                "derived from itself, directly or through a chain."
            )

        # The role default is a *default*, not a floor: it is written onto the
        # artifact's own ``visibility`` at load (ArtifactRef._apply_role_tier)
        # only when the owner did not declare one. So it is already folded into
        # ``part.visibility`` below when it applies, and an explicit declaration
        # wins. It is deliberately NOT re-added as a cause here, which would
        # re-floor a declared tier and make role-defaulted artifacts
        # (paper_fulltext, cv, web, grant, ...) unraisable by their owner.
        causes: list[tuple[Visibility, str]] = []
        unresolved: list[str] = []
        causes.append((default, f"the profile default ({default})"))
        for ref in part.derived_from or []:
            sources = by_paper.get(ref, []) + by_role.get(ref, [])
            if not sources:
                unresolved.append(ref)
                continue
            for src in sources:
                src_tier = resolve(src, (*visiting, url))
                causes.append((src_tier, f"derived from {src.content_url} ({src_tier})"))

        effective = most_restrictive(part.visibility, *(t for t, _ in causes))
        memo[url] = effective
        notes[url] = (causes, unresolved)
        return effective

    out: dict[str, TierExplanation] = {}
    for p in parts:
        effective = resolve(p, ())
        causes, unresolved = notes[p.content_url]
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
            raised_by=raised_by,
            unresolved=unresolved,
        )
    return out


def derivation_errors(profile: ProfileDocument) -> list[str]:
    """Every way this document's ``derivedFrom`` graph fails to resolve.

    One sentence per problem, in manifest order: a cycle (reported once, since
    it stops the walk) or a reference naming no artifact. ``rp validate``
    turns each into a cross-artifact violation; an empty list means every
    dependency resolved and :func:`explain_tiers` derived a tier from all of
    them.
    """
    try:
        explained = explain_tiers(profile)
    except DerivationCycleError as e:
        return [str(e)]
    return [
        f"{note.content_url} is derivedFrom {ref!r}, which names no artifact "
        "in this manifest (no matching paperId and no matching role)"
        for note in explained.values()
        for ref in note.unresolved
    ]


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
    to ``limited`` this way, because nobody has consented to publish it. Folded
    in with :func:`~researcher_profiles.schema.most_restrictive`, so a floor can
    only ever narrow.
    """
    return tier_allows(viewer, most_restrictive(profile.visibility, floor))


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
    come back ``private``; ``expertise``/``soul``/``paper_summary``/
    ``paper_abstract`` come back ``public`` (unless the whole profile is held
    back by its default). The embedding exporter drops every key whose tier
    exceeds its audience via :func:`tier_allows`.

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


__all__ = [
    "ALWAYS_PRIVATE_PREFIXES",
    "CHUNK_SOURCE_TYPE_ROLE",
    "DerivationCycleError",
    "TierExplanation",
    "ViewerTier",
    "derivation_errors",
    "access_rights",
    "explain_tiers",
    "narrow_viewer",
    "profile_visible",
    "tier_allows",
    "chunk_source_tiers",
    "effective_tiers",
    "label_access_rights",
    "SECTION_FIELDS",
    "project_document",
    "section_tiers",
]
