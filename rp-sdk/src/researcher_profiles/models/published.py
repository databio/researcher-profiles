"""Pydantic models for the published researcher-profiles standard.

These types describe the static artifacts that ``rp render`` and ``rp site``
emit: manifests, embedding indices, collection bundles, cluster documents, and
topic indices. They are distinct from the on-disk *source* models in
``schema/`` and the HTTP *wire* models in ``models/api.py``.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _PublishedModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# Shared / leaf types
# ---------------------------------------------------------------------------


class ArtifactLink(_PublishedModel):
    rel: str
    href: str
    media_type: str
    bytes: int | None = None
    sha256: str | None = None


# NOTE: ``PublishedManifest`` and ``PublishedPapers`` were removed in the
# "publishable by construction" collapse. There is no separate published
# document shape any more: ``profile.jsonld`` is the ``schema:Person`` record
# (``ProfileDocument``) everywhere, and its manifest is ``hasPart``/``subjectOf``
# (``ArtifactRef``). See docs/rp-spec/.


# ---------------------------------------------------------------------------
# Embedding index
# ---------------------------------------------------------------------------


class EmbeddingProbe(_PublishedModel):
    text: str
    vector: list[float]


class EmbeddingIndex(_PublishedModel):
    backend_spec: str
    file: str
    dtype: Literal["float32"] = "float32"
    byte_order: Literal["little"] = "little"
    layout: Literal["row_major"] = "row_major"
    dim: int
    count: int
    normalized: bool = True
    metric: str = "cosine"
    row_key: Literal["chunk_index", "slug"]
    rows: list[str | int]
    sha256: str
    probe: EmbeddingProbe


# ---------------------------------------------------------------------------
# Profile list
# ---------------------------------------------------------------------------


class ProfileListRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    name: str | None = None


class ProfileListLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    list: str


class ProfileListDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    rp_profileList: Literal["0.1"] = Field(alias="rp:profileList")
    name: str
    url: str
    updated: str
    profiles: list[str | ProfileListRef | ProfileListLink]


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


class ProfileCard(_PublishedModel):
    slug: str
    rid: str | None = None
    name: str
    level: str
    affiliation: str | None = None
    field: str | None = None
    paper_count: int = 0
    summary_count: int = 0
    fulltext_pct: float = 0.0
    base: str


class ProfileCollection(_PublishedModel):
    context: str = Field(alias="@context")
    id: str = Field(alias="@id")
    generated_at: str
    generator: str
    backend_spec: str | None = None
    dim: int | None = None
    count: int
    cards: list[ProfileCard]
    artifacts: list[ArtifactLink]


# ---------------------------------------------------------------------------
# Topic index
# ---------------------------------------------------------------------------


class TopicIndexDocument(_PublishedModel):
    version: int = 1
    computed_at: str
    index: dict[str, list[str]]
