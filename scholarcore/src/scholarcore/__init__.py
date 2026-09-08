"""scholarcore: a small, dependency-free shared vocabulary for academic data.

Defines the common nouns of academic work (Person, Paper, Award,
Opportunity, Organization) once, as tolerant Pydantic models keyed on
canonical identifiers. This module imports every submodule eagerly, so
importing any name from ``scholarcore`` (or a specific submodule such as
``scholarcore.funding``) loads the whole package.
"""

from .affil import Affiliation
from .base import ResearchOutput, ScholarModel
from .biblio import Authorship, CreditRole, Paper, PaperRef
from .funding import (
    Award,
    AwardRef,
    AwardStatus,
    GrantRole,
    Opportunity,
    OpportunityRef,
    SubmissionType,
)
from .identity import (
    is_local,
    is_rid,
    mint_local_rid,
    normalize_doi,
    normalize_pmid,
    orcid_of,
    validate_rid,
)
from .org import Organization
from .person import CareerEntry, Person, PersonRef, Researcher, Training

__all__ = [
    # Base
    "ScholarModel",
    "ResearchOutput",
    # Person hierarchy (see person.py for the Ref pattern)
    "PersonRef",
    "Person",
    "Researcher",
    "Training",
    "CareerEntry",
    # Bibliographic
    "PaperRef",
    "Paper",
    "Authorship",
    "CreditRole",
    # Funding
    "AwardRef",
    "Award",
    "AwardStatus",
    "GrantRole",
    "SubmissionType",
    "OpportunityRef",
    "Opportunity",
    # Organization
    "Organization",
    "Affiliation",
    # Identity helpers
    "is_local",
    "is_rid",
    "mint_local_rid",
    "normalize_doi",
    "normalize_pmid",
    "orcid_of",
    "validate_rid",
]
