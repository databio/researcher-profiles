"""Shared base model for every scholarcore entity."""

from pydantic import BaseModel, ConfigDict


class ScholarModel(BaseModel):
    """Base class for all scholarcore entities: shared config, no fields."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class ResearchOutput(ScholarModel):
    """A thing a researcher produced: the shared base for papers, awards,
    software, datasets, and anything else in their body of work."""

    title: str
    description: str | None = None
    url: str | None = None
