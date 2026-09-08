"""Vocabulary every schema module shares: the format hint, the depth and
privacy tiers, the provenance labels, and the tolerant base for nested nodes.

This is the leaf of the ``schema`` package: it imports nothing from its
siblings, so every model module can depend on it without a cycle.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

#: The error text naming the one on-disk format.
FORMAT_HINT = "a profile is profile.jsonld + sources/papers.jsonld"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

# Profile depth tier. A single ordered axis of increasing cost and
# capability, and also an input-requirements hierarchy:
#   lite -> public bibliometric identity + works + abstract index; no LLM,
#           no PDFs, no persona. Name + rid is sufficient.
#   full -> today's complete build (summaries, expertise, SOUL, persona).
#           Name + rid is still sufficient.
#   deep -> full + exhaustive corpus + supplied private resources (grant
#           records, a CV, website URLs). These cannot be discovered through
#           public APIs; a deep build with none configured must fail loudly.
# `level` is written explicitly on every profile; there is no "absent means
# full" fallback chain.
ProfileLevel = Literal["lite", "full", "deep"]

#: Privacy tier, most to least permissive. Declared IN the profile (on each
#: artifact and as a profile-level default) so a profile is self-describing and
#: a dumb sync is a correct sync, with no registry-side ``.visibility.json`` sidecar.
#:
#: ``public``      served on the open web; syncs anywhere.
#: ``internal``    lab-visible only; syncs to authenticated/internal hosts.
#: ``restricted``  never leaves the machine; syncs nowhere.
Visibility = Literal["public", "internal", "restricted"]

#: Ordered most-permissive -> least-permissive. Index = restrictiveness rank.
_VISIBILITY_ORDER: tuple[Visibility, ...] = ("public", "internal", "restricted")

#: Default tier by manifest ``role`` when the artifact does not declare its own.
#: Everything unlisted defaults to ``public`` (authored, servable content).
ROLE_DEFAULT_VISIBILITY: dict[str, Visibility] = {
    # Supplied private inputs, never public by default.
    "paper_fulltext": "restricted",
    "cv": "restricted",
    "web": "restricted",
    # Grant-derived embedding text (title + abstract) is restricted. This is the
    # singular chunk source_type produced by ``chunk_grant``; the manifest
    # collection role for grants is the plural ``grants`` (a public bibliographic
    # record), which no artifact resolves through this map. So this entry governs
    # only embedding-chunk tier resolution and makes cv/web/grant/paper_fulltext-
    # derived chunks all drop out of the public export by the one derivation rule.
    "grant": "restricted",
    # Build-local sqlite index; the servable embeddings are the flat artifacts.
    "embedding_index_sqlite": "restricted",
}

#: Roles whose tier is a legal constraint, not a preference: extracted full text
#: of copyrighted papers may never be lowered below ``restricted``.
ALWAYS_RESTRICTED_ROLES: frozenset[str] = frozenset({"paper_fulltext"})


def role_default_visibility(role: str | None) -> Visibility:
    """The default tier for a manifest ``role`` (``public`` when unlisted)."""
    if role in ALWAYS_RESTRICTED_ROLES:
        return "restricted"
    return ROLE_DEFAULT_VISIBILITY.get(role or "", "public")


def most_restrictive(*tiers: str | None) -> Visibility:
    """Return the most restrictive of the given tiers (``public`` if none).

    This is the derivation rule: an artifact's effective tier is the most
    restrictive of its own tier and those of everything it was derived from. A
    summary of a ``restricted`` paper is not ``public`` merely because nobody
    marked it.
    """
    rank = 0
    for t in tiers:
        if t in _VISIBILITY_ORDER:
            rank = max(rank, _VISIBILITY_ORDER.index(t))  # type: ignore[arg-type]
    return _VISIBILITY_ORDER[rank]


#: Who asserted this profile, and on what basis. Required, with no default: an
#: unlabeled published assertion about a real person is exactly the failure
#: mode this field exists to prevent.
#:
#: ``orcid_verified``  the ORCID record round-trips (its website list contains
#:                     this profile's ``url``). The only tier that may claim
#:                     the subject endorsed the profile.
#: ``self_published``  the subject published it about themselves.
#: ``third_party``     someone built it about someone else (what a build
#:                     tool ordinarily emits).
#: ``synthetic``       not a real person (an AI agent, a test fixture).
#: ``historical``      a real person who cannot hold an ORCID.
#: ``domain_verified`` control of the serving/linked site was proven (a
#:                     ``.well-known`` challenge). See :class:`Proof`.
#: ``key_signed``      the document carries a cryptographic signature from a key
#:                     the subject controls. See :class:`Proof` / :mod:`signing`.
#: ``institution_verified`` (reserved) an institution vouched via SSO/email.
#:
#: ``provenance`` is a single coarse "headline" trust label for crawlers. It is
#: an open set: the values below are the ones this reference implementation
#: understands, but, matching the consumer rule in docs/rp-spec/index.md
#: ("Consumers MUST ignore unknown enum values rather than treating them as
#: validation failures"), an unrecognized value is tolerated (warned, not
#: rejected). Fine-grained, multi-source evidence lives in :class:`Proof`.
KNOWN_PROVENANCE: frozenset[str] = frozenset(
    {
        "orcid_verified",
        "self_published",
        "third_party",
        "synthetic",
        "historical",
        "domain_verified",
        "key_signed",
        "institution_verified",
    }
)

#: Provenance is a plain string, not a closed ``Literal``: the format
#: standardizes the envelope for a proof, not the identity source, so the set
#: of headline labels must stay open. :data:`KNOWN_PROVENANCE` is the recognized
#: set; unknown values warn (see :meth:`ProfileDocument._warn_unknown_provenance`).
Provenance = str


class _Base(BaseModel):
    """Tolerant base for nested value objects (not standalone documents).

    ``extra="allow"``, matching :class:`JsonLdModel` and the format's stated
    posture: unknown terms are ignored, not rejected (``docs/rp-spec/index.md``;
    ``validate.py`` rule D6). Nested nodes get no carve-out: a
    publisher may extend a ``ResearchOutput`` exactly as it may extend the
    document root.

    Drift is *reported*, not enforced here: :func:`validate.undeclared_terms`
    walks nested objects and names every unmodeled key. Enforcement belongs at
    the generation site, not on *load*: forbidding here only stopped this
    package reading data its own writers had produced.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )
