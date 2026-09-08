"""Person and Researcher identity.

The Ref pattern
---------------

Each core entity has a corresponding ``XRef`` class (e.g. :class:`PersonRef`,
:class:`PaperRef`). These are lightweight pointers (a join key plus a
cached display field) used when you need to reference an entity without
embedding its full record.

Why: A Paper with 20 authors should not carry 20 full Researcher profiles with
training, career, affiliations, etc. It carries 20 PersonRefs (rid + name).
Similarly, an Award references its PI as a PersonRef, not a full Person. This
is the foreign-key pattern: store the key and optionally a denormalized field
for display, rather than embedding the full record.

The hierarchy here is::

    PersonRef       # join key (rid) + cached name
      └─ Person     # + schema.org-like biographical fields
           └─ Researcher  # + research-specific fields (training, career, etc.)
"""

from typing import Literal

from pydantic import Field, field_validator

from .affil import Affiliation
from .base import ScholarModel
from .identity import validate_rid


class PersonRef(ScholarModel):
    """A lightweight pointer to a person: the join key plus a cached name.

    Use this when referencing a person without needing their full record,
    for authorship lists, grant co-investigators, collaborator mentions. The
    ``rid`` is the join key; ``name`` is cached for display without a lookup.

    Attributes:
        rid: The canonical researcher id, an ORCID or a ``local:`` id.
        name: A cached display name. Not authoritative; the full Person/Researcher
            record is.
    """

    rid: str
    name: str | None = None

    @field_validator("rid")
    @classmethod
    def _validate_rid(cls, v: str) -> str:
        return validate_rid(v)


class Person(PersonRef):
    """A person with schema.org-like biographical fields.

    Extends :class:`PersonRef` with fields common to any person in an academic
    context, not researcher-specific. For research-specific fields (training,
    career, field of study), see :class:`Researcher`.

    Attributes:
        given_name: The person's given (first) name.
        family_name: The person's family (last) name.
        affiliations: Institutional affiliations, appointments and roles.
        email: The person's email address.
    """

    given_name: str | None = None
    family_name: str | None = None
    affiliations: list[Affiliation] | None = None
    email: str | None = None


class Training(ScholarModel):
    """One training or education entry.

    ``kind`` separates degrees from postdocs and clinical training. A postdoc
    is a span, not a degree. ``year_start``/``year_end`` express spans; a
    single-year degree may set only ``year_end`` (completion year).

    Attributes:
        kind: What kind of training this was.
        degree: The degree or training title (e.g. "PhD", "Postdoctoral Fellow").
        institution: The institution where the training occurred.
        year_start: Start year; optional for a degree.
        year_end: End year. For a degree this is the completion year.
        advisor: The advisor or mentor.
        field: Field of study (e.g. "Electrical and Computer Engineering").
    """

    kind: Literal["degree", "postdoc", "clinical_training"] = Field(
        description="What kind of training this was. A postdoc is a 'postdoc' span entry, never a degree."
    )
    degree: str
    institution: str
    year_start: int | None = Field(default=None, description="Start year; optional for a degree.")
    year_end: int | None = Field(
        default=None,
        description="End year. For a degree this is the completion year.",
    )
    advisor: str | None = None
    field: str | None = None


class CareerEntry(ScholarModel):
    """One career or employment entry.

    ``end_year`` of ``None`` on a held position means "to present"; use
    ``None`` for ``start_year`` only when the start is genuinely unknown.

    Attributes:
        role: The position title.
        institution: The employing institution.
        start_year: Year the position started; null if unknown.
        end_year: Year the position ended; null means the position is still held.
    """

    role: str
    institution: str
    start_year: int | None = Field(
        default=None, description="Year the position started; null if unknown."
    )
    end_year: int | None = Field(
        default=None,
        description="Year the position ended; null means the position is still held.",
    )


class Researcher(Person):
    """A researcher: a person with research-specific biographical fields.

    Extends :class:`Person` with fields specific to someone who does research:
    training history, career trajectory, research field. This is the base class
    for researcher-profiles' ProfileDocument and for any downstream person
    model that needs training and career history.

    Attributes:
        training: Education and training history (degrees, postdocs, clinical training).
        career: Employment and appointment history.
        field: Primary research field or discipline.
        subfields: More specific areas within the field.
        summary: A brief biographical summary or description.
    """

    training: list[Training] = []
    career: list[CareerEntry] = []
    field: str | None = None
    subfields: list[str] = []
    summary: str | None = None
