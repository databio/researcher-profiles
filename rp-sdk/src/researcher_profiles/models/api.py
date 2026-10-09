"""Pydantic models shared by the FastAPI server and the HTTP client.

The wire contract for ``researcher_profiles.api`` and
``researcher_profiles.client.ApiArtifactStorage``. Kept separate from the
on-disk models in ``schema/`` so the two can evolve independently.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _APIModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# Profile resources
# ---------------------------------------------------------------------------


class ProfileListEntry(_APIModel):
    """One entry in an ``rp:profileList`` response.

    ``url`` is the profile's content base URL.
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
    #: True when the caller's view of the profile lists therapeutic areas:
    #: a clinical researcher. Follows the section's privacy for this caller.
    clinical: bool = False


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
    #: The identity: a canonical ORCID or a ``local:`` id, and the join key
    #: consumers map onto their own users. ``None`` means the document failed
    #: to load.
    rid: Optional[str] = None
    name: str
    level: str = "full"
    affiliation: Optional[str] = None
    field: Optional[str] = None
    # Corpus stats. 0 when the profile has no papers.jsonld.
    paper_count: int = 0
    summary_count: int = 0
    fulltext_pct: float = 0.0
    contaminated_count: int = 0
    #: True when the caller's view of the profile lists therapeutic areas:
    #: a clinical researcher. Follows the section's privacy for this caller.
    clinical: bool = False


class ProfileMetadataPayload(_APIModel):
    """Wire shape of ``profile.jsonld``-derived metadata.

    Mirrors the on-disk :class:`~researcher_profiles.schema.ProfileDocument`
    but allows extra keys and is not JSON-LD. For the published bytes, fetch
    ``/profiles/{slug}/profile.jsonld``.
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
    job_title: Optional[str] = None
    #: Display projections of :attr:`research_interests` when that is set
    #: (positive weights, negative weights), and free-text lists otherwise.
    #: Patching either list writes declared text-only entries.
    interests: list[str] = []
    not_interests: list[str] = []
    #: The canonical form of the two lists above: one ``ResearchInterest``
    #: per entry (a concept, a signed weight in -1..1 or none for unknown,
    #: method, generator, assertedAt, evidence).
    research_interests: list[dict] = []
    same_as: list[str] = []
    methodological_commitments: list[str] = []
    #: Optional clinical extension; see ``docs/rp-spec/index.md``.
    therapeutic_areas: list[dict] = []
    site_capabilities: Optional[dict] = None
    regulatory_experience: list[str] = []
    #: Per-section declared tiers. Read-only here: set through
    #: ``PATCH /visibility``, the one surface that knows about floors and the
    #: full-text lock.
    section_visibility: list[dict] = []
    #: The document's own declared tier. Read-only here, like
    #: :attr:`section_visibility`. It does not decide who may read this
    #: response; the server-side projection already did.
    visibility: str = "public"


class ProfileDetail(_APIModel):
    """The whole-profile display shape: metadata, both narratives, the manifest.

    The shape the static publisher writes and ``rp-ui-lib`` renders. The HTTP
    read is :class:`ProfileRecord`.
    """

    slug: str
    #: See :attr:`ProfileSummary.rid`.
    rid: Optional[str] = None
    metadata: ProfileMetadataPayload
    #: Markdown body of ``personality/expertise.md``, or ``None`` when this
    #: viewer's tier does not reach it. ``None``, never ``""``, so a client can
    #: tell withheld from empty.
    expertise: Optional[str] = None
    #: Markdown body of ``personality/SOUL.md``. See :attr:`expertise`.
    soul: Optional[str] = None
    #: The profile manifest (``hasPart`` + ``subjectOf`` entries), each with an
    #: ``effective_visibility`` key.
    manifest: list[dict] = []
    #: The ``contentUrl``s this viewer did not receive.
    withheld: list[str] = []
    #: ``"sha256:<hex>"`` over the document + SOUL: the edit version token.
    content_hash: Optional[str] = None


class PaperEntry(_APIModel):
    """One paper as the static publisher and ``rp-ui-lib`` list it.

    The HTTP read is :class:`PaperPage`.
    """

    paper_id: Optional[str] = None
    title: str
    year: Optional[int] = None
    journal: Optional[str] = None
    first_author: Optional[str] = None
    full_text_link: Optional[str] = None
    summary_available: bool = False
    doi: Optional[str] = None
    pmid: Optional[str] = None
    openalex_id: Optional[str] = None
    authors: Optional[list[str]] = None


class PaperSummary(_APIModel):
    """One summary, as the static publisher and ``rp-ui-lib`` carry it."""

    paper_id: str
    summary: str


# ---------------------------------------------------------------------------
# Sized, per-caller reads (records, paper pages, text, passages)
# ---------------------------------------------------------------------------

#: Why a part is not available to this caller.
#:
#: - ``not_permitted``: it exists and this caller's tier does not reach it;
#: - ``none``: there is no such artifact;
#: - ``not_uploaded``: it is listed and visible, but its body was never pushed.
SizeReason = Literal["not_permitted", "none", "not_uploaded"]


class Size(_APIModel):
    """What this caller may fetch of one deeper part, and how big it is.

    ``available`` means *this* caller can fetch it through a route that exists,
    not that the store holds it. ``bytes`` is the UTF-8 size of the stored
    body; ``approx_tokens`` is ``bytes // 4``.
    """

    available: bool
    bytes: Optional[int] = None
    approx_tokens: Optional[int] = None
    #: Set only when ``available`` is false.
    reason: Optional[SizeReason] = None


class Trimmed(_APIModel):
    """The first entries of a long list, and how many there are in all."""

    top: list = []
    total: int = 0


class ProfileParts(_APIModel):
    """Sizes of a profile's deeper parts, for this caller."""

    soul: Size
    expertise: Size
    #: The works artifact (``sources/papers.jsonld``); the count is in
    #: ``fields.paper_count``.
    papers: Size
    #: Files this caller may not read, counted by role, e.g.
    #: ``{"paper_fulltext": 53}``.
    files_withheld: dict[str, int] = {}


class ProfileRecord(_APIModel):
    """``GET /profiles/{slug}``: one profile, sized for the caller.

    ``view="record"`` (the default) is the trimmed record: the fields an agent
    or a list view commonly needs, long lists cut to their top entries with a
    total, no JSON-LD plumbing, no file manifest, no narrative bodies. It stays
    under 8 KB. ``view="full"`` carries every metadata field untrimmed, plus
    the ``soul`` and ``expertise`` bodies, for an edit form.
    """

    slug: str
    rid: Optional[str] = None
    #: The edit version token (``"sha256:<hex>"`` over document + SOUL).
    content_hash: Optional[str] = None
    view: Literal["record", "full"]
    fields: dict
    parts: ProfileParts
    #: Field names this caller may not see; each is ``null`` in ``fields``.
    withheld: list[str] = []
    #: ``view="full"`` only: the narrative bodies (``None`` when withheld).
    soul: Optional[str] = None
    expertise: Optional[str] = None


class PaperRow(_APIModel):
    """One row of ``GET /profiles/{slug}/papers``: enough to choose the next read."""

    paper_id: str
    title: str
    year: Optional[int] = None
    journal: Optional[str] = None
    first_author: Optional[str] = None
    doi: Optional[str] = None
    openalex_id: Optional[str] = None
    #: The first ~160 chars of the summary this caller may read, else of the
    #: abstract.
    summary_short: Optional[str] = None
    summary: Size
    text: Size
    #: The paper's 16-hex version token, for ``PATCH``/``DELETE /works``.
    version: str
    #: Only when the request had ``q``: the fused (RRF) score.
    score: Optional[float] = None
    #: Only when the request had ``q``: which rankings kept this paper.
    matched_by: Optional[list[Literal["keyword", "semantic"]]] = None


class PaperPage(_APIModel):
    """``GET /profiles/{slug}/papers``: one page of paper rows."""

    items: list[PaperRow]
    #: Matching rows, before paging.
    total: int
    limit_applied: int
    next_cursor: Optional[str] = None
    has_more: bool = False
    filters_applied: dict = {}
    #: Only when the request had ``q``.
    search_mode_used: Optional[Literal["hybrid", "keyword"]] = None
    #: Set whenever the server fell back or left something out.
    note: Optional[str] = None


class PaperRecordView(_APIModel):
    """``GET /profiles/{slug}/papers/{paper_id}``: one paper, sized for the caller."""

    paper_id: str
    title: str
    version: str
    view: Literal["record", "full"]
    #: ``PaperRecord`` fields by name. The record view trims ``authors`` (first
    #: 10) and ``topics`` (5) and cuts ``abstract`` at 1,500 chars, setting
    #: ``abstract_truncated``.
    fields: dict
    #: The summary text inline: the summary artifact when this caller may read
    #: it, else the record's own ``summary`` field.
    summary: Optional[str] = None
    summary_source: Optional[Literal["generated", "record"]] = None
    #: ``{"summary": Size, "text": Size}``.
    parts: dict[str, Size]
    #: Full-text section names, when this caller may read the text.
    sections: Optional[list[str]] = None
    withheld: list[str] = []


class SummaryBatch(_APIModel):
    """``GET /profiles/{slug}/summaries?ids=``: several summaries in one read."""

    summaries: dict[str, str] = {}
    unavailable: dict[str, SizeReason] = {}
    limit_applied: int
    #: Ids past the cap, not looked at.
    not_processed: list[str] = []


class FileList(_APIModel):
    """``GET /profiles/{slug}/files``: the manifest, labeled for this caller."""

    #: Manifest entries, each with ``effective_visibility`` and ``slot``
    #: (``hasPart`` or ``subjectOf``).
    files: list[dict] = []
    #: The ``contentUrl``s this caller may not read.
    withheld: list[str] = []


class TextSection(_APIModel):
    """One heading's span in a text: what ``section=`` accepts."""

    name: str
    offset: int
    chars: int


class TextPage(_APIModel):
    """One bounded page of a long text (``.../text``)."""

    section: Optional[str] = None
    offset: int
    returned_chars: int
    #: Of the section, or of the whole text when no section was named.
    total_chars: int
    has_more: bool
    #: Pass as ``offset`` (with the same ``section``) to read on.
    next_offset: Optional[int] = None
    sections: list[TextSection] = []
    text: str
    #: Narrative reads only: the profile's edit version token.
    content_hash: Optional[str] = None


class PassageRequest(_APIModel):
    """Body of ``POST .../passages``."""

    query: str
    k: Optional[int] = None


PassageSource = Literal[
    "full_text", "summary", "abstract", "soul", "expertise", "cv", "web", "grant"
]


class Passage(_APIModel):
    """One passage that answers a query, from a source this caller may read."""

    source: PassageSource
    #: Web page id or grant id; a paper's id is implied by the route.
    source_id: Optional[str] = None
    section: Optional[str] = None
    #: Char offset into that source; the text routes accept it for
    #: ``full_text``, ``soul`` and ``expertise``.
    offset: Optional[int] = None
    #: At most ``SNIPPET_CHARS`` characters.
    text: str
    #: The fused (RRF) score.
    score: float
    matched_by: list[Literal["keyword", "semantic"]]


class PassageList(_APIModel):
    """``POST .../passages``: the best passages, and what was searched."""

    passages: list[Passage]
    k_applied: int
    search_mode_used: Literal["hybrid", "keyword", "hybrid+keyword_fulltext"]
    #: Sources actually searched, e.g. ``["full_text", "summary", "abstract"]``.
    searched: list[str] = []
    #: Every fallback or omission, in fixed server text.
    note: Optional[str] = None


class PushResponse(_APIModel):
    """Result of ``PUT /api/v1/profiles/{slug}`` (a pushed profile)."""

    slug: str
    rid: Optional[str] = None
    name: str
    level: str = "full"
    # True when the pushed profile carries a built embedding index
    # (.cache/embeddings.sqlite), i.e. it is immediately matchable.
    indexed: bool = False
    # Server-side files kept rather than deleted, by class name
    # ({"fulltext": 53, "index": 1}).
    kept: dict[str, int] = {}
    # Manifest entries the server added back because the incoming manifest
    # dropped a file the server kept. A nonzero count means the pushed
    # profile.jsonld was not the whole index.
    spliced: int = 0
    # {role: count} over the manifest the server holds after the commit.
    manifest_counts: dict[str, int] = {}
    # The ?mode= the push ran under: what happened to the live files the
    # archive did not carry (replace | merge | prune).
    mode: str = "replace"


class CapabilitiesResponse(_APIModel):
    """Result of ``GET /api/v1/capabilities``: what this server can be asked for.

    A push client reads this before the PUT, so a server that lacks a
    requested ``?mode=`` is refused up front instead of silently replacing.
    """

    #: API version this server serves.
    version: str = "v1"
    #: The ``?mode=`` values ``PUT /profiles/{slug}`` accepts.
    push_modes: list[str] = []
    #: Named behaviours a client can require. ``manifest_splice`` means the
    #: server adds a manifest entry back for every file it keeps, so a partial
    #: incoming manifest cannot delete artifacts the server holds.
    features: list[str] = []


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
    """Owner-editable metadata fields. Only the fields present are applied.

    Unknown keys pass the wire model but are rejected server-side against the
    editable whitelist in :mod:`researcher_profiles.profile.edit`.
    """

    name: Optional[str] = None
    affiliation: Optional[str] = None
    job_title: Optional[str] = None
    field: Optional[str] = None
    subfields: Optional[list[str]] = None
    summary: Optional[str] = None
    expertise: Optional[list[str]] = None
    #: Either list, when present, replaces the profile's text-only interest
    #: entries with declared ones (+0.5 / -0.5, ``generator: user``).
    interests: Optional[list[str]] = None
    not_interests: Optional[list[str]] = None
    #: Typed entries (``ResearchInterest``), validated server-side like
    #: ``training``. Replaces the whole list.
    research_interests: Optional[list[dict]] = None
    #: ``list[dict]`` on the wire and validated in ``profile.edit``, so a
    #: malformed entry is a 400 naming the entry rather than a 422.
    training: Optional[list[dict]] = None
    career: Optional[list[dict]] = None
    same_as: Optional[list[str]] = None
    #: The "how I think" narrative (``personality/SOUL.md``), markdown. Replaces
    #: the whole narrative in the same write as the other fields.
    soul: Optional[str] = None
    #: Optimistic concurrency: the ``content_hash`` this edit was composed
    #: against. Omitted, the patch is last-writer-wins; supplied and stale, the
    #: patch is a 409 carrying the current hash.
    base_hash: Optional[str] = None


class WorkPatch(_APIModel):
    """Owner-editable fields of one work in ``sources/papers.jsonld``.

    Same rules as :class:`MetadataPatch`. Field names are the on-disk names,
    ``datePublished`` included.
    """

    name: Optional[str] = None
    doi: Optional[str] = None
    openalex_id: Optional[str] = None
    #: The publication year. A plain field, not ``year`` with an alias, which
    #: makes FastAPI's schema pass print a pydantic warning.
    datePublished: Optional[int] = None
    type: Optional[str] = None
    citation: Optional[str] = None
    full_text_link: Optional[str] = None
    access: Optional[str] = None
    summary: Optional[str] = None
    first_author: Optional[str] = None
    author_position: Optional[str] = None
    is_corresponding: Optional[bool] = None
    #: Optimistic concurrency for this one work: the paper ``version`` (from
    #: ``GET /papers`` or ``GET /papers/{paper_id}``) this edit was composed
    #: against. Supplied and stale, the patch is a 409 carrying the current
    #: version in ``X-RP-Paper-Version``. A work edit does not move the
    #: profile's ``content_hash``.
    base_version: Optional[str] = None


class ArtifactVisibility(_APIModel):
    """One per-artifact visibility change. Supply exactly one selector
    (``content_url``, ``paper_id``, or ``role``) plus the target ``visibility``.
    """

    content_url: Optional[str] = None
    paper_id: Optional[str] = None
    role: Optional[str] = None
    visibility: str


class SectionTier(_APIModel):
    """One inline section's declared tier.

    Set through the visibility patch, not the metadata patch, because only
    that surface knows about host floors and the full-text lock.
    """

    section: str
    visibility: str


class VisibilityPatch(_APIModel):
    """Set the profile-level default tier, per-artifact tiers, section tiers."""

    profile_visibility: Optional[str] = None
    artifacts: list[ArtifactVisibility] = []
    sections: list[SectionTier] = []
    #: See :attr:`MetadataPatch.base_hash`.
    base_hash: Optional[str] = None


class EditResult(_APIModel):
    """Result of an owner edit: the slug and the fields that changed."""

    slug: str
    rid: Optional[str] = None
    updated: list[str] = []
    #: How many manifest artifacts a visibility patch actually re-tiered.
    artifacts_changed: int = 0
    #: The ``content_hash`` after this write: the next ``base_hash``.
    content_hash: Optional[str] = None
    #: Work edits only: the paper's version after this write (``None`` after a
    #: delete).
    version: Optional[str] = None


class ArtifactTier(_APIModel):
    """One artifact's tier, and why: the read side of the visibility API.

    Computed by ``privacy.explain_tiers``; ``visible_to`` comes from
    ``privacy.tier_allows``.
    """

    content_url: str
    role: Optional[str] = None
    name: Optional[str] = None
    paper_id: Optional[str] = None
    #: What is written on the manifest entry.
    declared: str
    #: What actually governs, after the profile default and the derivation rule.
    effective: str
    #: Concrete causes holding it above ``declared`` ("derived from
    #: sources/cv.md (private)"), for display on this row.
    raised_by: list[str] = []
    #: Subset of ``["anonymous", "lab", "you"]``.
    visible_to: list[str] = []


class SectionTierReport(_APIModel):
    """One inline section's tiers, and who they let in.

    ``declared`` is what the owner set; ``effective`` is what governs after
    the profile default folds in.
    """

    section: str
    #: What ``doc.section_visibility`` says for this section (``"public"`` when
    #: undeclared). For ``soul`` this is the most restrictive of the declared
    #: section tier and the declared tier of the ``soul`` manifest artifact, so
    #: the read-back matches what actually gates ``personality/SOUL.md``.
    declared: str
    #: After folding in the profile default (``privacy.section_tiers``).
    effective: str
    #: Subset of ``["anonymous", "lab", "you"]`` who may read this section.
    visible_to: list[str] = []


class VisibilityReport(_APIModel):
    """``GET /profiles/{slug}/visibility``: what is published, and to whom."""

    slug: str
    rid: Optional[str] = None
    profile_visibility: str
    #: A host-imposed ceiling on this profile ("nobody has claimed it"), or
    #: ``None``. The server overrides any choice above it.
    profile_floor: Optional[str] = None
    profile_floor_reason: Optional[str] = None
    artifacts: list[ArtifactTier] = []
    #: One row per inline section, in ``SECTION_FIELDS`` order.
    sections: list[SectionTierReport] = []
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
    #: True when ``text`` was cut to ``SNIPPET_CHARS`` at a word boundary.
    truncated: bool = False


class SearchResponse(_APIModel):
    hits: list[SearchHitPayload]
    #: The ``k`` that ran, after clamping.
    k_applied: int = 0


# ---------------------------------------------------------------------------
# Cross-profile matching (store.match.rank over HTTP)
# ---------------------------------------------------------------------------


class MatchRequest(_APIModel):
    query: str
    k: int = 5
    prefilter: int = 10
    #: Topic labels, or OpenAlex topic ids (``T10222``) matched against each
    #: profile's typed research interests.
    require_topics: Optional[list[str]] = None
    #: The query side's typed interests (``ResearchInterest`` entries, e.g. a
    #: query profile's ``research_interests``). Each candidate's OpenAlex topics
    #: are scored against them: declared weights boost or push down, a declared
    #: -1 drops the candidate, and an unweighted topic counts through its share.
    interests: list[dict] = []
    #: How much that topic score adds to a candidate's score.
    topic_alpha: float = 0.2
    diversify: bool = True
    lambda_: float = 0.5
    topk_chunks: int = 5
    normalize: bool = True
    include_chunks: bool = False


class MatchEvidencePayload(_APIModel):
    centroid_score: float
    top_papers: list[str] = []
    overlapping_topics: list[str] = []
    #: OpenAlex topic ids shared with the request's ``interests``.
    matched_topics: list[str] = []
    # Populated only when the request sets include_chunks=True.
    top_chunks: list["SearchHitPayload"] = []


class MatchResult(_APIModel):
    #: Directory name / display handle. Useful for links; not a join key.
    slug: str
    name: str
    #: The join key: a canonical ORCID or a ``local:`` id. A consumer seeing
    #: ``None`` should fail loudly, not fall back to name matching, which binds
    #: the wrong person when two researchers share a name.
    rid: Optional[str] = None
    # Derived from `rid`; None when the rid is local.
    orcid: Optional[str] = None
    score: float
    evidence: MatchEvidencePayload


class MatchResponse(_APIModel):
    matches: list[MatchResult]
    #: How many profiles were actually ranked, after privacy-tier filtering.
    #: With ``total_profiles`` it tells "nobody matched" from a broken
    #: embedding path.
    ranked_profiles: int
    #: The size of the indexed corpus this query was ranked against.
    total_profiles: int
    #: ``k`` and ``prefilter`` as they ran, after clamping.
    k_applied: int = 0
    prefilter_applied: int = 0


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
    #: Present on a candidate with a COI. Drop mode omits such candidates.
    coi: Optional[CoiBlock] = None


class ReviewerMatchResponse(_APIModel):
    matches: list[ReviewerMatchResult]
    #: ``k`` and ``prefilter`` as they ran, after clamping.
    k_applied: int = 0
    prefilter_applied: int = 0


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
    #: ``k`` and ``max_pages`` as they ran, after clamping.
    k_applied: int = 0
    max_pages_applied: int = 0


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
    #: The retrieval ``k`` that ran, after clamping.
    k_applied: int = 0


class IdeaPayload(_APIModel):
    hypothesis: str
    approach: str
    rationale: str
    related_works: list[str] = []


class IdeaList(_APIModel):
    items: list[IdeaPayload]
    #: The retrieval ``k`` that ran, after clamping.
    k_applied: int = 0


class RiffPayload(_APIModel):
    angle: str
    text: str
    related_work: Optional[str] = None


class RiffList(_APIModel):
    items: list[RiffPayload]
    #: The retrieval ``k`` that ran, after clamping.
    k_applied: int = 0


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
    "CapabilitiesResponse",
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
    "FileList",
    "Passage",
    "PassageList",
    "PassageRequest",
    "PaperPage",
    "PaperRecordView",
    "PaperRow",
    "ProfileDetail",
    "ProfileMetadataPayload",
    "ProfileParts",
    "ProfileRecord",
    "Size",
    "SizeReason",
    "SummaryBatch",
    "TextPage",
    "TextSection",
    "Trimmed",
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
    "VisibilityPatch",
    "WorkPatch",
]
