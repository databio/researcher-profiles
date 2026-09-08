"""The bibliographic core: :class:`PaperRef`, :class:`Paper`, :class:`Authorship`,
and :class:`CreditRole`.

See :mod:`scholarcore.person` for an explanation of the Ref pattern.
"""

from enum import Enum
from typing import Any

from pydantic import field_validator

from .affil import Affiliation
from .base import ResearchOutput, ScholarModel
from .identity import normalize_doi, normalize_pmid
from .person import PersonRef


class PaperRef(ScholarModel):
    """A lightweight pointer to a paper: the join key (DOI) plus a cached title.

    Use this when referencing a paper without needing its full record,
    for citation lists, related-works mentions, grant-to-paper links.

    Attributes:
        doi: The work's DOI, the primary join key.
        title: A cached title for display without a lookup.
    """

    doi: str
    title: str | None = None

    @field_validator("doi", mode="before")
    @classmethod
    def _normalize_doi(cls, v: Any) -> Any:
        """Strip prefix but preserve case."""
        return normalize_doi(v, lowercase=False)


class CreditRole(str, Enum):
    """The 14 NISO CRediT contributor roles (credit.niso.org).

    Values are the hyphenated slugs NISO publishes, not the enum member
    spelling. Python identifiers can't hold hyphens, but the wire form should
    match the standard.
    """

    conceptualization = "conceptualization"
    data_curation = "data-curation"
    formal_analysis = "formal-analysis"
    funding_acquisition = "funding-acquisition"
    investigation = "investigation"
    methodology = "methodology"
    project_administration = "project-administration"
    resources = "resources"
    software = "software"
    supervision = "supervision"
    validation = "validation"
    visualization = "visualization"
    writing_original_draft = "writing-original-draft"
    writing_review_editing = "writing-review-editing"


class Authorship(ScholarModel):
    """One person's authorship of one work: the shared fact, not a rendering.

    The paper-scoped byline *rendering* (superscript numbering, dedup of
    affiliations across authors, capability-URL edit graph, per-author verify
    flags) belongs to the consumer. This carries only what is a durable fact
    about the authorship itself.

    Attributes:
        person: The author, as a ref (``rid`` where known, else a
            locally-minted one; see :func:`scholarcore.identity.mint_local_rid`).
        position: The byline order. A consumer picks 0- or 1-based
            numbering; scholarcore carries it consistently.
        corresponding: Whether this author is a corresponding author.
        equal_contribution: Whether this author shares equal contribution
            with at least one co-author.
        affiliations: The institutions (and departments) this author is
            affiliated with, for this work. See
            :class:`scholarcore.affil.Affiliation`.
        credit_roles: The NISO CRediT roles this author contributed.
    """

    person: PersonRef
    position: int | None = None
    corresponding: bool = False
    equal_contribution: bool = False
    affiliations: list[Affiliation] = []
    credit_roles: list[CreditRole] = []


class Paper(ResearchOutput):
    """One scholarly work.

    Attributes:
        title: The work's title.
        authors: The work's authors, as authorship facts (byline position,
            corresponding/equal-contribution flags, affiliations, CRediT
            roles).
        year: The publication year.
        venue: The journal or conference the work appeared in.
        doi: The work's DOI, normalized on load (resolver prefix stripped,
            case preserved) so it can be compared directly across systems. See
            :func:`scholarcore.identity.normalize_doi`.
        pmid: The work's PMID, normalized on load (bare digits, prefix
            stripped). A value carrying no digits loads as None rather than
            raising. See :func:`scholarcore.identity.normalize_pmid`.
        pmcid: The PubMed Central id.
        openalex_id: The OpenAlex work id.
        arxiv_id: The arXiv id.
        abstract: The work's abstract.
    """

    title: str
    authors: list[Authorship] | None = None
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    openalex_id: str | None = None
    arxiv_id: str | None = None
    abstract: str | None = None

    @field_validator("doi", mode="before")
    @classmethod
    def _normalize_doi(cls, v: Any) -> Any:
        """Strip prefix but preserve case; some consumers depend on original case."""
        return normalize_doi(v, lowercase=False)

    @field_validator("pmid", mode="before")
    @classmethod
    def _normalize_pmid(cls, v: Any) -> Any:
        """Normalize on load, mirroring ``doi``. Non-numeric input loads as None."""
        return normalize_pmid(v)
