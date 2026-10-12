"""Vocabulary every schema module shares: the format hint, the depth and
privacy tiers, the provenance labels, and the tolerant base for nested nodes.

The leaf of the ``schema`` package: it imports nothing from its siblings.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, WithJsonSchema

#: The error text naming the one on-disk format.
FORMAT_HINT = "a profile is profile.jsonld + sources/papers.jsonld"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

# Profile depth tier, ordered by cost, capability, and required inputs:
#   lite -> public bibliometric identity + works + abstract index; no LLM,
#           no PDFs, no persona. Name + rid is sufficient.
#   full -> the complete build (summaries, expertise, SOUL, persona).
#           Name + rid is still sufficient.
#   deep -> full + exhaustive corpus + supplied private resources (grant
#           records, a CV, website URLs). A deep build with none configured
#           must fail loudly.
# `level` is written explicitly on every profile; absent does not mean full.
ProfileLevel = Literal["lite", "full", "deep"]

#: Privacy tier, most to least permissive. Declared in the profile (on each
#: artifact and as a profile-level default) so a profile is self-describing and
#: a dumb sync is a correct sync.
#:
#: ``public``   anyone may access; served on the open web, syncs anywhere.
#: ``limited``  only readers the owner or host has granted access (named apps,
#:              keys, collaborators, or a host-defined signed-in group).
#: ``private``  only the owner and agents acting for the owner; never served
#:              to anyone else.
#:
#: Order: ``public < limited < private``. See docs/rp-spec/privacy.md for the
#: mapping to the EU access-right, COAR and ORCID vocabularies.
VisibilityTier = Literal["public", "limited", "private"]

#: Retired tier names. Rejected, never aliased.
RENAMED_TIERS: dict[str, str] = {"internal": "limited", "restricted": "private"}


def reject_renamed_tier(value: Any) -> Any:
    """Fail a retired tier name with a message that names its replacement."""
    if isinstance(value, str) and value in RENAMED_TIERS:
        raise ValueError(
            f"visibility '{value}' was renamed to '{RENAMED_TIERS[value]}' (rp spec 2026-10)"
        )
    return value


Visibility = Annotated[VisibilityTier, BeforeValidator(reject_renamed_tier)]

#: The JSON key that carries an artifact's ``dcterms:accessRights`` when it is
#: served. Derived, never stored; see :data:`ACCESS_RIGHTS_IRI`.
ACCESS_RIGHTS_KEY = "accessRights"

#: Each tier's term in the EU Publications Office access-right table, the
#: vocabulary DCAT-AP uses for ``dcterms:accessRights``. A close match, not an
#: exact one; docs/rp-spec/privacy.md has the full mapping.
_EU_ACCESS_RIGHT = "http://publications.europa.eu/resource/authority/access-right/"
ACCESS_RIGHTS_IRI: dict[VisibilityTier, str] = {
    "public": _EU_ACCESS_RIGHT + "PUBLIC",
    "limited": _EU_ACCESS_RIGHT + "RESTRICTED",
    "private": _EU_ACCESS_RIGHT + "NON_PUBLIC",
}

#: Ordered most-permissive -> least-permissive. Index = restrictiveness rank.
_VISIBILITY_ORDER: tuple[VisibilityTier, ...] = ("public", "limited", "private")

#: Default tier by manifest ``role`` when the artifact does not declare its own.
#: Everything unlisted defaults to ``public`` (authored, servable content).
ROLE_DEFAULT_VISIBILITY: dict[str, Visibility] = {
    # Supplied private inputs, private by default but freely re-tierable by
    # the owner. ``paper_fulltext`` defaults to ``private`` so nothing
    # silently becomes public; it is a default, not a floor.
    "paper_fulltext": "private",
    "cv": "private",
    "web": "private",
    # The interview digest is the researcher's own private account of their
    # work; the public documents derived from it carry a disclosure line
    # instead, so the digest itself never needs to reach anyone but the owner.
    "interview": "private",
    # Grant-derived embedding text. The singular chunk source_type, used only
    # for chunk tiers; the public ``grants`` collection role is not this.
    "grant": "private",
    # Build-local sqlite index; the servable embeddings are the flat artifacts.
    "embedding_index_sqlite": "private",
    # Trial participation is site and patient-adjacent detail, so it starts
    # limited. The ``clinical_expertise`` narrative is authored for publication.
    "trials": "limited",
}

#: No role carries a hard privacy floor: every artifact's tier is the owner's
#: to choose. Defaults live in :data:`ROLE_DEFAULT_VISIBILITY`.
ALWAYS_PRIVATE_ROLES: frozenset[str] = frozenset()


def role_default_visibility(role: str | None) -> Visibility:
    """The default tier for a manifest ``role`` (``public`` when unlisted)."""
    return ROLE_DEFAULT_VISIBILITY.get(role or "", "public")


def most_restrictive(*tiers: str | None) -> Visibility:
    """Return the most restrictive of the given tiers (``public`` if none).

    The derivation rule: a summary of a ``private`` paper is not ``public``
    merely because nobody marked it.
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
#: ``provenance`` is a single coarse "headline" trust label for crawlers.
#: Fine-grained, multi-source evidence lives in :class:`Proof`.
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

#: An open set, so a plain string: unknown values warn, not fail, per the
#: consumer rule in docs/rp-spec/index.md. :data:`KNOWN_PROVENANCE` is the
#: recognized set.
Provenance = str


# ---------------------------------------------------------------------------
# Published-schema overrides for fields whose on-disk shape is not their
# Python type
# ---------------------------------------------------------------------------

# Some fields hold a flat Python value but are written as a JSON-LD node or
# typed literal. Without these annotations the generated JSON Schema would
# describe the Python type and reject every document this package writes.
# Each lists the published form first, then the flat form it also accepts.

_NULL: dict[str, Any] = {"type": "null"}


def _named_node(node_type: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": f"A {node_type} node.",
        "properties": {
            "@type": {"type": "string"},
            "@id": {"type": "string"},
            "name": {"type": "string"},
        },
        "additionalProperties": True,
    }


#: ``datePublished``: an ``xsd:gYear`` string on disk (``"2024"``); an int in Python.
GYear = Annotated[
    int | None,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "string", "pattern": r"^\s*\d+\s*$"},
                {"type": "integer"},
                _NULL,
            ]
        }
    ),
]


def _node_or_name(node_type: str) -> WithJsonSchema:
    return WithJsonSchema({"anyOf": [_named_node(node_type), {"type": "string"}, _NULL]})


#: ``isPartOf``: a ``Periodical`` node on disk; the journal name in Python.
PeriodicalName = Annotated[str | None, _node_or_name("Periodical")]

#: ``affiliation`` / ``funder``: an ``Organization`` node on disk; the name in Python.
OrganizationName = Annotated[str | None, _node_or_name("Organization")]

#: ``author``: ``Person`` nodes on disk; a list of names in Python.
PersonList = Annotated[
    list[str] | None,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "array", "items": {"anyOf": [_named_node("Person"), {"type": "string"}]}},
                _NULL,
            ]
        }
    ),
]

#: ``about``: an ``{"@id": ...}`` reference on disk; the bare IRI in Python.
IdRef = Annotated[
    str | None,
    WithJsonSchema(
        {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {"@id": {"type": "string"}},
                    "additionalProperties": True,
                },
                {"type": "string"},
                _NULL,
            ]
        }
    ),
]


class _Base(BaseModel):
    """Tolerant base for nested value objects (not standalone documents).

    ``extra="allow"``: unknown terms are ignored, not rejected
    (``docs/rp-spec/index.md``; ``validate.py`` rule D6), at every depth.
    :func:`validate.undeclared_terms` reports them.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )
