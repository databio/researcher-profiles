"""The organization core: :class:`Organization`."""

from .base import ScholarModel


class Organization(ScholarModel):
    """An institution: a university, funder, or other organization.

    Attributes:
        name: The organization's name.
        ror_id: The organization's Research Organization Registry id.
        type: The kind of organization (e.g. "university", "funder",
            "nonprofit").
        address: The organization's free-text address.
    """

    name: str
    ror_id: str | None = None
    type: str | None = None
    address: str | None = None
