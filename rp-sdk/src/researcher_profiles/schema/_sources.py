"""Everything under ``sources/``: the JSON-LD ``Collection`` sidecars
(``papers.jsonld``, ``grants.jsonld``, ``trials.jsonld``, ``seed_papers.jsonld``)
with their records, and the markdown summary frontmatter.
"""

from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from ._common import FORMAT_HINT, GYear, IdRef, OrganizationName, PeriodicalName, PersonList
from ._identity import _slugify, normalize_doi
from ._parts import ResearchOutput
from .jsonld import CONTEXT_URL, PROFILE_FORMAT_IRI, JsonLdModel

# ---------------------------------------------------------------------------
# sources/papers.jsonld
# ---------------------------------------------------------------------------


class PaperRecord(ResearchOutput):
    """One work: a ``schema:ScholarlyArticle`` node.

    A specialization of :class:`~.ResearchOutput` that adds bibliographic,
    authorship and access fields. Papers live in ``sources/papers.jsonld``,
    not in the profile's ``researchOutputs`` list.

    ``type`` here is the optional OpenAlex work type (``article``,
    ``review``, ...). Build state is never part of the published record.
    """

    type_: str | None = Field(default="ScholarlyArticle", alias="@type")

    # The on-disk key is ``name``; ``title`` is accepted on input and readable
    # as a property.
    name: str = Field(validation_alias=AliasChoices("name", "title"))
    year: GYear = Field(default=None, alias="datePublished")
    journal: PeriodicalName = Field(default=None, alias="isPartOf")
    authors: PersonList = Field(default=None, alias="author")

    # Identity
    paper_id: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    openalex_id: str | None = None
    #: ``@id`` of the record this one is another version of
    #: (``dcterms:isVersionOf``): a preprint names its journal article here.
    #: The study is the target; this record is kept for completeness and is
    #: not counted or summarized a second time.
    is_version_of: str | None = Field(default=None, alias="isVersionOf")

    # Bibliographic detail
    venue: str | None = None
    type: str | None = None
    first_author: str | None = None
    last_author: str | None = None
    citation: str | None = None
    cited_by_count: int | None = None
    abstract: str | None = None
    summary: str | None = None
    #: OpenAlex topic ids (``T…``) the work is tagged with, primary topic
    #: first, each once. What an inferred interest counts over.
    topics: list[str] = []

    # Authorship (researcher-profiles specific)
    author_position: str | None = None
    author_index: int | None = None
    total_authors: int | None = None
    is_corresponding: bool | None = None

    # Access
    open_access: bool | None = None
    is_oa: bool | None = None
    oa_status: str | None = None
    oa_url: str | None = None
    pdf_url: str | None = None
    url: str | None = None
    full_text_link: str | None = None
    access: str | None = None
    source: str | None = None

    # --- derived names ----------------------------------------------------

    @property
    def title(self) -> str:
        """The paper's ``name``, under the word this package uses for works."""
        return self.name

    # --- validation -------------------------------------------------------

    @field_validator("year", mode="before")
    @classmethod
    def _coerce_year(cls, v: Any) -> Any:
        """``xsd:gYear`` is a string on disk; an ``int`` in Python."""
        if isinstance(v, str):
            v = v.strip()
            return int(v) if v.isdigit() else None
        return v

    @field_validator("journal", mode="before")
    @classmethod
    def _flatten_journal(cls, v: Any) -> Any:
        """Accept either a plain string or a ``Periodical`` node."""
        if isinstance(v, dict):
            return v.get("name")
        return v

    @field_validator("authors", mode="before")
    @classmethod
    def _flatten_authors(cls, v: Any) -> Any:
        """Accept either plain strings or ``Person`` nodes."""
        if isinstance(v, list):
            return [(a.get("name") if isinstance(a, dict) else a) for a in v]
        return v

    # --- derived ------------------------------------------------------

    def resolve_id(self) -> str:
        """The node ``@id``: DOI IRI -> OpenAlex IRI -> relative fragment.

        A relative fragment resolves against wherever the document is served.
        """
        if self.id_:
            return self.id_
        doi = normalize_doi(self.doi)
        if doi:
            return f"https://doi.org/{doi}"
        if self.openalex_id:
            oa = str(self.openalex_id).strip()
            if oa.startswith("http"):
                return oa
            return f"https://openalex.org/{oa}"
        return f"#paper/{self.paper_id or _slugify(self.title)}"

    # --- serialization ------------------------------------------------

    @field_serializer("year", when_used="unless-none")
    def _ser_year(self, v: int) -> str:
        return str(v)

    @field_serializer("journal", when_used="unless-none")
    def _ser_journal(self, v: str) -> dict[str, Any]:
        return {"@type": "Periodical", "name": v}

    @field_serializer("authors", when_used="unless-none")
    def _ser_authors(self, v: list[str]) -> list[dict[str, Any]]:
        return [{"@type": "Person", "name": a} for a in v]

    def _jsonld_node(self, data: dict[str, Any]) -> dict[str, Any]:
        data["@id"] = self.resolve_id()
        return data


class _CollectionDocument(JsonLdModel):
    """Shared shape of the two ``Collection`` sidecars.

    A ``Collection`` node that declares its format, names the person it is
    ``about``, and carries a ``hasPart`` array. A subclass sets
    :attr:`_FILE_LABEL` and its own ``has_part`` element type.
    """

    #: Filename this document is read from, used in the bare-list error.
    _FILE_LABEL: ClassVar[str] = "the collection"

    context: str | None = Field(default=CONTEXT_URL, alias="@context")
    type_: str | None = Field(default="Collection", alias="@type")
    conforms_to: str = Field(default=PROFILE_FORMAT_IRI, alias="conformsTo")
    #: The person this collection is about (their profile ``@id``).
    about: IdRef = None

    @model_validator(mode="before")
    @classmethod
    def _reject_bare_list(cls, data: Any) -> Any:
        if isinstance(data, list):
            raise ValueError(
                f"{cls._FILE_LABEL} is a bare list; must be a Collection node with "
                f"a hasPart array. {FORMAT_HINT}"
            )
        return data

    @field_validator("conforms_to")
    @classmethod
    def _validate_conforms_to(cls, v: str) -> str:
        if v != PROFILE_FORMAT_IRI:
            raise ValueError(f"conformsTo is {v!r}, expected {PROFILE_FORMAT_IRI!r}. {FORMAT_HINT}")
        return v

    @field_validator("about", mode="before")
    @classmethod
    def _flatten_about(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return v.get("@id")
        return v

    @field_serializer("about", when_used="unless-none")
    def _ser_about(self, v: str) -> dict[str, Any]:
        return {"@id": v}


class PapersDocument(_CollectionDocument):
    """Top-level model for ``sources/papers.jsonld`` (a ``Collection``)."""

    _FILE_LABEL: ClassVar[str] = "papers.jsonld"

    date_modified: str | None = Field(default=None, alias="dateModified")
    has_part: list[PaperRecord] = Field(default=[], alias="hasPart")

    @property
    def papers(self) -> list[PaperRecord]:
        """Alias for :attr:`has_part`: the works in this collection."""
        return self.has_part


# ---------------------------------------------------------------------------
# sources/grants.jsonld
# ---------------------------------------------------------------------------


class GrantRecord(JsonLdModel):
    """One award: a ``schema:MonetaryGrant`` node.

    schema.org has no "this person received this grant" relation, so the
    person->grant link is ``rp:heldGrant`` (see ``context/README.md``).
    """

    type_: str | None = Field(default="MonetaryGrant", alias="@type")

    id: str
    title: str = Field(alias="name")
    funder: OrganizationName = None
    number: str | None = Field(default=None, alias="identifier")
    #: NIH activity code (e.g. "R01", "K99", "U01"), so eligibility evaluation
    #: need not parse the free-text ``identifier``.
    activity_code: str | None = None
    role: Literal["pi", "co_pi", "co_i", "other"] | None = None
    status: Literal["funded", "pending", "completed"] | None = None
    start: str | None = None
    end: str | None = None
    abstract: str | None = None
    #: Where the record came from: ``mygrants`` (MyGrants service), ``manual``
    #: (hand-supplied), or ``reporter`` (NIH RePORTER supplement).
    source: Literal["mygrants", "manual", "reporter"] | None = None
    url: str | None = None

    @field_validator("funder", mode="before")
    @classmethod
    def _flatten_funder(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return v.get("name")
        return v

    @field_serializer("funder", when_used="unless-none")
    def _ser_funder(self, v: str) -> dict[str, Any]:
        return {"@type": "Organization", "name": v}

    def _jsonld_node(self, data: dict[str, Any]) -> dict[str, Any]:
        if not data.get("@id"):
            data["@id"] = f"#grant/{self.id}"
        return data


class GrantsDocument(_CollectionDocument):
    """Top-level model for ``sources/grants.jsonld``.

    Any profile level may carry one.
    """

    _FILE_LABEL: ClassVar[str] = "grants.jsonld"

    has_part: list[GrantRecord] = Field(default=[], alias="hasPart")

    @property
    def grants(self) -> list[GrantRecord]:
        """Alias for :attr:`has_part`: the awards in this collection."""
        return self.has_part


class TrialRecord(JsonLdModel):
    """A confirmed researcher-to-clinical-trial relationship.

    Part of the optional clinical extension (``docs/rp-spec/index.md``). Only
    confirmed relationships belong here. ``researcher_role`` stays absent when
    unknown rather than claiming a role nobody stated. ``nct_id`` is the
    record's identity and its ``@id`` is derived from it.
    """

    type_: str | None = Field(default="MedicalTrial", alias="@type")
    nct_id: str = Field(pattern=r"^NCT\d{8}$")
    title: str = Field(alias="name")
    #: ``principal_investigator``, ``co_investigator``, ``sub_investigator``,
    #: ``site_pi``, or any other label the source uses. Absent = unknown.
    researcher_role: str | None = Field(default=None, alias="rp:researcherRole")
    phase: str | None = None
    status: str | None = None
    therapeutic_area: str | None = None
    conditions: list[str] = Field(default=[])
    sponsor: str | None = None
    sponsor_type: str | None = None
    #: Participants enrolled. ``None`` is unknown and must never be read as 0.
    enrollment: int | None = Field(default=None, ge=0)
    start_date: str | None = None
    completion_date: str | None = None
    sites: int | None = Field(default=None, ge=0)

    def _jsonld_node(self, data: dict[str, Any]) -> dict[str, Any]:
        data["@id"] = f"https://clinicaltrials.gov/study/{self.nct_id}"
        return data


class TrialStats(BaseModel):
    """Counts derived from a :class:`TrialsDocument`, never authored.

    Derived so a total can never disagree with the records under it.
    """

    model_config = ConfigDict(extra="forbid")

    total_trials: int = 0
    #: Records whose ``researcher_role`` names a principal-investigator role.
    as_pi: int = 0
    industry_sponsored: int = 0
    completed: int = 0
    #: Total enrollment, or ``None`` when any record's enrollment is unknown,
    #: so a partial sum is never reported as a total.
    total_enrollment: int | None = None


#: ``researcher_role`` values that count as leading a trial. Compared
#: case-insensitively; anything else (including an absent role) does not count.
_PI_ROLES: frozenset[str] = frozenset({"principal_investigator", "site_pi"})


class TrialsDocument(_CollectionDocument):
    """Top-level model for ``sources/trials.jsonld`` (a ``Collection``).

    Optional. An empty ``papers.jsonld`` alongside a populated trials
    collection is a complete profile.
    """

    _FILE_LABEL: ClassVar[str] = "trials.jsonld"
    has_part: list[TrialRecord] = Field(default=[], alias="hasPart")

    @property
    def trials(self) -> list[TrialRecord]:
        """Alias for :attr:`has_part`: the trials in this collection."""
        return self.has_part

    @property
    def stats(self) -> TrialStats:
        """Counts over the confirmed records in this collection."""
        enrollments = [t.enrollment for t in self.has_part]
        return TrialStats(
            total_trials=len(self.has_part),
            as_pi=sum(1 for t in self.has_part if (t.researcher_role or "").lower() in _PI_ROLES),
            industry_sponsored=sum(
                1 for t in self.has_part if (t.sponsor_type or "").lower() == "industry"
            ),
            completed=sum(1 for t in self.has_part if (t.status or "").lower() == "completed"),
            total_enrollment=(
                sum(e for e in enrollments if e is not None)
                if enrollments and all(e is not None for e in enrollments)
                else None
            ),
        )


# ---------------------------------------------------------------------------
# sources/seed_papers.jsonld
# ---------------------------------------------------------------------------

#: Most interest seed papers one profile may hold. Adds beyond it are refused.
SEED_PAPERS_MAX = 200

#: Longest seed abstract kept, in characters. It is the embedding input only.
SEED_ABSTRACT_MAX = 8000

#: How a seed reached the list.
SeedAddedVia = Literal["owner", "interview", "feed_upvote", "migration"]


def _bare_openalex_id(value: Any) -> str | None:
    """``https://openalex.org/W1`` / ``w1`` / ``W1`` -> ``W1``; ``None`` when empty."""
    if value is None:
        return None
    text = str(value).strip().rstrip("/")
    if not text:
        return None
    tail = text.rsplit("/", 1)[-1]
    return tail[:1].upper() + tail[1:]


class SeedPaperRecord(JsonLdModel):
    """One interest seed paper: a work that defines what the person wants to study.

    Lives in ``sources/seed_papers.jsonld``. A seed asserts interest, never
    authorship: it is deliberately **not** a :class:`PaperRecord`, carries no
    authorship, full text, summary or citation fields, and never appears in
    the publication list or the synthesized narrative. It feeds the interest
    signal only (the embedding index, the centroid, feed ranking, matching).
    """

    type_: str | None = Field(default="ScholarlyArticle", alias="@type")

    name: str = Field(validation_alias=AliasChoices("name", "title"))
    year: GYear = Field(default=None, alias="datePublished")
    #: Display only.
    journal: PeriodicalName = Field(default=None, alias="isPartOf")
    #: Bare OpenAlex work id (``W…``).
    openalex_id: str | None = None
    #: Bare DOI (``10.x/y``), case preserved.
    doi: str | None = None
    #: The embedding input, capped at :data:`SEED_ABSTRACT_MAX` characters.
    abstract: str | None = None
    #: OpenAlex topic ids (``T…``), for the feed query.
    topics: list[str] = []
    added_at: datetime | None = Field(default=None, alias="dateCreated")
    added_via: SeedAddedVia = "owner"

    @property
    def title(self) -> str:
        """The seed's ``name``."""
        return self.name

    @property
    def seed_id(self) -> str:
        """Identity within the list: the bare OpenAlex id, else ``doi:<doi>``."""
        return self.openalex_id or f"doi:{self.doi}"

    @field_validator("year", mode="before")
    @classmethod
    def _coerce_year(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return int(v) if v.isdigit() else None
        return v

    @field_validator("journal", mode="before")
    @classmethod
    def _flatten_journal(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return v.get("name")
        return v

    @field_validator("openalex_id", mode="before")
    @classmethod
    def _bare_openalex(cls, v: Any) -> Any:
        return _bare_openalex_id(v)

    @field_validator("doi", mode="before")
    @classmethod
    def _normalize_doi(cls, v: Any) -> Any:
        return normalize_doi(v) if isinstance(v, str) else v

    @field_validator("abstract", mode="before")
    @classmethod
    def _cap_abstract(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()[:SEED_ABSTRACT_MAX]
            return v or None
        return v

    @model_validator(mode="after")
    def _needs_an_identifier(self) -> "SeedPaperRecord":
        if not (self.openalex_id or self.doi):
            raise ValueError("a seed paper needs an OpenAlex id or a DOI")
        return self

    @classmethod
    def from_paper(cls, rec: PaperRecord, *, added_via: SeedAddedVia) -> "SeedPaperRecord":
        """A seed carrying only ``rec``'s lightweight metadata, stamped now.

        Authorship, full text, summary and citation fields are dropped.
        """
        return cls(
            name=rec.name,
            year=rec.year,
            journal=rec.journal,
            openalex_id=rec.openalex_id,
            doi=rec.doi,
            abstract=rec.abstract,
            topics=list(rec.topics or []),
            added_at=datetime.now(UTC),
            added_via=added_via,
        )

    @field_serializer("year", when_used="unless-none")
    def _ser_year(self, v: int) -> str:
        return str(v)

    @field_serializer("journal", when_used="unless-none")
    def _ser_journal(self, v: str) -> dict[str, Any]:
        return {"@type": "Periodical", "name": v}

    @field_serializer("added_at", when_used="unless-none")
    def _ser_added_at(self, v: datetime) -> str:
        return v.isoformat()

    def _jsonld_node(self, data: dict[str, Any]) -> dict[str, Any]:
        if self.openalex_id:
            data["@id"] = f"https://openalex.org/{self.openalex_id}"
        else:
            data["@id"] = f"https://doi.org/{self.doi}"
        return data


class SeedPapersDocument(_CollectionDocument):
    """Top-level model for ``sources/seed_papers.jsonld`` (a ``Collection``).

    Optional, owner curated, ``private`` by role default. Consumers must never
    list its entries as the person's works.
    """

    _FILE_LABEL: ClassVar[str] = "seed_papers.jsonld"
    has_part: list[SeedPaperRecord] = Field(default=[], alias="hasPart")

    @property
    def seed_papers(self) -> list[SeedPaperRecord]:
        """Alias for :attr:`has_part`: the seeds in this collection."""
        return self.has_part


# ---------------------------------------------------------------------------
# Non-JSON-LD sidecar artifacts (markdown frontmatter, question queue)
# ---------------------------------------------------------------------------


class SummaryFile(BaseModel):
    """Frontmatter shape for sources/summaries/<paper_id>.summary.md.

    Validates the YAML frontmatter only; the body is plain markdown.
    """

    model_config = ConfigDict(extra="forbid")

    paper_id: str
    source_kind: Literal["fulltext", "abstract"]
    source_hash: str  # sha256 of the input markdown / abstract text
    written_at: str | None = None  # ISO-8601 timestamp

    @field_validator("written_at", mode="before")
    @classmethod
    def _coerce_written_at(cls, v: Any) -> Any:
        """Normalize an unquoted YAML timestamp (parsed as a datetime) back to a string."""
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v
