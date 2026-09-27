"""Nested value objects of ``profile.jsonld``: the nodes a
:class:`ProfileDocument` carries but that are not documents in their own right,
plus :class:`ArtifactRef`, the manifest entry.

Training and CareerEntry are the two exceptions: they come from scholarcore and
are re-exported by the package, not defined here.
"""

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, model_validator

from ._common import Visibility, _Base, role_default_visibility
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


class ConceptReference(JsonLdModel):
    """One term in a controlled vocabulary (``rp:concept``).

    The ``system``/``code``/``version`` triple is what makes a concept
    comparable across profiles; ``label`` is only what a human reads. Used by
    ``therapeutic_areas``; research interests use :class:`InterestConcept`.
    """

    system: str
    code: str
    label: str
    version: str | None = None


class InterestConcept(JsonLdModel):
    """The concept a :class:`ResearchInterest` points at.

    Two forms, never mixed. A **coded** concept names a term in a controlled
    vocabulary: ``system`` (the vocabulary URI), ``code``, ``display`` (the
    label snapshot a human reads), an optional pinned ``version``, and the
    term's own IRI as ``@id``. A **text-only** concept is a ``label`` with
    ``unmapped: true`` and no ``@id``, ``system`` or ``code``: an interest no
    vocabulary covers, kept rather than forced onto a wrong term.

    The field names follow FHIR ``Coding`` (``system``, ``code``,
    ``display``, ``version``), so a Coding view is a rename, not a mapping.
    This is a separate type from :class:`ConceptReference`, which stays the
    shape of ``therapeutic_areas``.
    """

    system: str | None = None
    code: str | None = None
    display: str | None = None
    version: str | None = None
    label: str | None = None
    unmapped: bool = False

    @model_validator(mode="after")
    def _coded_or_text(self) -> "InterestConcept":
        coded = self.system is not None and self.code is not None
        if self.unmapped:
            if coded or self.system or self.code or self.id_ or not self.label:
                raise ValueError("text-only concept needs label and no @id/system/code")
        elif not coded or not self.display:
            raise ValueError("coded concept needs system, code and display")
        return self

    def _jsonld_node(self, data: dict[str, Any]) -> dict[str, Any]:
        # ``unmapped`` is a claim only when true; ``false`` on every coded
        # concept would be noise.
        if not data.get("unmapped"):
            data.pop("unmapped", None)
        return data

    @property
    def text(self) -> str:
        """What a human reads: the label of a text-only concept, else ``display``."""
        return (self.label if self.unmapped else self.display) or ""

    @property
    def key(self) -> str:
        """Identity for precedence: ``system|code``, or the casefolded label."""
        if self.unmapped:
            return "text|" + (self.label or "").casefold()
        return f"{self.system}|{self.code}"


class InterestEvidence(JsonLdModel):
    """Why an inferred interest was proposed: raw counts live here, not in weight."""

    #: Work ids (OpenAlex ``W…`` or paper ids) that carry the concept.
    papers: list[str] = []
    #: Share of the profile's papers that carry the concept, in ``[0, 1]``.
    share: float | None = Field(default=None, ge=0, le=1)


#: Who said so. ``declared``: the person. ``inferred``: computed from papers or
#: written by a model. ``imported``: copied from another record (e.g. ORCID).
InterestMethod = Literal["declared", "inferred", "imported"]


class ResearchInterest(JsonLdModel):
    """One weighted person-to-concept link (``rp:ResearchInterest``).

    Modeled on the Weighted Interest Ontology pattern (person ->
    ``wi:WeightedInterest`` -> topic, with ``wi:weight``). ``weight`` is signed
    in ``[-1, 1]``: +1 a core interest, 0 declared neutral, -1 a hard exclude,
    and values between -1 and 0 mean rank lower. A missing weight means
    *unknown*, never 0, which is why an inferred entry built from paper counts
    carries its share in ``evidence`` and leaves ``weight`` unset.

    Several entries may cover one concept; :func:`effective_interests` picks
    the one that counts.
    """

    type_: str | None = Field(default="ResearchInterest", alias="@type")
    concept: InterestConcept
    weight: float | None = Field(default=None, ge=-1, le=1)
    method: InterestMethod
    #: What produced the entry: ``user``, ``llm``, ``prosopia-wizard``,
    #: ``openalex-topics@2026-09``, ...
    generator: str = Field(min_length=1)
    asserted_at: datetime = Field(alias="assertedAt")
    evidence: InterestEvidence | None = None


def effective_interests(entries: Iterable[ResearchInterest]) -> list[ResearchInterest]:
    """The one entry per concept that counts, in first-appearance order.

    Precedence: a ``declared`` entry beats an ``inferred`` or ``imported``
    one; among equals the newest ``assertedAt`` wins.
    """
    best: dict[str, ResearchInterest] = {}
    for e in entries:
        k = e.concept.key
        cur = best.get(k)
        if cur is None or _rank(e) > _rank(cur):
            best[k] = e
    return list(best.values())


def _rank(e: ResearchInterest) -> tuple[int, datetime]:
    at = e.asserted_at
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (1 if e.method == "declared" else 0, at)


#: The weight a free-text list entry gets when it becomes a typed entry:
#: "interested" / "less interested", the middle stops of the scale.
TEXT_INTEREST_WEIGHT = 0.5


def interests_from_text(
    interests: Iterable[str],
    not_interests: Iterable[str] = (),
    *,
    generator: str,
    method: InterestMethod,
    asserted_at: datetime | None = None,
) -> list[ResearchInterest]:
    """Text-only entries for two free-text lists (+0.5 and -0.5).

    The one conversion every builder uses, so an LLM's ``interests`` /
    ``not_interests`` become the same typed entries whichever tool wrote them.
    Blank and repeated labels (case-insensitive) are dropped.
    """
    at = asserted_at or datetime.now(timezone.utc).replace(microsecond=0)
    out: list[ResearchInterest] = []
    seen: set[str] = set()
    for labels, weight in (
        (interests, TEXT_INTEREST_WEIGHT),
        (not_interests, -TEXT_INTEREST_WEIGHT),
    ):
        for raw in labels:
            label = (raw or "").strip()
            if not label or label.casefold() in seen:
                continue
            seen.add(label.casefold())
            out.append(
                ResearchInterest(
                    concept=InterestConcept(label=label, unmapped=True),
                    weight=weight,
                    method=method,
                    generator=generator,
                    assertedAt=at,
                )
            )
    return out


def merge_generated_interests(
    existing: Iterable[ResearchInterest | dict[str, Any]],
    generated: Iterable[ResearchInterest],
    *,
    generator_prefix: str,
) -> list[ResearchInterest]:
    """Replace one generator's entries, keeping every other entry.

    A rebuild regenerates only what it generates: entries whose ``generator``
    starts with ``generator_prefix`` (``llm``, ``openalex-topics``) are dropped
    and ``generated`` appended; everything else, and every declared entry
    whatever its generator, survives.
    """
    kept: list[ResearchInterest] = []
    for e in existing:
        entry = e if isinstance(e, ResearchInterest) else ResearchInterest.model_validate(e)
        if entry.method != "declared" and entry.generator.startswith(generator_prefix):
            continue
        kept.append(entry)
    return kept + list(generated)


def project_interests(entries: Iterable[ResearchInterest]) -> tuple[list[str], list[str]]:
    """``(interests, not_interests)``: labels of effective entries by weight sign.

    Positive weights are interests, negative weights not-interests; a missing
    weight (unknown) or 0 (declared neutral) goes in neither list.
    """
    pos: list[str] = []
    neg: list[str] = []
    for e in effective_interests(entries):
        if e.weight is None or e.weight == 0:
            continue
        (pos if e.weight > 0 else neg).append(e.concept.text)
    return pos, neg


class SectionVisibility(JsonLdModel):
    """A declared privacy tier for one inline section of the document.

    Artifacts carry their own tier on an :class:`ArtifactRef`; the fields
    *inside* ``profile.jsonld`` had nowhere to carry one, so a private summary
    or a private methods list rode out in a public document however carefully
    the files were excluded. The section names are a closed set on purpose:
    :data:`researcher_profiles.privacy.SECTION_FIELDS` maps each to the exact
    fields it governs, and a section nothing maps to would be a tier that
    silently protects nothing.
    """

    section: Literal[
        "summary",
        "expertise",
        "focus",
        "methods",
        "soul",
        "clinical",
        "site_capabilities",
        "regulatory_experience",
        "contact",
        "background",
    ]
    visibility: Visibility


class SiteInfrastructure(_Base):
    """What a trial site has to run a study with (``rp:siteCapabilities``).

    Every field is optional and ``None`` means unknown, never zero: a site
    whose coordinator count nobody recorded must not read as a site with no
    coordinators.
    """

    coordinators: int | None = Field(default=None, ge=0)
    irb_experience: bool | None = None
    phase_experience: list[str] = []


class SiteCapabilities(_Base):
    """A clinical site's capacity to host trials (``rp:siteCapabilities``).

    Typed rather than a free dict so the owner editor can round-trip it and
    the privacy projection can drop it as one section. See the optional
    clinical extension in ``docs/rp-spec/index.md``.
    """

    patient_populations: list[str] = []
    disease_areas: list[str] = []
    infrastructure: SiteInfrastructure | None = None
    #: A coarse band, not a headcount. ``None`` is unknown and stays unknown.
    enrollment_capacity: Literal["low", "medium", "high"] | None = None


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
    #: ``cv``/``web``/``paper_fulltext`` artifact defaults to ``restricted``.
    #: Every role's tier is fully choosable by the owner; the default is a
    #: default, not a floor. The *effective* tier also folds in
    #: ``derived_from``; see :func:`researcher_profiles.privacy.effective_tiers`.
    visibility: Visibility = "public"
    #: Roles or paper_ids this artifact was derived from. Its effective tier is
    #: the most restrictive of its own and its sources'.
    derived_from: list[str] = Field(default=[], alias="derivedFrom")

    @model_validator(mode="after")
    def _apply_role_tier(self) -> "ArtifactRef":
        # When the tier was not stated, inherit the role default.
        if "visibility" not in self.model_fields_set:
            self.visibility = role_default_visibility(self.role)
        return self
