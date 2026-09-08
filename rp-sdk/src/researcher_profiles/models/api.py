"""Pydantic models shared by the FastAPI server and the HTTP client.

These types define the wire contract for ``researcher_profiles.api`` and
``researcher_profiles.client.ApiArtifactStorage``. They are distinct from the
on-disk pydantic models in ``schema/`` so the two layers
can evolve independently, but the field names line up where reasonable.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _APIModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# Profile resources
# ---------------------------------------------------------------------------


class ProfileListEntry(_APIModel):
    """One entry in an ``rp:profileList`` response.

    Extends :class:`ProfileSummary` with a ``url`` field so consumers can
    discover the profile's content base URL from the listing alone.
    """

    url: str
    slug: str
    rid: Optional[str] = None
    name: str
    level: str = "full"
    affiliation: Optional[str] = None
    field: Optional[str] = None
    paper_count: int = 0
    summary_count: int = 0
    fulltext_pct: float = 0.0
    contaminated_count: int = 0


class ProfileListResponse(_APIModel):
    """The ``rp:profileList`` envelope returned by ``GET /profiles``."""

    rp_profileList: str = Field("0.1", alias="rp:profileList")
    name: Optional[str] = None
    url: Optional[str] = None
    updated: Optional[str] = None
    profiles: list[ProfileListEntry] = []


class ProfileSummary(_APIModel):
    #: The profile directory name: a display handle, and the path segment
    #: this profile is served under. Not identity; may be renamed.
    slug: str
    #: The identity: a canonical ORCID or a ``local:`` id. This is the join
    #: key consumers should map onto their own users. The document requires
    #: it; ``None`` here means the summary was built from a document that
    #: failed to load.
    rid: Optional[str] = None
    name: str
    level: str = "full"
    affiliation: Optional[str] = None
    field: Optional[str] = None
    # Corpus stats (populated by the server; clients use these for the
    # list view's numeric columns). 0 when the profile has no papers.jsonld.
    paper_count: int = 0
    summary_count: int = 0
    fulltext_pct: float = 0.0
    contaminated_count: int = 0


class ProfileMetadataPayload(_APIModel):
    """Wire shape of ``profile.jsonld``-derived metadata.

    Mirrors the on-disk :class:`~researcher_profiles.schema.ProfileDocument`
    but uses ``extra="allow"`` so arbitrary keys round-trip cleanly between
    server and client. This is not JSON-LD: the wire contract and
    the on-disk format evolve independently, and a client that wants the
    published bytes fetches ``/profiles/{slug}/profile.jsonld`` instead.

    The derived ``orcid`` field is gone: ``rid`` is the join key and
    ``orcid_of(rid)`` is one call away.
    """

    name: str
    level: str = "full"
    #: Identity: a canonical ORCID or a ``local:`` id.
    rid: Optional[str] = None
    #: Who asserted this profile and on what basis. See ``schema.Provenance``.
    provenance: Optional[str] = None
    #: Reuse terms for the published record (an IRI), when declared.
    license: Optional[str] = None
    #: The published profile URL.
    url: Optional[str] = None
    affiliation: Optional[str] = None
    scholar_url: Optional[str] = None
    openalex_id: Optional[str] = None
    field: Optional[str] = None
    subfields: list[str] = []
    summary: Optional[str] = None
    training: list[dict] = []
    career: list[dict] = []
    expertise: list[str] = []
    #: Declared below, not left to ``extra="allow"``. These values already rode
    #: the wire (``_metadata_payload`` dumps the whole document), but only as
    #: untyped extras, so the generated TypeScript saw ``[k: string]: unknown``
    #: and no owner form could round-trip a field it could not read back typed.
    job_title: Optional[str] = None
    interests: list[str] = []
    not_interests: list[str] = []
    same_as: list[str] = []
    #: The document's own declared tier. Read-only here: it is set through
    #: ``PATCH /visibility`` (or, on a management host, the publication act), never by a
    #: metadata patch, and it is not what decides who may read this response.
    #: The server-side tier projection already did that.
    visibility: str = "public"


class ProfileDetail(_APIModel):
    slug: str
    #: See :attr:`ProfileSummary.rid`.
    rid: Optional[str] = None
    metadata: ProfileMetadataPayload
    #: Markdown body of ``personality/expertise.md``, or ``None`` when this
    #: viewer's tier does not reach it. ``None``, never ``""``: a client must be
    #: able to tell withheld from empty, and an owner shown an empty box would
    #: reasonably conclude that publishing had worked.
    expertise: Optional[str] = None
    #: Markdown body of ``personality/SOUL.md``. See :attr:`expertise`.
    soul: Optional[str] = None
    #: The profile manifest (``hasPart`` + ``subjectOf`` entries), so a client
    #: knows what the profile contains without walking the directory. Served
    #: whole at every tier (spec section 6), with each entry carrying an
    #: ``effective_visibility`` key so no client recomputes the derivation rule.
    manifest: list[dict] = []
    #: The ``contentUrl``s this viewer did not receive. What makes a preview
    #: legible rather than merely correct: an owner previewing as a stranger can
    #: see the shape of what the stranger is missing.
    withheld: list[str] = []
    #: ``"sha256:<hex>"`` over the document + SOUL. An editor reads it here and
    #: sends it back on the next patch; if it no longer matches, the patch is a
    #: 409 instead of a silent overwrite of somebody else's edit.
    content_hash: Optional[str] = None


class PaperEntry(_APIModel):
    paper_id: Optional[str] = None
    title: str
    year: Optional[int] = None
    journal: Optional[str] = None
    first_author: Optional[str] = None
    full_text_link: Optional[str] = None
    summary_available: bool = False
    #: Bibliographic identifiers, carried from the on-disk
    #: :class:`~researcher_profiles.schema.PaperRecord`. Without them a remote
    #: consumer holds a title and has to guess which work it names. A
    #: title search is a different paper away from wrong. They come off the
    #: same ``sources/papers.jsonld`` artifact as ``title``/``year``, so they
    #: are gated by exactly the tier that already gates this route and widen
    #: nothing: an identifier is a pointer at the open bibliographic record,
    #: never at content.
    doi: Optional[str] = None
    pmid: Optional[str] = None
    openalex_id: Optional[str] = None
    #: Full author list where the record carries one (``first_author`` alone
    #: cannot answer a co-authorship question).
    authors: Optional[list[str]] = None


class PaperSummary(_APIModel):
    paper_id: str
    summary: str


class PushResponse(_APIModel):
    """Result of ``PUT /api/v1/profiles/{slug}`` (a pushed profile)."""

    slug: str
    rid: Optional[str] = None
    name: str
    level: str = "full"
    # True when the pushed profile carries a built embedding index
    # (.cache/embeddings.sqlite), i.e. it is immediately matchable.
    indexed: bool = False


class ResolveRequest(_APIModel):
    """Body of ``POST /api/v1/identity/resolve``. At least one of ``rid``/
    ``name``. ``rid`` is an ORCID or a ``local:`` id the resolver minted (the
    disambiguation round-trip).

    ``create_new`` is the answer to a deferral whose candidates are all
    wrong: it skips matching and mints a fresh identity for ``name``. The
    resolver picks the id, so calling it twice creates two people. Send it
    only after a human has looked at the candidates.
    """

    rid: Optional[str] = None
    name: Optional[str] = None
    affiliation: Optional[str] = None
    create_new: bool = False


class ResolveCandidate(_APIModel):
    """One profile an undecidable name could mean, mirroring ``resolve.Candidate``."""

    rid: str
    name: str
    affiliation: Optional[str] = None


class ResolveResponse(_APIModel):
    """Result of ``POST /api/v1/identity/resolve``.

    ``rid`` is ``None`` only in the deferred case; ``candidates`` is present
    only then too (see ``resolve.ResolveResult``, which this mirrors on the
    wire). ``confidence`` is ``"exact"`` | ``"high"`` | ``"low"``.
    """

    rid: Optional[str] = None
    created: bool
    confidence: str
    candidates: list[ResolveCandidate] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Owner-scoped edits (interactive per-field mutation, not tarball push)
# ---------------------------------------------------------------------------


class MetadataPatch(_APIModel):
    """Owner-editable metadata fields. Every field is optional; only the ones
    present are applied. Unknown keys are allowed on the wire (``extra="allow"``)
    but rejected server-side against the editable whitelist in
    :mod:`researcher_profiles.profile.edit`.
    """

    name: Optional[str] = None
    affiliation: Optional[str] = None
    job_title: Optional[str] = None
    field: Optional[str] = None
    subfields: Optional[list[str]] = None
    summary: Optional[str] = None
    expertise: Optional[list[str]] = None
    interests: Optional[list[str]] = None
    not_interests: Optional[list[str]] = None
    #: Authored history. Kept as ``list[dict]`` on the wire: the
    #: entries are validated against ``schema.Training`` / ``schema.CareerEntry``
    #: inside ``profile.edit.patch_metadata``, so a malformed entry is a 400
    #: naming the entry rather than a 422 about the request body, and one set of
    #: rules governs both.
    training: Optional[list[dict]] = None
    career: Optional[list[dict]] = None
    same_as: Optional[list[str]] = None
    #: Optimistic concurrency: the ``content_hash`` this edit was composed
    #: against. Omitted, the patch is last-writer-wins (which is what a
    #: single-owner CLI wants); supplied and stale, the patch is a 409 carrying
    #: the current hash.
    base_hash: Optional[str] = None


class SoulUpdate(_APIModel):
    """Replacement body for ``personality/SOUL.md``."""

    soul: str
    #: See :attr:`MetadataPatch.base_hash`. The digest spans the document and
    #: the SOUL, so a soul write and a metadata write conflict with each other,
    #: which is exactly right: they are two halves of one profile.
    base_hash: Optional[str] = None


class ArtifactVisibility(_APIModel):
    """One per-artifact visibility change. Supply exactly one selector
    (``content_url``, ``paper_id``, or ``role``) plus the target ``visibility``.
    """

    content_url: Optional[str] = None
    paper_id: Optional[str] = None
    role: Optional[str] = None
    visibility: str


class VisibilityPatch(_APIModel):
    """Set the profile-level default tier and/or per-artifact tiers."""

    profile_visibility: Optional[str] = None
    artifacts: list[ArtifactVisibility] = []
    #: See :attr:`MetadataPatch.base_hash`. Tiers live in the document, so a
    #: visibility write shares the one clock with metadata and soul writes.
    base_hash: Optional[str] = None


class EditResult(_APIModel):
    """Result of an owner edit: the slug and the fields that changed."""

    slug: str
    rid: Optional[str] = None
    updated: list[str] = []
    #: How many manifest artifacts a visibility patch actually re-tiered. An
    #: owner who asks to hide "my paper summaries" needs to be told whether that
    #: was one of them or all sixty-three.
    artifacts_changed: int = 0
    #: The ``content_hash`` after this write. An editor holding a form open
    #: sends it as the next ``base_hash`` without re-reading the profile.
    content_hash: Optional[str] = None


class ArtifactTier(_APIModel):
    """One artifact's tier, and why: the read side of the visibility API.

    Every field is computed by ``privacy.explain_tiers``; nothing here is a
    restatement of the rule in prose. ``visible_to`` and the report's ``counts``
    come from ``privacy.tier_allows``, so an interface renders a consequence
    ("a stranger can see 0 of 63 items") instead of teaching a tier lattice.
    """

    content_url: str
    role: Optional[str] = None
    name: Optional[str] = None
    paper_id: Optional[str] = None
    #: What is written on the manifest entry.
    declared: str
    #: What actually governs, after the legal floor, the profile default, and
    #: the derivation rule.
    effective: str
    #: A legal floor: no one, owner included, may raise this.
    locked: bool = False
    #: The full sentence to show a human when ``locked``.
    lock_reason: Optional[str] = None
    #: Concrete causes holding it above ``declared`` ("derived from
    #: sources/cv.md (restricted)"), for display on this row.
    raised_by: list[str] = []
    #: Subset of ``["anonymous", "lab", "you"]``.
    visible_to: list[str] = []


class VisibilityReport(_APIModel):
    """``GET /profiles/{slug}/visibility``: what is published, and to whom."""

    slug: str
    rid: Optional[str] = None
    profile_visibility: str
    #: A host-imposed ceiling on this profile ("nobody has claimed it"), or
    #: ``None``. An interface disables the options above it rather than
    #: accepting a choice the server will override.
    profile_floor: Optional[str] = None
    profile_floor_reason: Optional[str] = None
    artifacts: list[ArtifactTier] = []
    #: ``{"anonymous": 0, "lab": 12, "you": 63}``: items each viewer can see.
    counts: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchRequest(_APIModel):
    query: str
    k: int = 5
    filter: Optional[dict] = None


class SearchHitPayload(_APIModel):
    text: str
    source_type: str
    source_id: str
    chunk_index: int
    section: Optional[str] = None
    cosine: float
    score: float
    meta: dict = {}


class SearchResponse(_APIModel):
    hits: list[SearchHitPayload]


# ---------------------------------------------------------------------------
# Cross-profile matching (store.match.rank over HTTP)
# ---------------------------------------------------------------------------


class MatchRequest(_APIModel):
    query: str
    k: int = 5
    prefilter: int = 10
    require_topics: Optional[list[str]] = None
    diversify: bool = True
    lambda_: float = 0.5
    topk_chunks: int = 5
    normalize: bool = True
    include_chunks: bool = False


class MatchEvidencePayload(_APIModel):
    centroid_score: float
    top_papers: list[str] = []
    overlapping_topics: list[str] = []
    # Populated only when the request sets include_chunks=True.
    top_chunks: list["SearchHitPayload"] = []


class MatchResult(_APIModel):
    #: Directory name / display handle. Useful for links; not a join key.
    slug: str
    name: str
    #: The join key: a canonical ORCID or a ``local:`` id. Consumers mapping a
    #: match back to their own users must resolve on this. ``None`` only for
    #: an unmigrated profile, and a consumer seeing None should fail loudly
    #: rather than fall back to name matching, which silently binds the wrong
    #: human when two researchers share a name.
    rid: Optional[str] = None
    # Derived from `rid` (None when the rid is local). Retained for consumers
    # that predate `rid`.
    orcid: Optional[str] = None
    score: float
    evidence: MatchEvidencePayload


class MatchResponse(_APIModel):
    matches: list[MatchResult]
    #: How many of the entries below are actually ranked results (post
    #: privacy-tier filtering). Together with ``total_profiles`` this is what
    #: lets a caller tell "47 of 47 ranked" from "0 of 47 ranked". An empty
    #: ``matches`` list alone cannot distinguish "nobody matched" from a
    #: broken embedding path.
    ranked_profiles: int
    #: The size of the indexed corpus this query was ranked against.
    total_profiles: int


# ---------------------------------------------------------------------------
# Profile graph: COI, COI-filtered reviewer match, neighborhood
# ---------------------------------------------------------------------------


class AuthorDescriptor(_APIModel):
    """One manuscript author, as a consumer supplies it for a COI query.

    Any subset may be given; resolution prefers ``rid``/``orcid`` (exact),
    then a name match against the profile set, then an external node keyed by the
    folded name. ``affiliation`` (optionally with a ``affiliation_id`` ROR) lets a
    same-institution COI be caught even when the author has no profile.
    """

    name: Optional[str] = None
    orcid: Optional[str] = None
    rid: Optional[str] = None
    affiliation: Optional[str] = None
    affiliation_id: Optional[str] = None


class CoiReasonPayload(_APIModel):
    author_key: str
    type: str  # coauthor | shared_institution | advised
    author_name: Optional[str] = None
    author_rid: Optional[str] = None
    last_year: Optional[int] = None
    in_window: Optional[bool] = None
    paper_count: Optional[int] = None
    institution: Optional[str] = None
    overlap_years: Optional[bool] = None
    direction: Optional[str] = None
    confidence: str = "medium"


class CoiBlock(_APIModel):
    has_coi: bool
    reasons: list[CoiReasonPayload] = []


class CoiCheckRequest(_APIModel):
    author_set: list[AuthorDescriptor] = []
    candidate: str
    years: int = 4


class CoiCheckResponse(_APIModel):
    candidate: str
    rid: Optional[str] = None
    has_coi: bool
    reasons: list[CoiReasonPayload] = []


class ReviewerMatchRequest(MatchRequest):
    """A ``/match`` request plus the COI filter parameters.

    ``mode='drop'`` removes conflicted candidates (the default); ``'annotate'``
    keeps them and attaches a ``coi`` block naming the conflict.
    """

    author_set: list[AuthorDescriptor] = []
    years: int = 4
    mode: str = "drop"  # drop | annotate


class ReviewerMatchResult(MatchResult):
    #: Present when a candidate has a COI (always, in annotate mode; only on
    #: retained-but-flagged candidates otherwise; drop mode omits conflicted
    #: candidates entirely, so their block never ships).
    coi: Optional[CoiBlock] = None


class ReviewerMatchResponse(_APIModel):
    matches: list[ReviewerMatchResult]


class GraphNodePayload(_APIModel):
    key: str
    kind: str
    rid: Optional[str] = None
    slug: Optional[str] = None
    name: Optional[str] = None


class GraphEdgePayload(_APIModel):
    src: str
    dst: str
    type: str
    directed: bool = False
    confidence: str = "medium"
    paper_count: int = 0
    first_year: Optional[int] = None
    last_year: Optional[int] = None
    institution: Optional[str] = None
    overlap_years: Optional[bool] = None
    training_kind: Optional[str] = None
    evidence: list[str] = []


class NeighborPayload(_APIModel):
    node: GraphNodePayload
    hops: int
    edges: list[GraphEdgePayload] = []


class NeighborsResponse(_APIModel):
    center: GraphNodePayload
    neighbors: list[NeighborPayload] = []


# ---------------------------------------------------------------------------
# Work ranking (candidate works ranked against one profile, the inverse of /match)
# ---------------------------------------------------------------------------


class RankWorksRequest(_APIModel):
    """Rank candidate works against one profile's embedding vector.

    Two modes: supply candidate ``works`` (PaperRecord-shaped dicts), or set
    ``use_openalex=true`` to fetch candidates published since ``since`` from
    OpenAlex using the profile's own topics and citation neighborhood.
    Ranking field names mirror :class:`MatchRequest`.
    """

    since: Optional[str] = None  # YYYY-MM-DD; default: 30 days back
    k: int = 10
    kind: str = "centroid"  # centroid | summary | expertise
    diversify: bool = True
    lambda_: float = 0.5
    threshold: Optional[float] = None
    use_openalex: bool = False
    works: Optional[list[dict]] = None
    mailto: Optional[str] = None
    max_pages: int = 5


class RankedWorkPayload(_APIModel):
    title: str
    year: Optional[int] = None
    journal: Optional[str] = None
    first_author: Optional[str] = None
    doi: Optional[str] = None
    openalex_id: Optional[str] = None
    score: float
    #: Overlapping profile topic labels supporting the score.
    evidence: list[str] = []


class RankWorksResponse(_APIModel):
    slug: str
    rid: Optional[str] = None
    works: list[RankedWorkPayload]


# ---------------------------------------------------------------------------
# LLM endpoints
# ---------------------------------------------------------------------------


class AskRequest(_APIModel):
    question: str
    k: int = 5
    model: Optional[str] = None
    strict_corpus: bool = False
    refusal_threshold: Optional[float] = None
    history: Optional[list[dict]] = None


class ReviewRequest(_APIModel):
    material: str
    focus: Optional[str] = None
    k: int = 5
    model: Optional[str] = None
    strict_corpus: bool = False
    refusal_threshold: Optional[float] = None


class InnovateRequest(_APIModel):
    topic: str
    n: int = 3
    k: int = 12
    model: Optional[str] = None
    temperature: float = 0.7


class RiffRequest(_APIModel):
    seed: str
    n: int = 5
    k: int = 4
    model: Optional[str] = None
    temperature: float = 1.0


class CitationRefPayload(_APIModel):
    paper_id: str
    relevance: Optional[float] = None
    span: Optional[str] = None


class LLMTextResponse(_APIModel):
    text: str
    citations: list[CitationRefPayload] = []
    model: str
    usage: dict = {}
    request_id: Optional[str] = None
    refused: bool = False
    refusal_reason: Optional[str] = None
    grounded: bool = True


class IdeaPayload(_APIModel):
    hypothesis: str
    approach: str
    rationale: str
    related_works: list[str] = []


class IdeaList(_APIModel):
    items: list[IdeaPayload]


class RiffPayload(_APIModel):
    angle: str
    text: str
    related_work: Optional[str] = None


class RiffList(_APIModel):
    items: list[RiffPayload]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(_APIModel):
    status: str
    #: A display locator for the store being served: a directory path or a
    #: database URL.
    store: str
    profile_count: int
    #: Set only when ``status`` is degraded: why, e.g. the startup
    #: query-embedding preflight's exception. See ``app.state.embedding_healthy``.
    detail: Optional[str] = None


__all__ = [
    "ArtifactVisibility",
    "AskRequest",
    "AuthorDescriptor",
    "CoiBlock",
    "CoiCheckRequest",
    "CoiCheckResponse",
    "CoiReasonPayload",
    "GraphEdgePayload",
    "GraphNodePayload",
    "NeighborPayload",
    "NeighborsResponse",
    "ReviewerMatchRequest",
    "ReviewerMatchResponse",
    "ReviewerMatchResult",
    "CitationRefPayload",
    "EditResult",
    "HealthResponse",
    "IdeaList",
    "IdeaPayload",
    "InnovateRequest",
    "LLMTextResponse",
    "MatchEvidencePayload",
    "MatchRequest",
    "MatchResponse",
    "MatchResult",
    "MetadataPatch",
    "PaperEntry",
    "PaperSummary",
    "ProfileDetail",
    "ProfileMetadataPayload",
    "ProfileSummary",
    "PushResponse",
    "RankWorksRequest",
    "RankWorksResponse",
    "RankedWorkPayload",
    "ReviewRequest",
    "RiffList",
    "RiffPayload",
    "RiffRequest",
    "SearchHitPayload",
    "SearchRequest",
    "SearchResponse",
    "SoulUpdate",
    "VisibilityPatch",
]
