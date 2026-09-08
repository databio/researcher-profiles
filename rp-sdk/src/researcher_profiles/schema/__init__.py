"""Pydantic models for researcher-profiles on-disk artifacts.

The published record is JSON-LD:

- ``profile.jsonld``        -> :class:`ProfileDocument` (a ``schema:Person`` node)
- ``sources/papers.jsonld`` -> :class:`PapersDocument` of :class:`PaperRecord`
- ``sources/grants.jsonld`` -> :class:`GrantsDocument` of :class:`GrantRecord`

Build bookkeeping is not part of that record; it lives in
``.build/<slug>/meta/build_state.json`` (see :mod:`researcher_profiles.build_state`).

The format gate is ``conformsTo`` (a resolvable IRI), not an integer
``schema_version``. A document without it (which is what every pre-cutover
file looks like) fails to load, naming the one format it could have been.

**JSON-LD shape lives in the serializers, not in the attribute types.**
``p.affiliation`` is a ``str``; ``paper.year`` is an ``int``; ``paper.journal``
is a ``str``. Serialization turns those into an ``Organization`` node, an
``xsd:gYear`` string, and a ``Periodical`` node respectively, and validation
turns them back. Python callers never touch a node object they did not ask for.

How the schema package is laid out
==================================

Every name is imported from ``researcher_profiles.schema``; the modules below
are private and may move. ``_common`` is the leaf (the format hint, the depth
and privacy tiers, the provenance labels, the tolerant ``_Base``) and
``_identity`` wraps scholarcore's rid grammar with RP-specific hints. The
models sit on top of those two: ``_parts`` (the nested nodes of the profile and
the :class:`ArtifactRef` manifest entry), ``_proof`` (the verification envelope),
``_document`` (:class:`ProfileDocument` and its field groups) and ``_sources``
(the ``sources/`` sidecars: papers, grants, summary frontmatter).
"""

# Shared biographical and identity primitives from scholarcore, the one
# implementation. RP's wrappers over the rid grammar live in ``_identity``.
from scholarcore import CareerEntry, Training
from scholarcore.identity import LOCAL_RID_RE, is_local, is_rid, orcid_of

from ._common import _VISIBILITY_ORDER as _VISIBILITY_ORDER
from ._common import (
    ALWAYS_RESTRICTED_ROLES,
    FORMAT_HINT,
    KNOWN_PROVENANCE,
    ROLE_DEFAULT_VISIBILITY,
    ProfileLevel,
    Provenance,
    Visibility,
    most_restrictive,
    role_default_visibility,
)
from ._document import ANCHOR_FIELDS, DERIVED_FIELDS, DOCUMENT_FIELDS, ProfileDocument
from ._identity import (
    SLUG_RE,
    mint_local_rid,
    normalize_doi,
    validate_ref,
    validate_rid,
)
from ._identity import _slugify as _slugify
from ._parts import Anchor, ArtifactRef, CareerStage, Identifier, PaperStats, ResearchOutput
from ._proof import KNOWN_PROOF_KINDS, Proof
from ._sources import (
    GrantRecord,
    GrantsDocument,
    PaperRecord,
    PapersDocument,
    SummaryFile,
)

__all__ = [
    "ALWAYS_RESTRICTED_ROLES",
    "LOCAL_RID_RE",
    "SLUG_RE",
    "ANCHOR_FIELDS",
    "Anchor",
    "ArtifactRef",
    "CareerEntry",
    "CareerStage",
    "DERIVED_FIELDS",
    "DOCUMENT_FIELDS",
    "GrantRecord",
    "GrantsDocument",
    "Identifier",
    "FORMAT_HINT",
    "KNOWN_PROOF_KINDS",
    "KNOWN_PROVENANCE",
    "PaperRecord",
    "PaperStats",
    "PapersDocument",
    "ProfileDocument",
    "ProfileLevel",
    "Proof",
    "Provenance",
    "ROLE_DEFAULT_VISIBILITY",
    "ResearchOutput",
    "Visibility",
    "most_restrictive",
    "SummaryFile",
    "Training",
    "is_local",
    "is_rid",
    "mint_local_rid",
    "normalize_doi",
    "orcid_of",
    "role_default_visibility",
    "validate_ref",
    "validate_rid",
]
