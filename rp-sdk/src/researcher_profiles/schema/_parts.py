"""Nested value objects of ``profile.jsonld``: the nodes a
:class:`ProfileDocument` carries but that are not documents in their own right,
plus :class:`ArtifactRef`, the manifest entry.

Training and CareerEntry are the two exceptions: they come from scholarcore and
are re-exported by the package, not defined here.
"""

from typing import Literal

from pydantic import Field, model_validator

from ._common import ALWAYS_RESTRICTED_ROLES, Visibility, _Base, role_default_visibility
from .jsonld import JsonLdModel


class ResearchOutput(JsonLdModel):
    """The base type for every research output a researcher produces.

    An entry in the ``researchOutputs`` list (``rp:researchOutput``) is a
    ``ResearchOutput`` directly: software, a dataset, a protocol, a reagent, or
    another non-paper output. Papers are not stored here — they live in
    ``sources/papers.jsonld`` — but :class:`~.PaperRecord` is a *specialization*
    of this class, and future output kinds (grants, abstracts, presentations,
    patents, standards) are expected to specialize it too, adding their own
    type-specific fields on top of this shared core.

    It is a :class:`~.JsonLdModel`, not a plain nested value object, because a
    research output is a citable thing with an identity: a DOI, a repository
    URL, a grant number. ``@id`` and ``@type`` are therefore available on every
    output; both are optional, and a generic output needs neither.

    ``type`` is free text, not a closed enum. The recommended vocabulary is
    ``software``, ``dataset``, ``protocol``, ``reagent``, ``model``, ``grant``,
    ``abstract``, ``presentation``, ``patent``, ``standard`` (see
    ``docs/rp-spec/index.md``); a publisher may use any other value.
    """

    type: str
    name: str
    description: str | None = None
    url: str | None = None


class Anchor(_Base):
    """Disambiguation evidence (``rp:anchor``).

    ``sources`` and ``created_at`` are part of the evidence, not bookkeeping:
    the anchor's claim is "these public records were consulted and found
    consistent", which is unverifiable without knowing which records and when.
    """

    disambiguation_evidence: str | None = None
    confidence: str | None = None
    #: The URLs consulted when anchoring (ORCID person/employments, OpenAlex).
    sources: list[str] = []
    #: ISO-8601 timestamp of when the anchoring was performed.
    created_at: str | None = None


class PaperStats(_Base):
    """Author position statistics (``rp:paperStats``)."""

    first: int = 0
    last: int = 0
    middle: int = 0
    unknown: int = 0
    corresponding: int = 0
    total: int = 0
    year_min: int = 0
    year_max: int = 0


class CareerStage(_Base):
    """Date-anchored career-stage and eligibility facts (``rp:careerStage``).

    Stores anchor facts, not verdicts. Eligibility (NIH ESI, K99, NSF CAREER,
    ...) is always evaluated relative to an application due date, so the
    profile never records "is_esi: true". It records the dated facts an
    evaluator applies a NOFO's rule to at evaluation time. ``as_of`` is when
    the facts were assessed (distinct from the profile build date). ``None``
    and ``"unknown"`` are legal everywhere so an evaluator can say "can't
    determine" instead of guessing. ``sources_checked`` disambiguates the
    nulls: a null fact whose source category was checked is an established
    absence; a null fact whose category was never checked is unknown.

    Out of scope (use ``notes`` or ask the human): citizenship /
    visa status, postdoc research-months accounting, ESI extensions.
    """

    as_of: str = Field(
        description=(
            "ISO date on which these facts were assessed. not the profile "
            "build date. Every fact below is a claim as of this date."
        )
    )
    sources_checked: list[Literal["grants", "publications", "training", "employment"]] = Field(
        default=[],
        description=(
            "Source categories consulted when these facts were assessed, from "
            "'grants', 'publications', 'training', 'employment'. Disambiguates "
            "nulls: a null field whose category is listed here "
            "means the source was checked and nothing was found (established "
            "absence); a null field whose category is not listed means nobody "
            "looked. E.g. first_r01_equivalent_year null with 'grants' listed "
            "means no R01-equivalent award exists; null without 'grants' means "
            "grant records were never consulted."
        ),
    )
    terminal_degree_year: int | None = Field(
        default=None,
        description="Year the terminal degree was completed; null if unknown.",
    )
    terminal_degree_type: Literal["research", "clinical"] | None = Field(
        default=None,
        description=(
            "Whether the terminal degree is a research or clinical degree. NIH "
            "ESI runs from the later of terminal research degree and end of "
            "clinical training, so the distinction matters."
        ),
    )
    clinical_training_end_year: int | None = Field(
        default=None,
        description=(
            "Year residency/fellowship clinical training ended; null when the "
            "person had none or it is unknown."
        ),
    )
    first_independent_appointment_year: int | None = Field(
        default=None,
        description="Year of the first independent faculty/PI appointment.",
    )
    current_rank: (
        Literal[
            "assistant_professor",
            "associate_professor",
            "professor",
            "research_faculty",
            "staff_scientist",
            "postdoc",
            "student",
            "other",
        ]
        | None
    ) = Field(default=None, description="Current academic rank; null if unknown.")
    tenure_status: Literal["tenured", "untenured_tenure_track", "non_tenure_track", "unknown"] = (
        Field(
            description=(
                "Tenure status. Use 'unknown' unless the evidence states it. Do not "
                "infer it from rank, since assistant professors may be off the "
                "tenure track and research faculty usually are."
            )
        )
    )
    independence: Literal["independent", "mentored", "trainee", "unknown"] = Field(
        description=(
            "Whether the researcher currently runs an independent program, works "
            "under a mentor, or is still a trainee. Use 'unknown' when unclear."
        )
    )
    first_r01_equivalent_year: int | None = Field(
        default=None,
        description=(
            "First year as PD/PI of a substantial independent NIH award "
            "(R01, DP1, DP2, DP5, R37, RF1, RL1, U01, some R35). null means "
            "none found as of as_of (see sources_checked). K, R00, R03, R21, "
            "F and T awards and a one-year R56 do not count and must not be "
            "recorded here."
        ),
    )
    notes: str | None = Field(
        default=None,
        description=(
            "Free text for anything the fields cannot carry: eligibility "
            "extensions, career gaps, MPI nuances, or citizenship if the "
            "researcher volunteered it."
        ),
    )
    evidence: str = Field(
        description=(
            "One sentence naming the sources these facts came from, e.g. which "
            "ORCID employment records and training entries were used."
        )
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description="Overall confidence in the facts recorded here."
    )


class Identifier(JsonLdModel):
    """A ``schema:PropertyValue`` identifier node.

    Used for the identifiers that are not the profile's ``@id``: OpenAlex,
    Scopus, and whatever an older ``external_ids`` block carried.
    """

    type_: str | None = Field(default="PropertyValue", alias="@type")
    property_id: str = Field(alias="propertyID")
    value: str


class ArtifactRef(JsonLdModel):
    """One manifest entry: a typed link to one artifact (file) inside the profile.

    ``contentUrl`` is always a relative path, so a profile stays portable
    across servers. Copying the directory to a different host cannot break a
    single link.
    """

    type_: str | None = Field(default="DigitalDocument", alias="@type")
    name: str | None = None
    encoding_format: str | None = Field(default=None, alias="encodingFormat")
    content_url: str = Field(alias="contentUrl")
    role: str | None = None
    paper_id: str | None = Field(default=None, alias="paperId")
    #: Size and digest, so a consumer can budget/verify a fetch. Populated at
    #: write time.
    bytes: int | None = None
    sha256: str | None = None
    #: This artifact's declared privacy tier. When not explicitly set it
    #: defaults to the role default (:func:`role_default_visibility`); e.g. a
    #: ``cv``/``web`` artifact is ``restricted``. A ``paper_fulltext`` artifact
    #: is forced to ``restricted`` and cannot be lowered (a legal constraint).
    #: The *effective* tier also folds in ``derived_from``; see
    #: :func:`researcher_profiles.privacy.effective_tiers`.
    visibility: Visibility = "public"
    #: Roles or paper_ids this artifact was derived from. Its effective tier is
    #: the most restrictive of its own and its sources'.
    derived_from: list[str] = Field(default=[], alias="derivedFrom")

    @model_validator(mode="after")
    def _apply_role_tier(self) -> "ArtifactRef":
        # Full text of copyrighted papers is always restricted and non-overridable.
        if self.role in ALWAYS_RESTRICTED_ROLES:
            self.visibility = "restricted"
        # Otherwise, when the tier was not stated, inherit the role default.
        elif "visibility" not in self.model_fields_set:
            self.visibility = role_default_visibility(self.role)
        return self
