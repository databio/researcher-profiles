"""The affiliation core: :class:`Affiliation`."""

from datetime import date

from pydantic import model_validator

from .base import ScholarModel
from .org import Organization


class Affiliation(ScholarModel):
    """A person's relationship to an organization: appointment or byline affiliation.

    One model serves two uses: biographical (Person employment/education, with
    title + dates) and byline (Authorship, usually just organization + department).
    The paper-scoped rendering (superscript order, cross-author dedup) is a
    consumer's job, not this model's.

    Attributes:
        organization: The institution (the identity, keyed on ror_id).
        department: The sub-unit within the organization (free text; most
            departments have no ROR, so this is not a nested Organization).
        title: The role/appointment (e.g. "Professor", "Postdoc"); mainly
            biographical.
        start: Appointment start (ISO 8601), when known.
        end: Appointment end; absent means current (see :attr:`current`).
    """

    organization: Organization
    department: str | None = None
    title: str | None = None
    start: date | None = None
    end: date | None = None

    @property
    def current(self) -> bool:
        """True when the appointment has no end date. Derived, never stored."""
        return self.end is None

    @model_validator(mode="before")
    @classmethod
    def _coerce_bare_string(cls, v: object) -> object:
        """Accept a bare org-name string as a low-fidelity affiliation."""
        if isinstance(v, str):
            return {"organization": {"name": v}}
        return v
