"""Everything under ``sources/``: the two JSON-LD ``Collection`` sidecars
(``papers.jsonld``, ``grants.jsonld``) with their records, and the markdown
summary frontmatter.
"""

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

from ._common import FORMAT_HINT
from ._identity import _slugify, normalize_doi
from ._parts import ResearchOutput
from .jsonld import CONTEXT_URL, PROFILE_FORMAT_IRI, JsonLdModel

# ---------------------------------------------------------------------------
# sources/papers.jsonld
# ---------------------------------------------------------------------------


class PaperRecord(ResearchOutput):
    """One work: a ``schema:ScholarlyArticle`` node.

    A paper IS a research output, so this is a specialization of
    :class:`~.ResearchOutput`: it inherits the shared core (``name``,
    ``type``, ``description``, ``url``, plus ``@id`` / ``@type``) and adds the
    bibliographic, authorship and access fields a scholarly work needs.

    Papers live in ``sources/papers.jsonld``, not in the profile's
    ``researchOutputs`` list; the shared lineage is about the schema, not the
    storage location.

    ``type`` here is the OpenAlex work type (``article``, ``review``, ...),
    which narrows the base class's output-kind meaning; it is optional because
    not every source reports one.

    Every *build* field (``status``, ``download_attempts``, ``contaminated``,
    ``identity_verified``, ...) lives in ``.build/<slug>/meta/build_state.json``
    instead. Publishing a profile publishes the bibliographic record, not the
    build's dirty laundry.
    """

    type_: str | None = Field(default="ScholarlyArticle", alias="@type")

    # Bibliographic core. The on-disk key is ``name`` (inherited from
    # ``ResearchOutput``); ``title`` stays accepted on input and readable as a
    # property, because that is what a paper's name is called everywhere else
    # in this package.
    name: str = Field(validation_alias=AliasChoices("name", "title"))
    year: int | None = Field(default=None, alias="datePublished")
    journal: str | None = Field(default=None, alias="isPartOf")
    authors: list[str] | None = Field(default=None, alias="author")

    # Identity
    paper_id: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    openalex_id: str | None = None

    # Bibliographic detail
    venue: str | None = None
    type: str | None = None
    first_author: str | None = None
    last_author: str | None = None
    citation: str | None = None
    cited_by_count: int | None = None
    abstract: str | None = None
    summary: str | None = None

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

        A relative fragment is a legitimate answer, not a degraded one: it
        resolves against wherever the document is served.
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

    ``sources/papers.jsonld`` and ``sources/grants.jsonld`` are the same
    document with a different payload: a ``Collection`` node that declares the
    format it conforms to, names the person it is ``about``, and carries a
    ``hasPart`` array. Everything except that array (and papers' own
    ``dateModified``) is here, once.

    A subclass supplies :attr:`_FILE_LABEL` so the bare-list rejection names
    the file the caller actually passed, and declares its own ``has_part``
    element type and its own read-through alias property (``papers`` /
    ``grants``), which are the public API and differ by name.
    """

    #: Filename this document is read from, used in the bare-list error.
    _FILE_LABEL: ClassVar[str] = "the collection"

    context: str | None = Field(default=CONTEXT_URL, alias="@context")
    type_: str | None = Field(default="Collection", alias="@type")
    conforms_to: str = Field(default=PROFILE_FORMAT_IRI, alias="conformsTo")
    #: The person this collection is about (their profile ``@id``).
    about: str | None = None

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

    schema.org has no crisp "this person received this grant" relation, so the
    person->grant link is ``rp:heldGrant``. That is an ``rp:``
    decision, not an oversight (see ``context/README.md``).
    """

    type_: str | None = Field(default="MonetaryGrant", alias="@type")

    id: str
    title: str = Field(alias="name")
    funder: str | None = None
    number: str | None = Field(default=None, alias="identifier")
    #: NIH activity code (e.g. "R01", "K99", "U01"), recoverable without
    #: parsing the free-text ``identifier``. Feeds eligibility evaluation
    #: (R01-equivalent history for ESI/NI determinations).
    activity_code: str | None = None
    role: Literal["pi", "co_pi", "co_i", "other"] | None = None
    status: Literal["funded", "pending", "completed"] | None = None
    start: str | None = None
    end: str | None = None
    abstract: str | None = None
    #: Where the record came from: ``grants-data`` (lab service), ``manual``
    #: (hand-supplied), or ``reporter`` (NIH RePORTER supplement).
    source: Literal["grants-data", "manual", "reporter"] | None = None
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

    Any profile level may carry one: grant history feeds eligibility
    evaluation, which is not a deep-profile-only concern.
    """

    _FILE_LABEL: ClassVar[str] = "grants.jsonld"

    has_part: list[GrantRecord] = Field(default=[], alias="hasPart")

    @property
    def grants(self) -> list[GrantRecord]:
        """Alias for :attr:`has_part`: the awards in this collection."""
        return self.has_part


# ---------------------------------------------------------------------------
# Non-JSON-LD sidecar artifacts (markdown frontmatter, question queue)
# ---------------------------------------------------------------------------


class SummaryFile(BaseModel):
    """Frontmatter shape for sources/summaries/<paper_id>.summary.md.

    On-disk file is markdown with a YAML frontmatter block. This model
    validates the frontmatter only - the body is plain markdown.
    """

    model_config = ConfigDict(extra="forbid")

    paper_id: str
    source_kind: Literal["fulltext", "abstract"]
    source_hash: str  # sha256 of the input markdown / abstract text
    written_at: str | None = None  # ISO-8601 timestamp

    @field_validator("written_at", mode="before")
    @classmethod
    def _coerce_written_at(cls, v: Any) -> Any:
        """An unquoted ISO-8601 timestamp in YAML frontmatter parses as a
        ``date``/``datetime`` object, not a string; this normalizes it back."""
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v
