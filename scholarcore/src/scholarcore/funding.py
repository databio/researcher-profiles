"""The funding core: :class:`AwardRef`, :class:`Award`, :class:`OpportunityRef`,
:class:`Opportunity`, and their shared vocabularies.

See :mod:`scholarcore.person` for an explanation of the Ref pattern.
"""

from datetime import date
from enum import Enum
from typing import Any

from pydantic import field_validator

from .base import ResearchOutput, ScholarModel
from .person import PersonRef


class AwardRef(ScholarModel):
    """A lightweight pointer to an award: the join key plus a cached title.

    Use this when referencing an award without needing its full record,
    for prior-support lists, related-grants mentions.

    Attributes:
        application_id: The canonical join key, assigned by the system of record.
        title: A cached title for display without a lookup.
    """

    application_id: str
    title: str | None = None


class OpportunityRef(ScholarModel):
    """A lightweight pointer to a funding opportunity: the join key plus a cached title.

    Use this when referencing an opportunity without needing its full record,
    for match results, subscription lists.

    Attributes:
        opportunity_number: The funder-assigned opportunity number, the join key.
        title: A cached title for display without a lookup.
    """

    opportunity_number: str
    title: str | None = None


class GrantRole(str, Enum):
    """A person's role on a funded award."""

    pi = "pi"
    co_pi = "co_pi"
    co_i = "co_i"
    other = "other"


class AwardStatus(str, Enum):
    """An award's lifecycle stage."""

    planning = "planning"
    submitted = "submitted"
    pending = "pending"
    funded = "funded"
    active = "active"
    completed = "completed"
    rejected = "rejected"
    withdrawn = "withdrawn"


class SubmissionType(str, Enum):
    """The kind of submission an award application represents."""

    new = "new"
    renewal = "renewal"
    resubmission = "resubmission"
    supplement = "supplement"


class Award(ResearchOutput):
    """A funded or proposed award of financial support for research.

    Attributes:
        application_id: The canonical join key for this award, assigned by
            whatever external system of record tracks the application.
        title: The award's title.
        funder: The funding organization's name.
        number: The award or grant number (an identifier distinct from
            ``application_id``).
        activity_code: The funder's activity/mechanism code (e.g. an NIH
            code like "R01", "U01", "K99").
        pi: The principal investigator.
        co_investigators: The award's co-investigators.
        role: The role of the person the record is scoped to, when the
            record represents one person's participation rather than the
            award as a whole.
        status: The award's lifecycle stage. An unrecognized value loads as
            None rather than raising, so a consumer with a finer-grained
            vocabulary can layer it in an extra field instead of breaking.
        submission_type: The kind of submission (new, renewal, resubmission,
            supplement). An unrecognized value loads as None rather than
            raising, for the same reason as ``status``.
        start: The award's start date.
        end: The award's end date.
        effort: The PI's (or the person the record is scoped to) effort, as
            an FTE fraction (0-1). A person-months figure is a derived,
            tool-specific representation, not a second base field.
        directs: Direct costs, in whole US dollars.
        indirects: Indirect costs, in whole US dollars.
        total: Total costs, in whole US dollars.
        abstract: The award's abstract or project summary.
    """

    application_id: str | None = None
    title: str
    funder: str | None = None
    number: str | None = None
    activity_code: str | None = None
    pi: PersonRef | None = None
    co_investigators: list[PersonRef] = []
    role: GrantRole | None = None
    status: AwardStatus | None = None
    submission_type: SubmissionType | None = None
    start: date | None = None
    end: date | None = None
    effort: float | None = None
    directs: int | None = None
    indirects: int | None = None
    total: int | None = None
    abstract: str | None = None

    @field_validator("role", mode="before")
    @classmethod
    def _tolerate_unknown_role(cls, v: Any) -> Any:
        if v is None or isinstance(v, GrantRole):
            return v
        try:
            return GrantRole(v)
        except ValueError:
            return None

    @field_validator("status", mode="before")
    @classmethod
    def _tolerate_unknown_status(cls, v: Any) -> Any:
        if v is None or isinstance(v, AwardStatus):
            return v
        try:
            return AwardStatus(v)
        except ValueError:
            return None

    @field_validator("submission_type", mode="before")
    @classmethod
    def _tolerate_unknown_submission_type(cls, v: Any) -> Any:
        if v is None or isinstance(v, SubmissionType):
            return v
        try:
            return SubmissionType(v)
        except ValueError:
            return None


class Opportunity(ScholarModel):
    """A published funding opportunity (a call for applications).

    Attributes:
        opportunity_number: The funder-assigned opportunity number, the
            canonical join key.
        agency: The funding agency's name.
        title: The opportunity's title.
        url: A link to the opportunity's published announcement.
        document_type: The kind of announcement (e.g. "NOFO", "RFA", "PA").
        activity_codes: The activity/mechanism codes this opportunity
            supports.
        posted_date: The date the opportunity was posted.
        expiration_date: The date the opportunity closes.
        budget_max: The maximum budget an application may request.
        purpose: A short statement of the opportunity's purpose.
        keywords: Keywords describing the opportunity's scope.
    """

    opportunity_number: str
    agency: str | None = None
    title: str | None = None
    url: str | None = None
    document_type: str | None = None
    activity_codes: list[str] = []
    posted_date: date | None = None
    expiration_date: date | None = None
    budget_max: int | None = None
    purpose: str | None = None
    keywords: list[str] = []
