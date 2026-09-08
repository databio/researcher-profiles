"""``profile.jsonld``: the :class:`ProfileDocument` root and the field-group
classification that says who may write each of its fields.
"""

import warnings
from typing import Any

from pydantic import Field, field_validator, model_validator

# Shared biographical types from scholarcore.
from scholarcore import CareerEntry, Training
from scholarcore.identity import is_local, orcid_of

from ._common import FORMAT_HINT, KNOWN_PROVENANCE, ProfileLevel, Provenance, Visibility
from ._identity import validate_rid
from ._parts import Anchor, ArtifactRef, CareerStage, Identifier, PaperStats, ResearchOutput
from ._proof import Proof
from .jsonld import CONTEXT_URL, PROFILE_FORMAT_IRI, JsonLdModel


class ProfileDocument(JsonLdModel):
    """Top-level model for ``profile.jsonld``, a ``schema:Person`` node.

    This single document is simultaneously the format, the manifest, the
    crawler payload, and the LLM entry point.
    """

    context: str | None = Field(default=CONTEXT_URL, alias="@context")
    type_: str | None = Field(default="Person", alias="@type")
    conforms_to: str = Field(default=PROFILE_FORMAT_IRI, alias="conformsTo")

    name: str = Field(..., min_length=1)
    #: The identity of this profile. Never inferred from a directory name.
    rid: str = Field(..., description="Researcher id: a canonical ORCID or a local: id")
    #: Who asserted this and on what basis. No default; see :data:`Provenance`.
    provenance: Provenance
    #: ISO-8601 timestamp of the ORCID round-trip check. Required by, and only
    #: meaningful for, ``provenance == "orcid_verified"``. Retained as the
    #: ``orcid_roundtrip`` proof's timestamp under the multi-proof model.
    verified_at: str | None = Field(default=None, alias="verifiedAt")
    #: Verification proofs (``rp:proof``). Optional and multi-proof: a
    #: profile may carry zero, one, or many. See :class:`Proof`.
    proof: list[Proof] = Field(default=[])
    #: Reuse terms for the published record (an IRI, e.g. a CC licence).
    license_: str | None = Field(default=None, alias="license")
    #: The published profile URL: where this document is served from.
    url: str | None = None
    date_modified: str | None = Field(default=None, alias="dateModified")

    #: Profile-level default privacy tier. Artifacts inherit it when they do not
    #: declare their own ``visibility``. Set to ``internal``/``restricted`` to
    #: hold a whole profile back. This replaces the ``.visibility.json`` sidecar.
    visibility: Visibility = "public"

    #: Consumer-interface flags: read once at stage 1 so a consumer picks a
    #: navigation path without probing for artifacts. Carried over from the
    #: former published manifest.
    has_citation_graph: bool | None = Field(default=None, alias="hasCitationGraph")
    has_embedding_index: bool | None = Field(default=None, alias="hasEmbeddingIndex")
    #: True when expertise.md cites published paper_ids in square brackets.
    expertise_cites_paper_ids: bool | None = Field(default=None, alias="expertiseCitesPaperIds")

    level: ProfileLevel = "full"
    affiliation: str | None = None
    #: A ROR IRI for :attr:`affiliation`. Not an on-disk key of its own: when
    #: set, ``affiliation`` serializes as an ``Organization`` node carrying it.
    affiliation_id: str | None = None
    job_title: str | None = Field(default=None, alias="jobTitle")
    email: str | None = None
    field: str | None = None
    #: Free-text descriptive labels, not slugs: case and punctuation carry
    #: meaning ("COVID-19", "C. elegans systems biology", "ATAC-seq"). These
    #: are display strings everywhere they are consumed; derive a slug at
    #: index time if one is ever needed, and leave the label alone.
    subfields: list[str] = []
    summary: str | None = None

    same_as: list[str] = Field(default=[], alias="sameAs")
    identifier: list[Identifier] = []

    training: list[Training] = []
    career: list[CareerEntry] = []
    expertise: list[str] = []
    interests: list[str] = []
    not_interests: list[str] = []
    methodological_commitments: list[str] = []
    recurring_positions: list[str] = []
    intellectual_lineage: list[str] = []
    critiques: list[str] = []
    research_outputs: list[ResearchOutput] = Field(default=[], alias="researchOutputs")
    # Collaborators are sometimes a list of strings, sometimes a list of dicts
    # ({name, affiliation, relationship}). Keep tolerant.
    collaborators: list[str | dict] = []
    anchor: Anchor | None = None
    paper_stats: PaperStats | None = None
    #: Date-anchored eligibility facts. LLM-synthesized (requires judgment
    #: over CV/ORCID/web); ``None`` on profiles not yet re-synthesized.
    career_stage: CareerStage | None = None

    #: The manifest. Every artifact in the profile, typed and relatively
    #: linked, so an agent landing on a published directory never has to guess.
    has_part: list[ArtifactRef] = Field(default=[], alias="hasPart")
    #: Persona documents (SOUL / expertise): things about the person rather
    #: than parts of the record.
    subject_of: list[ArtifactRef] = Field(default=[], alias="subjectOf")

    # --- validation -------------------------------------------------------

    @field_validator("conforms_to")
    @classmethod
    def _validate_conforms_to(cls, v: str) -> str:
        if v != PROFILE_FORMAT_IRI:
            raise ValueError(
                f"conformsTo is {v!r}, expected {PROFILE_FORMAT_IRI!r}. This is "
                f"the format gate: {FORMAT_HINT}"
            )
        return v

    @field_validator("rid")
    @classmethod
    def _validate_rid(cls, v: str) -> str:
        return validate_rid(v)

    @field_validator("provenance")
    @classmethod
    def _warn_unknown_provenance(cls, v: str) -> str:
        """Tolerate an unknown headline label; warn rather than reject.

        ``provenance`` is an OPEN set (see :data:`KNOWN_PROVENANCE`). Rejecting
        an unrecognized value would violate the consumer rule in
        docs/rp-spec/index.md, so a stranger's newer label must load. It is
        still non-empty and structural constraints still bind for the values
        this version *does* understand (see :meth:`_check_provenance`).
        """
        if v not in KNOWN_PROVENANCE:
            warnings.warn(
                f"provenance {v!r} is not one of the known values "
                f"{sorted(KNOWN_PROVENANCE)}; loading it anyway (unknown values "
                "are tolerated, not rejected), but no tier-specific checks apply",
                stacklevel=2,
            )
        return v

    @field_validator("affiliation", mode="before")
    @classmethod
    def _flatten_affiliation(cls, v: Any) -> Any:
        """Accept either a plain string or an ``Organization`` node."""
        if isinstance(v, dict):
            return v.get("name")
        return v

    @model_validator(mode="before")
    @classmethod
    def _lift_affiliation_id(cls, data: Any) -> Any:
        """Carry a typed affiliation's ``@id`` (a ROR IRI) into a side field."""
        if isinstance(data, dict):
            aff = data.get("affiliation")
            if isinstance(aff, dict) and aff.get("@id") and not data.get("affiliation_id"):
                data = {**data, "affiliation_id": aff["@id"]}
        return data

    @model_validator(mode="after")
    def _check_provenance(self) -> "ProfileDocument":
        """Enforce what each provenance tier is allowed to claim."""
        if self.provenance == "orcid_verified":
            if is_local(self.rid):
                raise ValueError(
                    "provenance 'orcid_verified' requires an ORCID rid, but "
                    f"rid is {self.rid!r} (local). A local id is never an "
                    "ORCID-verified identity."
                )
            expected = f"https://orcid.org/{self.rid}"
            if self.id_ != expected:
                raise ValueError(
                    f"provenance 'orcid_verified' requires @id == {expected!r}, got {self.id_!r}"
                )
            if not self.url:
                raise ValueError(
                    "provenance 'orcid_verified' requires a url: the claim is "
                    "that the ORCID record's website list contains this "
                    "profile's URL, and without the URL there is nothing to "
                    "round-trip against"
                )
            if not self.verified_at:
                raise ValueError(
                    "provenance 'orcid_verified' requires verifiedAt (when the "
                    "ORCID round-trip was checked)"
                )
        elif self.provenance in ("synthetic", "historical"):
            if not is_local(self.rid):
                raise ValueError(
                    f"provenance {self.provenance!r} requires a local: rid; "
                    f"got {self.rid!r}. Neither a synthetic agent nor a "
                    "historical figure holds an ORCID."
                )
        if self.id_ is None:
            # The classic self-referential fragment: resolves against wherever
            # the document is served, so an unpublished profile still has a
            # well-formed subject IRI.
            self.id_ = self.url or "#me"
        return self

    # --- derived ------------------------------------------------------

    @property
    def orcid(self) -> str | None:
        """The profile's ORCID, derived from ``rid``; ``None`` when local.

        Read-only and derived, so the "two identity fields disagree" failure
        mode is structurally impossible. Not serialized: the
        published document already carries the ORCID twice (as ``rid`` and, for
        an ORCID identity, as ``@id``), and a third copy on disk is exactly the
        drift ``rid`` was invented to eliminate.
        """
        return orcid_of(self.rid)

    @property
    def openalex_id(self) -> str | None:
        """The OpenAlex id from :attr:`identifier`, if one is recorded."""
        for ident in self.identifier:
            if ident.property_id == "openalex":
                return ident.value
        return None

    @property
    def scholar_url(self) -> str | None:
        """The Google Scholar URL from :attr:`same_as`, if one is recorded."""
        for url in self.same_as:
            if "scholar.google" in url:
                return url
        return None

    # --- serialization ------------------------------------------------

    def _jsonld_node(self, data: dict[str, Any]) -> dict[str, Any]:
        aff_id = data.pop("affiliation_id", None)
        if aff_id and data.get("affiliation"):
            data["affiliation"] = {
                "@type": "Organization",
                "@id": aff_id,
                "name": data["affiliation"],
            }
        return data


# ---------------------------------------------------------------------------
# Field-group classification
# ---------------------------------------------------------------------------
#
# Every ProfileDocument field belongs to exactly one group. The groups define
# ownership boundaries: who may write each field, and where it comes from.
#
#   anchor      Python-owned identity. Set by the build tool, never LLM-writable.
#   synthesized The LLM's output surface. These are the only fields the LLM
#               is asked to produce; the generator enforces that.
#   derived     Python-computed from the corpus. The LLM must never author these:
#               LLM-authored statistics are actively wrong, not merely redundant.
#   document    JSON-LD infrastructure, document metadata, manifest, and
#               externally-assembled identifiers. Managed by the build tool and the format.

ANCHOR_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "rid",
        "affiliation",
        "anchor",
        "level",
    }
)

DERIVED_FIELDS: frozenset[str] = frozenset(
    {
        "paper_stats",
    }
)

#: JSON-LD infrastructure, document metadata, manifest, external identifiers,
#: and fields assembled from ORCID by the build tool (not LLM-written).
DOCUMENT_FIELDS: frozenset[str] = frozenset(
    {
        "context",
        "id_",
        "type_",
        "conforms_to",
        "provenance",
        "verified_at",
        "proof",
        "license_",
        "url",
        "date_modified",
        "visibility",
        "has_citation_graph",
        "has_embedding_index",
        "expertise_cites_paper_ids",
        "same_as",
        "identifier",
        "has_part",
        "subject_of",
        "affiliation_id",
        "job_title",
        "email",
    }
)
