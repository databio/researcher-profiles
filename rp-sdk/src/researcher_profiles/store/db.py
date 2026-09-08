"""The SQL profile store: SQLModel tables a whole profile lives in.

A profile stored in these tables is a full profile, the third backing store
alongside the filesystem and the HTTP/static clients, and
:class:`researcher_profiles.store.sql.SqlArtifactStorage` reads and writes it
through the same ``ArtifactStorage`` interface the filesystem backend implements. The tables
cover the whole record: the profile document, the works and grants
collections, the expertise labels, every free-form artifact the manifest can
name, and (outside the published set) build state.

The tables:

- :class:`ProfileRow`        -> ``rp_profiles``        (PK: ``rid``)
- :class:`PaperRow`          -> ``rp_papers``          (one row per work)
- :class:`GrantRow`          -> ``rp_grants``          (one row per award)
- :class:`ExpertiseTopicRow` -> ``rp_expertise_topics``(one row per label)
- :class:`ArtifactRow`       -> ``rp_artifacts``       (one row per file)
- :class:`ChunkVectorRow`    -> ``rp_chunk_vectors``   (one row per chunk vector)
- :class:`ProfileVectorRow`  -> ``rp_profile_vectors`` (one row per profile vector)
- :class:`BuildStateRow`     -> ``rp_build_state``     (not published; droppable)

Identity: ``rid``, not ``slug``
-------------------------------

The primary key is ``rid``, not ``slug``. ``slug`` is the profile directory
name (a display handle that may be renamed), so keying rows on it would make
a rename a data migration. ``rid`` (a canonical ORCID or a ``local:`` id) is
the stable identity, and it is what a consumer joining these tables against
its own users should join on. ``slug`` survives as a plain column because it is
what humans recognise in query output.

``slug`` is ``unique=True, index=True`` here, and that uniqueness is a
store constraint, not an identity claim: one store cannot hold two profiles
under one handle, exactly as one directory root cannot hold two directories
with one name. Renaming is ``UPDATE rp_profiles SET slug=... WHERE rid=...``
and touches zero child rows (:meth:`ProfileStore.rename`). Nothing anywhere
may read that uniqueness as authority: use ``rid``.

Document of record + derived projections
----------------------------------------

``rp_profiles.document`` holds the whole ``profile.jsonld`` payload and is
the source of truth. ``SqlArtifactStorage.load_document`` reads it and nothing else.
Every other column on ``rp_profiles`` is derived: rebuilt from ``document``
on every write by exactly one writer (:meth:`ProfileRow.from_document`) and
never read back into the model. The same rule holds one level down for
``rp_papers.record`` / ``rp_grants.record``.

The document is not shredded into columns because
:class:`~researcher_profiles.schema.ProfileDocument` and every nested node
inherit ``extra="allow"`` and the format promises unknown terms round-trip
untouched (``docs/rp-spec/index.md``). Shredding
drops them, and turns every new field in ``schema/`` into an ``ALTER TABLE``.
The projections exist because a store nobody can ask "which profiles have an
ORCID / are level=deep / were modified since X" is not worth putting profiles
in Postgres for.

The one column that is neither the document nor a projection of it is
``content_hash``: it is store-maintained derived state, spanning the
document and the SOUL text, refreshed inside the write unit by
``SqlArtifactStorage.refresh_derived`` before the pre-commit hooks run. See
:meth:`researcher_profiles.profile.ResearcherProfile.write_unit`.

JSON storage
------------

Portable :data:`sqlmodel.JSON` columns are used (never Postgres ``JSONB``)
so the same table definitions load on SQLite in tests and on Postgres in
production. Postgres stores these as ``json``; switch to ``JSONB`` in a later
numbered migration if indexed JSON querying is ever needed.

Optional dependency
-------------------

This module requires the ``sql`` extra; see the install instructions in the
README.

``db.py`` and the ``store/sql/`` package are the only modules in the package
that import ``sqlmodel``, and nothing on the core path imports them at module
scope, so the base install stays free of the SQLModel/SQLAlchemy dependency.
"""

import hashlib
import json
from typing import TYPE_CHECKING, Iterator, Optional

from sqlalchemy import LargeBinary, UniqueConstraint
from sqlmodel import JSON, Column, Field, SQLModel

from ..schema.jsonld import canonical_dumps

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import Engine
    from sqlmodel import Session

    from ..schema import ArtifactRef, GrantRecord, PaperRecord, ProfileDocument

__all__ = [
    "DEFAULT_MANIFEST_SLOT",
    "ArtifactRow",
    "BuildStateRow",
    "ChunkVectorRow",
    "ExpertiseTopicRow",
    "GrantRow",
    "PaperRow",
    "ProfileRow",
    "ProfileVectorRow",
    "content_hash_for",
    "create_all",
    "get_engine",
    "get_session",
    "reset_engine",
]

#: The manifest list an artifact belongs to when nothing says otherwise.
#: ``profile.jsonld`` splits its manifest in two: ``subjectOf`` carries the
#: persona documents (things *about* the person), ``hasPart`` everything else.
DEFAULT_MANIFEST_SLOT = "hasPart"


def content_hash_for(document: dict, soul: str) -> str:
    """``"sha256:<hex>"`` over a profile's canonical content.

    Spans two artifacts, NUL-separated: the canonical profile document bytes
    and the SOUL text, byte-for-byte identically to the filesystem backend's
    :meth:`researcher_profiles.profile.storage.ArtifactStorage.content_hash`.
    Backends that disagree here are not interchangeable, so this is the one
    definition and the SQL backend stores its output in a column rather than
    recomputing it per read.
    """
    h = hashlib.sha256()
    h.update(canonical_dumps(document or {}).encode("utf-8"))
    h.update(b"\x00")
    h.update((soul or "").encode("utf-8"))
    return f"sha256:{h.hexdigest()}"


class ProfileRow(SQLModel, table=True):
    """A whole profile document. Primary key: ``rid``.

    ``document`` is the record; everything below it is a derived projection
    rebuilt by :meth:`from_document` on every write. Do not read a projection
    back into a model, and do not write one from anywhere else.
    """

    __tablename__ = "rp_profiles"

    #: The identity: a canonical ORCID or a ``local:`` id.
    rid: str = Field(primary_key=True)
    #: Directory name / display handle. Unique within this store (one store
    #: cannot hold two profiles under one handle): a store constraint, never
    #: an identity claim. Renameable; see :meth:`ProfileStore.rename`.
    slug: str = Field(index=True, unique=True)

    # the record

    #: The whole ``profile.jsonld`` payload. The source of truth.
    document: dict = Field(default_factory=dict, sa_column=Column(JSON))
    #: Store-maintained derived state: sha256 over the canonical document and
    #: the SOUL text (see :func:`content_hash_for`). Refreshed inside the write
    #: unit, before the pre-commit hooks run.
    content_hash: str = Field(default="", index=True)

    # derived projections (rebuilt from ``document`` on every write)

    name: str = ""
    conforms_to: Optional[str] = None
    level: str = Field(default="full", index=True)
    #: Derived from ``rid`` (NULL when the rid is local). A convenience for
    #: ORCID joins; join on ``rid``.
    orcid: Optional[str] = Field(default=None, index=True)
    #: Who asserted this profile and on what basis. Required on disk, so it is
    #: non-null here too: a consumer must never have to guess whether a row
    #: describes a verified self-publication or a third-party assertion.
    provenance: str = Field(default="", index=True)
    #: Reuse terms for the published record (an IRI), when declared.
    license: Optional[str] = None
    url: Optional[str] = None
    affiliation: Optional[str] = None
    affiliation_id: Optional[str] = None
    job_title: Optional[str] = None
    email: Optional[str] = None
    scholar_url: Optional[str] = None
    openalex_id: Optional[str] = None
    field: Optional[str] = None
    summary: Optional[str] = None
    #: Profile-level default privacy tier.
    visibility: str = Field(default="public", index=True)
    #: The published vintage. Nullable and never defaulted: 45 of the 47
    #: profiles in the reference corpus carry no ``dateModified`` at all, and
    #: inventing one would be exactly the confident lie
    #: :mod:`researcher_profiles.utils.date_modified` exists to prevent.
    date_modified: Optional[str] = Field(default=None, index=True)
    has_citation_graph: Optional[bool] = None
    has_embedding_index: Optional[bool] = None
    expertise_cites_paper_ids: Optional[bool] = None

    # List/object-valued projections, as portable JSON blobs.
    subfields: list = Field(default_factory=list, sa_column=Column(JSON))
    interests: list = Field(default_factory=list, sa_column=Column(JSON))
    not_interests: list = Field(default_factory=list, sa_column=Column(JSON))
    methodological_commitments: list = Field(default_factory=list, sa_column=Column(JSON))
    recurring_positions: list = Field(default_factory=list, sa_column=Column(JSON))
    intellectual_lineage: list = Field(default_factory=list, sa_column=Column(JSON))
    critiques: list = Field(default_factory=list, sa_column=Column(JSON))
    collaborators: list = Field(default_factory=list, sa_column=Column(JSON))
    research_outputs: list = Field(default_factory=list, sa_column=Column(JSON))
    same_as: list = Field(default_factory=list, sa_column=Column(JSON))
    identifier: list = Field(default_factory=list, sa_column=Column(JSON))
    training: list = Field(default_factory=list, sa_column=Column(JSON))
    career: list = Field(default_factory=list, sa_column=Column(JSON))
    proof: list = Field(default_factory=list, sa_column=Column(JSON))
    anchor: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    paper_stats: Optional[dict] = Field(default=None, sa_column=Column(JSON))

    # career-stage projections
    #
    # Flat and nullable, because eligibility filtering (NIH ESI, K99, CAREER)
    # is a stated consumer query and none of it is answerable from inside a
    # JSON blob. These are filters; the full ``CareerStage`` node stays in
    # ``document`` and remains the record.

    career_stage_as_of: Optional[str] = Field(default=None, index=True)
    terminal_degree_year: Optional[int] = Field(default=None, index=True)
    terminal_degree_type: Optional[str] = None
    clinical_training_end_year: Optional[int] = None
    first_independent_appointment_year: Optional[int] = Field(default=None, index=True)
    current_rank: Optional[str] = Field(default=None, index=True)
    tenure_status: Optional[str] = None
    independence: Optional[str] = None
    first_r01_equivalent_year: Optional[int] = Field(default=None, index=True)

    @classmethod
    def from_document(
        cls,
        meta: "ProfileDocument",
        *,
        slug: str,
        document: Optional[dict] = None,
        soul: str = "",
    ) -> "ProfileRow":
        """The one writer of ``document`` and every projection derived from it.

        ``meta`` supplies the identity and the projections; ``document`` is the
        exact serialized payload to store, defaulting to ``meta``'s canonical
        dump. Pass it explicitly when ingesting a profile whose stored bytes
        must survive verbatim. A nested node's un-pruned ``null`` is content,
        and re-dumping the model would rewrite it.

        ``rid`` comes from ``meta.rid``; there is no override parameter,
        because a document without a rid cannot load in the first place.
        """
        payload = (
            document
            if document is not None
            else json.loads(canonical_dumps(meta.model_dump(mode="json")))
        )
        return cls(
            rid=meta.rid,
            slug=slug,
            document=payload,
            content_hash=content_hash_for(payload, soul),
            **_scalar_projection(meta),
            **_json_projection(meta),
            **_career_stage_projection(meta),
        )


def _scalar_projection(meta: "ProfileDocument") -> dict:
    """The one-value-per-column projections: the filterable scalars."""
    return dict(
        name=meta.name,
        conforms_to=meta.conforms_to,
        level=str(getattr(meta, "level", "full") or "full"),
        orcid=meta.orcid,
        provenance=str(meta.provenance),
        license=meta.license_,
        url=meta.url,
        affiliation=meta.affiliation,
        affiliation_id=meta.affiliation_id,
        job_title=meta.job_title,
        email=meta.email,
        scholar_url=meta.scholar_url,
        openalex_id=meta.openalex_id,
        field=meta.field,
        summary=meta.summary,
        visibility=str(meta.visibility),
        date_modified=meta.date_modified,
        has_citation_graph=meta.has_citation_graph,
        has_embedding_index=meta.has_embedding_index,
        expertise_cites_paper_ids=meta.expertise_cites_paper_ids,
    )


def _json_projection(meta: "ProfileDocument") -> dict:
    """The list- and object-valued projections, dumped to portable JSON."""
    return dict(
        subfields=list(meta.subfields),
        interests=list(meta.interests),
        not_interests=list(meta.not_interests),
        methodological_commitments=list(meta.methodological_commitments),
        recurring_positions=list(meta.recurring_positions),
        intellectual_lineage=list(meta.intellectual_lineage),
        critiques=list(meta.critiques),
        collaborators=[dict(c) if isinstance(c, dict) else c for c in meta.collaborators],
        research_outputs=[o.model_dump(mode="json") for o in meta.research_outputs],
        same_as=list(meta.same_as),
        identifier=[i.model_dump(mode="json") for i in meta.identifier],
        training=[t.model_dump(mode="json") for t in meta.training],
        career=[c.model_dump(mode="json") for c in meta.career],
        proof=[p.model_dump(mode="json") for p in meta.proof],
        anchor=meta.anchor.model_dump(mode="json") if meta.anchor else None,
        paper_stats=meta.paper_stats.model_dump(mode="json") if meta.paper_stats else None,
    )


def _career_stage_projection(meta: "ProfileDocument") -> dict:
    """The flat eligibility filters lifted off the ``CareerStage`` node.

    Every column is ``None`` when the document carries no career stage.
    """
    stage = meta.career_stage
    return dict(
        career_stage_as_of=stage.as_of if stage else None,
        terminal_degree_year=stage.terminal_degree_year if stage else None,
        terminal_degree_type=stage.terminal_degree_type if stage else None,
        clinical_training_end_year=stage.clinical_training_end_year if stage else None,
        first_independent_appointment_year=(
            stage.first_independent_appointment_year if stage else None
        ),
        current_rank=stage.current_rank if stage else None,
        tenure_status=stage.tenure_status if stage else None,
        independence=stage.independence if stage else None,
        first_r01_equivalent_year=stage.first_r01_equivalent_year if stage else None,
    )


class PaperRow(SQLModel, table=True):
    """One work from ``sources/papers.jsonld``.

    Keyed by position within the profile, never by ``paper_id``. There is
    no ``UNIQUE(profile_rid, paper_id)``: ``paper_id`` is a
    generated citekey and is not unique within a profile in the real corpus.
    13 of 47 reference profiles carry duplicates, and one has 17 genuinely
    distinct works that collided on the same key. A uniqueness constraint here
    would refuse to ingest a quarter of the corpus and would silently redefine
    what a work is. ``ordinal`` is the key that actually holds.

    ``record`` is the whole :class:`~researcher_profiles.schema.PaperRecord`;
    the columns beside it are a derived projection for querying. ``abstract``
    and ``summary`` are not projected: they are large and they live in
    ``record``.

    No build field (``status``, ``identity_verified``, ``contaminated``, ...)
    appears here. Publishing a profile publishes the bibliographic record, not
    the build's dirty laundry; build state lives in ``rp_build_state``.
    """

    __tablename__ = "rp_papers"
    __table_args__ = (UniqueConstraint("profile_rid", "ordinal", name="uq_rp_papers_position"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_rid: str = Field(foreign_key="rp_profiles.rid", index=True)
    #: Position in the collection's ``hasPart``. Re-rendering ``papers.jsonld``
    #: from these rows reproduces the original array order.
    ordinal: int = Field(default=0, index=True)
    #: The whole ``PaperRecord``. The source of truth for this work.
    record: dict = Field(default_factory=dict, sa_column=Column(JSON))

    paper_id: Optional[str] = Field(default=None, index=True)
    title: str = ""
    year: Optional[int] = Field(default=None, index=True)
    journal: Optional[str] = None
    doi: Optional[str] = Field(default=None, index=True)
    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    openalex_id: Optional[str] = None
    first_author: Optional[str] = None
    last_author: Optional[str] = None
    author_position: Optional[str] = None
    author_index: Optional[int] = None
    total_authors: Optional[int] = None
    is_corresponding: Optional[bool] = None
    cited_by_count: Optional[int] = None
    type: Optional[str] = None
    venue: Optional[str] = None
    source: Optional[str] = None
    open_access: Optional[bool] = None
    is_oa: Optional[bool] = None
    oa_status: Optional[str] = None
    url: Optional[str] = None

    @classmethod
    def from_record(cls, profile_rid: str, paper: "PaperRecord", *, ordinal: int) -> "PaperRow":
        """Build a row from a :class:`~researcher_profiles.schema.PaperRecord`."""
        return cls(
            profile_rid=profile_rid,
            ordinal=ordinal,
            record=paper.model_dump(mode="json"),
            paper_id=paper.paper_id,
            title=paper.title,
            year=paper.year,
            journal=paper.journal,
            doi=paper.doi,
            pmid=paper.pmid,
            pmcid=paper.pmcid,
            openalex_id=paper.openalex_id,
            first_author=paper.first_author,
            last_author=paper.last_author,
            author_position=paper.author_position,
            author_index=paper.author_index,
            total_authors=paper.total_authors,
            is_corresponding=paper.is_corresponding,
            cited_by_count=paper.cited_by_count,
            type=paper.type,
            venue=paper.venue,
            source=paper.source,
            open_access=paper.open_access,
            is_oa=paper.is_oa,
            oa_status=paper.oa_status,
            url=paper.url,
        )


class GrantRow(SQLModel, table=True):
    """One award from ``sources/grants.jsonld``.

    Grants earn a table of their own rather than living only inside the
    document because cross-profile grant queries (R01-equivalent history for
    an ESI determination, funder rollups) are the reason grants are in the
    format at all.

    The projection column is ``grant_id``, not ``id``: ``id`` is already the
    autoincrement primary key, and two columns called ``id`` meaning different
    things is how a join goes quietly wrong.
    """

    __tablename__ = "rp_grants"
    __table_args__ = (
        UniqueConstraint("profile_rid", "grant_id", name="uq_rp_grants_profile_grant"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_rid: str = Field(foreign_key="rp_profiles.rid", index=True)
    ordinal: int = Field(default=0, index=True)
    #: The whole ``GrantRecord``. The source of truth for this award.
    record: dict = Field(default_factory=dict, sa_column=Column(JSON))

    #: :attr:`~researcher_profiles.schema.GrantRecord.id`: the award's own id.
    grant_id: str = Field(index=True)
    title: str = ""
    funder: Optional[str] = Field(default=None, index=True)
    number: Optional[str] = None
    activity_code: Optional[str] = Field(default=None, index=True)
    role: Optional[str] = None
    status: Optional[str] = Field(default=None, index=True)
    start: Optional[str] = None
    end: Optional[str] = None
    source: Optional[str] = None
    url: Optional[str] = None

    @classmethod
    def from_record(cls, profile_rid: str, grant: "GrantRecord", *, ordinal: int) -> "GrantRow":
        """Build a row from a :class:`~researcher_profiles.schema.GrantRecord`."""
        return cls(
            profile_rid=profile_rid,
            ordinal=ordinal,
            record=grant.model_dump(mode="json"),
            grant_id=grant.id,
            title=grant.title,
            funder=grant.funder,
            number=grant.number,
            activity_code=grant.activity_code,
            role=grant.role,
            status=grant.status,
            start=grant.start,
            end=grant.end,
            source=grant.source,
            url=grant.url,
        )


class ExpertiseTopicRow(SQLModel, table=True):
    """One expertise label belonging to a profile.

    ``ordinal`` preserves the declared order: the labels are a curated, ordered
    list in ``profile.jsonld``, not a set.
    """

    __tablename__ = "rp_expertise_topics"

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_rid: str = Field(foreign_key="rp_profiles.rid", index=True)
    ordinal: int = Field(default=0, index=True)
    topic: str


class ArtifactRow(SQLModel, table=True):
    """One file the profile contains. ``rp_artifacts`` is the manifest.

    Keyed ``(profile_rid, content_url)``, the manifest's own address space,
    so ``personality/SOUL.md``, ``personality/expertise.md``,
    ``sources/summaries/<paper_id>.summary.md``, ``sources/cv.md``,
    ``sources/papers/<id>.md``, ``sources/web/<n>-<host>.md``, ``SKILL.md``,
    ``index.html``, ``sources/citations.json`` and ``embeddings/index.json``
    all land in one uniform place: retrievable by role, and privacy-filterable
    with ``WHERE visibility = 'public'`` instead of a second implementation of
    :func:`researcher_profiles.privacy.effective_tiers`.

    The columns mirror :class:`~researcher_profiles.schema.ArtifactRef` one for
    one, with one rename: ``ArtifactRef.bytes`` is ``size_bytes`` here,
    because ``bytes`` cannot be a column name. The mapping is
    ``ArtifactRef.bytes <-> ArtifactRow.size_bytes``, both directions, and
    :meth:`from_part` / :meth:`to_part` are the only places it is applied.

    Bodies: ``text`` for markdown / JSON / JSON-LD / HTML, ``data`` for binary.
    Binary bodies are opt-in (``ProfileStore.put(..., include_binary=True)``)
    because ``.cache/embeddings.sqlite`` can be tens of megabytes.

    ``rendered=True`` marks an artifact whose bytes are regenerated from the
    relational tables (``sources/papers.jsonld``, ``sources/grants.jsonld``);
    both bodies are NULL for those and ``envelope`` holds their collection node
    minus ``hasPart``, so a round-trip is byte-identical.
    """

    __tablename__ = "rp_artifacts"
    __table_args__ = (
        UniqueConstraint("profile_rid", "content_url", name="uq_rp_artifacts_address"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_rid: str = Field(foreign_key="rp_profiles.rid", index=True)
    #: The relative path, exactly as ``ArtifactRef.contentUrl``.
    content_url: str = Field(index=True)

    #: Which manifest list this entry belongs to: ``hasPart`` or ``subjectOf``.
    manifest_slot: str = Field(default=DEFAULT_MANIFEST_SLOT, index=True)
    ordinal: int = Field(default=0, index=True)

    # ArtifactRef mirror
    name: Optional[str] = None
    role: Optional[str] = Field(default=None, index=True)
    encoding_format: Optional[str] = None
    type_: Optional[str] = None
    paper_id: Optional[str] = Field(default=None, index=True)
    visibility: str = Field(default="public", index=True)
    derived_from: list = Field(default_factory=list, sa_column=Column(JSON))
    sha256: Optional[str] = None
    #: ``ArtifactRef.bytes``. Renamed; see the class docstring.
    size_bytes: Optional[int] = None

    # body
    text: Optional[str] = None
    data: Optional[bytes] = Field(default=None, sa_column=Column(LargeBinary))

    rendered: bool = Field(default=False, index=True)
    #: For a rendered collection: the node minus ``hasPart`` (``about``,
    #: ``dateModified``, ``@context``, ``conformsTo``, ...).
    envelope: Optional[dict] = Field(default=None, sa_column=Column(JSON))

    @classmethod
    def from_part(
        cls,
        profile_rid: str,
        part: "ArtifactRef",
        *,
        manifest_slot: str = DEFAULT_MANIFEST_SLOT,
        ordinal: int = 0,
        text: Optional[str] = None,
        data: Optional[bytes] = None,
        rendered: bool = False,
        envelope: Optional[dict] = None,
    ) -> "ArtifactRow":
        """Build a row from one manifest entry plus its body."""
        return cls(
            profile_rid=profile_rid,
            content_url=part.content_url,
            manifest_slot=manifest_slot,
            ordinal=ordinal,
            name=part.name,
            role=part.role,
            encoding_format=part.encoding_format,
            type_=part.type_,
            paper_id=part.paper_id,
            visibility=str(part.visibility),
            derived_from=list(part.derived_from or []),
            sha256=part.sha256,
            size_bytes=part.bytes,
            text=text,
            data=data,
            rendered=rendered,
            envelope=envelope,
        )

    def to_part(self) -> "ArtifactRef":
        """Rebuild the manifest entry this row records."""
        from ..schema import ArtifactRef

        return ArtifactRef(
            **{
                "@type": self.type_,
                "name": self.name,
                "encodingFormat": self.encoding_format,
                "contentUrl": self.content_url,
                "role": self.role,
                "paperId": self.paper_id,
                "bytes": self.size_bytes,
                "sha256": self.sha256,
                "visibility": self.visibility,
                "derivedFrom": list(self.derived_from or []),
            }
        )

    def apply_part(self, part: "ArtifactRef") -> None:
        """Copy a manifest entry's fields onto this row, in place.

        The write path for a document whose manifest changed (the SQL store's
        ``save_profile``, reached from ``edit.set_visibility`` and ``rp profile
        visibility``): the entry's metadata lands on the row, and the document's
        manifest is then regenerated from the rows, so the two cannot disagree.
        """
        self.name = part.name
        self.role = part.role
        self.encoding_format = part.encoding_format
        self.type_ = part.type_
        self.paper_id = part.paper_id
        self.visibility = str(part.visibility)
        self.derived_from = list(part.derived_from or [])
        self.sha256 = part.sha256
        self.size_bytes = part.bytes


class ChunkVectorRow(SQLModel, table=True):
    """One chunk's embedding, as a queryable row. ``rp_chunk_vectors``.

    The SQL store's vectors, shredded out of the per-profile
    ``.cache/embeddings.sqlite`` at ingest so they can be *selected* rather than
    unpacked. Before this table the whole index file lived in a single
    ``rp_artifacts`` BLOB, which meant a SQL-backed deployment had to export
    every profile to a temp directory before it could rank anything.

    Storage: ``vector`` is row-major little-endian float32, the same bytes
    :func:`researcher_profiles.embeddings._sqlite.serialize_vec` writes, in a
    portable ``LargeBinary`` column. Deliberately not sqlite-vec (a SQLite-only
    extension needing raw ``CREATE VIRTUAL TABLE`` DDL) and not pgvector
    (Postgres-only): these tables must load unchanged on SQLite in tests and on
    Postgres in production, which is the whole point of this module. Cosine runs
    in numpy on the read side, through
    :class:`~researcher_profiles.embeddings.flat.FlatEmbeddingIndex`, which is
    brute force over a few hundred rows per profile and is exactly what the
    filesystem and HTTP backends already do.

    Rows are the *public* subset, the same one
    :func:`~researcher_profiles.embeddings.flat.write_flat_export` publishes:
    embeddings are partially invertible, so a chunk built from a restricted
    source never reaches this table any more than it reaches a ``.bin``.
    ``text`` is not stored for the same reason the published form drops it.

    The key mirrors the sqlite index's own ``UNIQUE(source_type, source_id,
    chunk_index)``, scoped to the profile.
    """

    __tablename__ = "rp_chunk_vectors"
    __table_args__ = (
        UniqueConstraint(
            "profile_rid",
            "source_type",
            "source_id",
            "chunk_index",
            name="uq_rp_chunk_vectors_address",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_rid: str = Field(foreign_key="rp_profiles.rid", index=True)

    #: Blob order: ``source_type, source_id, chunk_index``, assigned at insert
    #: so a read can restore the published row order without re-sorting.
    ordinal: int = Field(default=0, index=True)

    source_type: str = Field(index=True)
    source_id: str = Field(index=True)
    chunk_index: int = 0
    #: ``chunks.json`` mirror; carried so the flat form round-trips exactly.
    section: Optional[str] = None
    char_count: Optional[int] = None

    dim: int = 0
    backend_spec: str = Field(default="", index=True)
    #: Row-major little-endian float32, ``dim`` floats. See the class docstring.
    vector: bytes = Field(sa_column=Column(LargeBinary))


class ProfileVectorRow(SQLModel, table=True):
    """One profile-level vector. ``rp_profile_vectors``.

    ``kind="centroid"`` is the one that exists today: the L2-normalized mean of
    every chunk vector in the profile's index, computed at ingest by the same
    function the filesystem backend uses
    (:func:`researcher_profiles.embeddings.profile_vec._centroid_vec`) so the
    two backends report the same number for the same profile.

    Separate from :class:`ChunkVectorRow` because the registry's hot path wants
    the whole roster's centroids in one ``SELECT`` of N rows, not N scans of
    every profile's chunks. This table is the relational form of the published
    ``collection/embeddings/<backend>.bin``.

    ``kind`` is part of the key so ``summary`` / ``expertise`` vectors can join
    later without a migration.
    """

    __tablename__ = "rp_profile_vectors"

    profile_rid: str = Field(primary_key=True, foreign_key="rp_profiles.rid")
    kind: str = Field(default="centroid", primary_key=True)

    dim: int = 0
    backend_spec: str = Field(default="", index=True)
    #: Little-endian float32, ``dim`` floats. Already L2-normalized.
    vector: bytes = Field(sa_column=Column(LargeBinary))


class BuildStateRow(SQLModel, table=True):
    """Build bookkeeping. Not part of the published record.

    The SQL analogue of ``.build/<slug>/`` being a sibling tree outside the
    content root. "Publish this store" means copy ``rp_profiles``,
    ``rp_papers``, ``rp_grants``, ``rp_expertise_topics`` and ``rp_artifacts``
    and not copy this table; ``DROP TABLE rp_build_state`` must stay as
    free as ``rm -rf .build/``, and every profile must still load afterwards.

    It is never rendered into the manifest, never reaches a
    :class:`~researcher_profiles.profile.export.ProfileExportBundle`, and
    :meth:`SqlProfileStore.export_directory` writes it only when passed
    ``with_build=True``.

    The state is stored whole rather than shredded because
    :class:`~researcher_profiles.build_state.BuildState` keeps a build-local
    integer ``schema_version`` and makes no external promise; giving its fields
    columns would create one.
    """

    __tablename__ = "rp_build_state"

    profile_rid: str = Field(primary_key=True, foreign_key="rp_profiles.rid")
    #: ``BuildState``'s own private counter. Build-local; never published.
    schema_version: int = 0
    state: dict = Field(default_factory=dict, sa_column=Column(JSON))


# ---------------------------------------------------------------------------
# Engine + session helpers
#
# Imported lazily inside the functions so that a plain
# ``from researcher_profiles.store.db import ProfileRow`` (the [sql] tier) keeps
# working with only sqlmodel installed, and a caller passing an explicit URL
# never pays for a config-file read (the same deferral
# ``config.resolve_profiles_root`` documents).
# ---------------------------------------------------------------------------

_ENGINE: Optional["Engine"] = None
_ENGINE_URL: Optional[str] = None


def create_all(engine: "Engine") -> None:
    """Create every ``rp_*`` table on ``engine`` if not present.

    Fresh-instance convenience only. On a deployed Postgres, column evolution
    goes through numbered SQL migration files, not this::

        from sqlmodel import create_engine
        from researcher_profiles.store.db import create_all

        engine = create_engine("postgresql://user@host/db")
        create_all(engine)
    """
    SQLModel.metadata.create_all(engine)


def get_engine(url: Optional[str] = None, *, echo: bool = False) -> "Engine":
    """Return (building once per URL) the process-wide SQLAlchemy engine.

    The URL resolves through :func:`researcher_profiles.store.config.resolve_database_url`:
    explicit argument, then ``$RESEARCHER_PROFILES_DATABASE_URL``. SQLite URLs get
    ``check_same_thread=False`` so one dev/in-memory database is shared across
    a threadpool.
    """
    global _ENGINE, _ENGINE_URL
    from sqlmodel import create_engine

    from .config import resolve_database_url

    resolved = resolve_database_url(url)
    if _ENGINE is None or _ENGINE_URL != resolved:
        reset_engine()
        connect_args = {"check_same_thread": False} if resolved.startswith("sqlite") else {}
        _ENGINE = create_engine(resolved, echo=echo, connect_args=connect_args)
        _ENGINE_URL = resolved
    return _ENGINE


def reset_engine() -> None:
    """Drop the cached engine (tests reconfigure the database between cases)."""
    global _ENGINE, _ENGINE_URL
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _ENGINE_URL = None


def get_session() -> Iterator["Session"]:
    """Yield a :class:`~sqlmodel.Session` bound to the shared engine.

    Generator form, so it plugs straight into FastAPI's ``Depends`` and can be
    driven manually elsewhere via ``next(get_session())``.
    """
    from sqlmodel import Session

    with Session(get_engine()) as session:
        yield session
