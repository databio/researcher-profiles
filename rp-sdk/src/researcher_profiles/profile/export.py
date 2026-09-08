"""Render one researcher profile into what a knowledge base ingests.

A knowledge base wants two things from a profile: a single block of prose it can
chunk and embed, and the handful of fields that travel with it (identity, links,
the corpus DOI list, and a content hash to use as an idempotency key). This
module produces both as a :class:`ProfileExportBundle`, so a downstream connector
never opens ``profile.jsonld`` or the other source files itself. Vendor-specific
details (a KB's collection ids, chunk size, field names) belong in the connector,
not here; a connector that needs a new fact gets a new field on the bundle.

Core-only: this imports stdlib + pydantic + sibling core modules, nothing else,
so a bare install with no extras can render an export. That is why paper
selection here is a dependency-free scoring pass rather than an embedding one; a
caller that has vectors passes its own paper list in. Nothing is written -- the
export is rendered on demand from an existing profile directory.
"""

import hashlib
import math
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..errors import ProfileError
from ..privacy import chunk_source_tiers, drop_above_public
from ..schema import CareerEntry, PaperRecord, ProfileDocument, Training, normalize_doi, orcid_of
from ..schema.jsonld import PROFILE_FORMAT_IRI, canonical_dumps
from ..utils.clock import now_iso
from . import ResearcherProfile

#: Bump on any change to the rendered text layout or the bundle shape, so a KB
#: can detect the change and re-ingest. This is a detector, not a switch: there
#: is exactly one rendering and it is the current one. Never add a ``version=``
#: parameter that reproduces an older blob.
EXPORT_VERSION: int = 1

#: The ``[abstract-only]`` marker a summary carries when it was written from an
#: abstract rather than full text. Same marker ``embeddings/chunking.py``
#: strips before embedding; it is bookkeeping, not prose.
_ABSTRACT_ONLY_RE = re.compile(r"\[abstract-only\]\s*", re.IGNORECASE)

#: A leading YAML frontmatter fence in a ``*.summary.md`` file. The frontmatter
#: shape is :class:`schema.SummaryFile`; the body is what a KB wants.
_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n", re.DOTALL)

#: Word-ish tokens for the diversity pass: three or more chars, letter-initial.
_TOKEN_RE = re.compile(r"[a-z][a-z0-9-]{2,}")

#: Three or more consecutive newlines, collapsed to a paragraph break.
_BLANK_RUN_RE = re.compile(r"\n{3,}")

#: Generic English stopwords plus the scientific-abstract boilerplate that
#: appears in every corpus. Kept small: the corpus-specific pass in
#: :func:`select_export_papers` removes the researcher's own boilerplate, which
#: is what a hand-maintained list can never keep up with.
_STOPWORDS: frozenset[str] = frozenset(
    """
    the and for that with this from are was were has have had not but all any can
    been being does did his her its our their they them these those there where
    when which while who whom why how than then thus into onto over under also
    such same each other more most much many both few own out off via per upon
    about above after again against among because before below between during
    further here once only some very will would could should may might must
    new novel study studies result results method methods approach approaches
    using used use uses show shows shown showed here we our data based provide
    provides present presents propose proposed proposes work works paper
    analysis analyses model models framework frameworks large small high low
    """.split()
)

#: A token appearing in more than this share of the candidate papers is treated
#: as corpus boilerplate and ignored by the similarity measure.
_CORPUS_STOPWORD_SHARE = 0.6

#: One paper eligible for export: ``(paper, rendered body, body source)``.
_Candidate = tuple[PaperRecord, str, str]


class ExportError(ProfileError):
    """Base error for the knowledge-base export surface."""


class ExportVisibilityError(ExportError):
    """The profile is not ``public`` and the caller did not opt in.

    Pass ``allow_nonpublic=True`` (CLI: ``--allow-nonpublic``) when the
    destination is authorized to hold the profile's tier.
    """


class ExportOptions(BaseModel):
    """Every knob the export surface has.

    Options live on this model rather than in positional parameters so a
    downstream connector never breaks on a signature change: a new knob is a
    new field with a default, and existing callers keep working untouched.
    """

    model_config = ConfigDict(extra="forbid")

    max_papers: int = Field(default=40, description="Cap on papers whose text enters the blob.")
    char_budget: int | None = Field(
        default=120_000,
        description=(
            "Soft cap on total blob characters. Whole paper blocks are dropped "
            "from the end; a body is never truncated mid-text. None disables it."
        ),
    )
    include_soul: bool = Field(default=True, description="Include personality/SOUL.md.")
    include_expertise_doc: bool = Field(
        default=True, description="Include personality/expertise.md."
    )
    prefer_summaries: bool = Field(
        default=True,
        description=(
            "Use a paper's sources/summaries/<id>.summary.md body when present, "
            "falling back to its abstract."
        ),
    )
    diversity: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Overlap-penalty weight in the greedy paper selector (0..1).",
    )
    allow_nonpublic: bool = Field(
        default=False,
        description="Permit export of a profile whose document visibility is above public.",
    )
    explore_base: str | None = Field(
        default=None,
        description=(
            "Base URL of a profile browser app. When unset, the bundle carries no explore_url."
        ),
    )
    profile_url: str | None = Field(
        default=None,
        description="Override for the published profile URL (default: metadata.url).",
    )


class ExportPaperRef(BaseModel):
    """One paper whose prose is inside the rendered text."""

    model_config = ConfigDict(extra="forbid")

    paper_id: str | None
    #: Normalized and bare (``10.xxxx/yyy``), never a resolver URL.
    doi: str | None
    title: str
    year: int | None
    body_source: Literal["summary", "abstract"]


class ProfileExportBundle(BaseModel):
    """One profile, rendered for a knowledge base: the text plus what travels with it.

    ``dois`` and ``papers`` are different things and must not be conflated.
    ``dois`` is the profile's full corpus DOI list: every paper in
    ``sources/papers.jsonld``, normalized and de-duplicated, in document order.
    A KB uses it to link this profile to works it already holds, whether or not
    their prose is in this export. ``papers`` is the much smaller subset whose
    prose is actually inside :attr:`text`.

    ``content_hash`` is the idempotency key. It covers every field except
    itself and :attr:`built_at` (including :attr:`summary`, which is real
    content, so an edited summary triggers a refresh), hence two builds seconds
    apart hash identically and a KB upserts only when the hash changes.
    """

    model_config = ConfigDict(extra="forbid")

    # --- format / provenance of the export itself
    export_version: int
    sdk_version: str
    conforms_to: str

    # --- identity
    rid: str
    name: str
    slug: str
    orcid: str | None
    affiliation: str | None
    field: str | None
    #: The profile's one-paragraph self-description (``metadata.summary``), the
    #: same value rendered into the blob's ``## Overview``. Carried as its own
    #: field so a connector can use it as a short document description without
    #: re-opening ``profile.jsonld``. This package owns profile reading.
    summary: str | None
    level: str
    provenance: str
    license: str | None
    visibility: str

    # --- links
    profile_url: str | None
    explore_url: str | None

    # --- corpus
    dois: list[str]
    paper_count: int
    papers: list[ExportPaperRef]

    # --- payload
    text: str
    text_chars: int

    # --- idempotency
    content_hash: str
    built_at: str


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _normalize_text(text: str) -> str:
    """CRLF -> LF, collapse 3+ blank lines to 2, strip the outer whitespace."""
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    out = _BLANK_RUN_RE.sub("\n\n", out)
    return out.strip()


def explore_url(profile_url: str | None, *, explore_base: str | None = None) -> str | None:
    """The browser-app backlink for a published profile directory.

    Returns ``None`` unless both a profile URL and an explore base are given.
    The hash shape matches what the browser app parses back to the same
    directory: a trailing slash, ``profile.jsonld`` stripped, percent-encoded.
    """
    if not profile_url or not explore_base:
        return None
    url = str(profile_url).strip()
    if not url:
        return None
    if url.endswith("/profile.jsonld"):
        url = url[: -len("profile.jsonld")]
    elif url.endswith("/profile.jsonld/"):
        url = url[: -len("profile.jsonld/")]
    if not url.endswith("/"):
        url += "/"
    quoted = urllib.parse.quote(url, safe="")
    return f"{explore_base.rstrip('/')}/#/p?u={quoted}"


def export_paper_body(
    profile: ResearcherProfile,
    paper: PaperRecord,
    options: ExportOptions | None = None,
) -> tuple[str, str]:
    """Return ``(text, source)`` for one paper's prose.

    ``source`` is ``"summary"`` or ``"abstract"``. The summary body is the
    markdown after any YAML frontmatter fence (whose shape is
    :class:`schema.SummaryFile`), with the ``[abstract-only]`` bookkeeping
    marker removed and whitespace normalized. Returns ``("", "abstract")`` when
    the paper has no usable body at all.
    """
    opts = options or ExportOptions()
    pid = paper.paper_id
    if opts.prefer_summaries and pid:
        raw = profile.summaries.get(pid)
        if raw:
            body = _FRONTMATTER_RE.sub("", raw)
            body = _ABSTRACT_ONLY_RE.sub("", body)
            body = _normalize_text(body)
            if body:
                return body, "summary"
    abstract = _normalize_text(paper.abstract or "")
    return abstract, "abstract"


# ---------------------------------------------------------------------------
# Paper selection
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _overlap(a: set[str], b: set[str]) -> float:
    """Overlap coefficient: insensitive to the length variance between abstracts."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _public_body_keys(
    profile: ResearcherProfile,
    paper_ids: Sequence[str],
) -> set[tuple[str, str]]:
    """The ``(source_type, source_id)`` chunk keys whose tier is ``public``.

    ``privacy.py`` is the single authority; this never re-implements the rule.
    """
    keys = [(kind, pid) for pid in paper_ids for kind in ("paper_summary", "paper_abstract")]
    tiers = chunk_source_tiers(profile.metadata, keys)
    return set(drop_above_public(tiers.items()))


def _export_candidates(profile: ResearcherProfile, opts: ExportOptions) -> list[_Candidate]:
    """The papers eligible for export: titled, uncontaminated, bodied, public."""
    try:
        build_state = profile.build_state
    except (OSError, ValidationError):  # pragma: no cover - a malformed sidecar is not fatal
        build_state = None

    candidates: list[_Candidate] = []
    for p in profile.papers:
        if not (p.title and p.title.strip()):
            continue
        if build_state is not None and p.paper_id and build_state.is_contaminated(p.paper_id):
            continue
        body, source = export_paper_body(profile, p, opts)
        if not body:
            continue
        candidates.append((p, body, source))

    if opts.allow_nonpublic:
        return candidates

    # The f"paper_{source}" key built here must match the ("paper_summary",
    # "paper_abstract") literals _public_body_keys enumerates; the two strings
    # are one convention split across two functions.
    allowed = _public_body_keys(profile, [p.paper_id for p, _, _ in candidates if p.paper_id])
    return [
        (p, body, source)
        for p, body, source in candidates
        if not p.paper_id or (f"paper_{source}", p.paper_id) in allowed
    ]


def _candidate_scores(candidates: list[_Candidate]) -> list[float]:
    """Authorship + impact + recency per candidate, min-max normalized to 0..1."""
    years = [p.year for p, _, _ in candidates if p.year is not None]
    y_min, y_max = (min(years), max(years)) if years else (0, 0)
    y_span = (y_max - y_min) or 1

    raw_scores: list[float] = []
    for p, _, _ in candidates:
        authorship = (
            2.0 if (p.author_position in ("first", "last") or p.is_corresponding is True) else 1.0
        )
        impact = math.log1p(p.cited_by_count or 0)
        recency = ((p.year - y_min) / y_span) if p.year is not None else 0.0
        raw_scores.append(1.5 * authorship + 1.0 * impact + 1.0 * recency)

    s_min, s_max = min(raw_scores), max(raw_scores)
    s_span = (s_max - s_min) or 1.0
    return [(s - s_min) / s_span for s in raw_scores]


def _distinctive_token_sets(candidates: list[_Candidate]) -> list[set[str]]:
    """Per-candidate token sets with the corpus's own boilerplate removed.

    A token in most of the papers carries no discriminating signal; it is the
    researcher's own boilerplate vocabulary. Removing it is what makes the
    overlap coefficient measure topic difference rather than dialect.
    """
    token_sets = [_tokens(f"{p.title} {body}") for p, body, _ in candidates]
    n = len(candidates)
    doc_freq: dict[str, int] = {}
    for ts in token_sets:
        for t in ts:
            doc_freq[t] = doc_freq.get(t, 0) + 1
    common = {t for t, c in doc_freq.items() if c > _CORPUS_STOPWORD_SHARE * n}
    return [ts - common for ts in token_sets]


def _greedy_select(
    scores: list[float],
    token_sets: list[set[str]],
    tiebreak: list[tuple[str, str]],
    *,
    max_papers: int,
    diversity: float,
) -> list[int]:
    """Greedy score-minus-overlap selection, returning indices in pick order.

    ``tiebreak[i]`` is the ``(paper_id, title)`` pair that breaks an exact tie,
    so the result is stable without this function knowing what a paper is.
    """
    remaining = set(range(len(scores)))
    selected: list[int] = []
    while remaining and len(selected) < max(0, max_papers):
        best: tuple[float, float, str, str] | None = None
        best_i = -1
        for i in sorted(remaining):
            penalty = max((_overlap(token_sets[i], token_sets[j]) for j in selected), default=0.0)
            value = scores[i] - diversity * penalty
            key = (-value, -scores[i], tiebreak[i][0], tiebreak[i][1])
            if best is None or key < best:
                best, best_i = key, i
        selected.append(best_i)
        remaining.discard(best_i)
    return selected


def select_export_papers(
    profile: ResearcherProfile,
    options: ExportOptions | None = None,
) -> list[PaperRecord]:
    """The topic-representative subset of a profile's papers, deterministically.

    Filter (untitled, contaminated, bodyless, non-public), score
    (authorship + impact + recency), then diversify greedily so the selection
    spans the researcher's topics instead of stacking their most-cited cluster.
    No embeddings are involved. A caller that has vectors should select its
    own list and pass it to :func:`render_export_text` instead.

    Returned in presentation order (year desc, then title), which is not
    selection order.
    """
    opts = options or ExportOptions()
    candidates = _export_candidates(profile, opts)
    if not candidates:
        return []

    selected = _greedy_select(
        _candidate_scores(candidates),
        _distinctive_token_sets(candidates),
        [(p.paper_id or "", p.title) for p, _, _ in candidates],
        max_papers=opts.max_papers,
        diversity=opts.diversity,
    )

    chosen = [candidates[i][0] for i in selected]
    # Presentation order: newest first, undated last, ties broken by title.
    return sorted(
        chosen,
        key=lambda p: (0 if p.year is not None else 1, -(p.year or 0), p.title),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _bullets(heading: str, items: Sequence[str], *, trailer: str = "") -> list[str]:
    """A markdown section, or nothing at all. Never an empty heading."""
    lines = [f"- {i.strip()}" for i in items if i and str(i).strip()]
    if not lines and not trailer:
        return []
    block = [f"## {heading}", ""]
    block.extend(lines)
    if trailer:
        if lines:
            block.append("")
        block.append(trailer)
    block.append("")
    return block


def _paper_block(profile: ResearcherProfile, paper: PaperRecord, opts: ExportOptions) -> str:
    body, _ = export_paper_body(profile, paper, opts)
    meta = [str(paper.year) if paper.year is not None else "", paper.journal or ""]
    doi = normalize_doi(paper.doi)
    if doi:
        meta.append(f"doi:{doi}")
    meta_line = " · ".join(m for m in meta if m)
    lines = [f"### {paper.title.strip()}", ""]
    if meta_line:
        lines.extend([meta_line, ""])
    if body:
        lines.extend([body, ""])
    return "\n".join(lines)


def _identity_lines(meta: ProfileDocument, opts: ExportOptions) -> list[str]:
    """The title block: name, role, ORCID, profile URL, then a blank line."""
    out: list[str] = [f"# {meta.name}", ""]
    role_line = ", ".join(x for x in (meta.job_title, meta.affiliation) if x and x.strip())
    if role_line:
        out.append(role_line)
    orcid = orcid_of(meta.rid)
    if orcid:
        out.append(f"ORCID: {orcid}")
    profile_url = opts.profile_url or meta.url
    if profile_url:
        out.append(f"Profile: {profile_url}")
    out.append("")
    return out


def _overview_and_field_lines(meta: ProfileDocument) -> list[str]:
    """The ``## Overview`` and ``## Field`` sections, either of which may be absent."""
    out: list[str] = []
    if meta.summary and meta.summary.strip():
        out.extend(["## Overview", "", meta.summary.strip(), ""])

    field_lines = []
    if meta.field and meta.field.strip():
        field_lines.append(meta.field.strip())
    subfields = [s for s in meta.subfields if s and s.strip()]
    if subfields:
        field_lines.append("Subfields: " + ", ".join(s.strip() for s in subfields))
    if field_lines:
        out.extend(["## Field", ""])
        out.extend(field_lines)
        out.append("")
    return out


def _profile_bullet_sections(meta: ProfileDocument) -> list[str]:
    """Expertise, interests (with the not-interested trailer), and commitments."""
    out = list(_bullets("Expertise", meta.expertise))
    not_interests = [i for i in meta.not_interests if i and i.strip()]
    out.extend(
        _bullets(
            "Interests",
            meta.interests,
            trailer=(
                "Not interested in: " + ", ".join(i.strip() for i in not_interests)
                if not_interests
                else ""
            ),
        )
    )
    out.extend(_bullets("Methodological commitments", meta.methodological_commitments))
    return out


def _career_lines(career: Sequence[CareerEntry]) -> list[str]:
    """One ``role, institution (span)`` string per career entry, unbulleted."""
    out: list[str] = []
    for c in career:
        span = ""
        if c.start_year is not None or c.end_year is not None:
            start = str(c.start_year) if c.start_year is not None else ""
            end = str(c.end_year) if c.end_year is not None else "present"
            span = f" ({start}-{end})"
        out.append(f"{c.role}, {c.institution}{span}")
    return out


def _training_lines(training: Sequence[Training]) -> list[str]:
    """One ``degree, field, institution (year)`` string per training entry, unbulleted."""
    out: list[str] = []
    for t in training:
        head = t.degree
        if t.field and t.field.strip():
            head = f"{head}, {t.field.strip()}"
        year = f" ({t.year_end})" if t.year_end is not None else ""
        out.append(f"{head}, {t.institution}{year}")
    return out


def _persona_doc_sections(
    profile: ResearcherProfile,
    opts: ExportOptions,
    doc_tiers: Mapping[tuple[str, str], str],
) -> list[str]:
    """The ``expertise.md`` and ``SOUL.md`` bodies, each gated on opt-in and tier."""
    out: list[str] = []
    if (
        opts.include_expertise_doc
        and doc_tiers.get(("expertise", "expertise")) == "public"
        and profile.expertise.strip()
    ):
        out.extend(["## Research narrative", "", _normalize_text(profile.expertise), ""])

    if opts.include_soul and doc_tiers.get(("soul", "soul")) == "public" and profile.soul.strip():
        out.extend(["## Approach and voice", "", _normalize_text(profile.soul), ""])
    return out


def _fit_to_budget(head: str, blocks: list[str], char_budget: int | None) -> str:
    """Join ``head`` and as many paper ``blocks`` as ``char_budget`` allows.

    Whole blocks come off the end of the presentation-ordered list; a body is
    never split, because half an abstract embeds as a claim its author did not
    make. ``char_budget`` is therefore not a hard cap: a ``head`` longer than
    the budget is returned over budget.
    """

    def assemble(n_blocks: int) -> str:
        parts = [head]
        if n_blocks:
            parts.append("## Selected work\n")
            parts.extend(blocks[:n_blocks])
        return _normalize_text("\n\n".join(p for p in parts if p.strip())) + "\n"

    n = len(blocks)
    text = assemble(n)
    while char_budget is not None and len(text) > char_budget and n > 0:
        n -= 1
        text = assemble(n)
    return text


def render_export_text(
    profile: ResearcherProfile,
    options: ExportOptions | None = None,
    *,
    papers: Sequence[PaperRecord] | None = None,
) -> str:
    """Render one profile as a single deterministic markdown blob.

    The blob is the whole payload a knowledge base ingests: a synthesized
    narrative (identity, expertise, interests, career, then the ``expertise.md``
    and ``SOUL.md`` bodies verbatim) followed by the prose of a selected,
    topic-representative set of papers. How it is chunked and embedded is
    entirely the consumer's business.

    Deterministic by contract. There is no timestamp, counter, hash, or
    host-dependent value anywhere in the output: two renders of an unchanged
    directory are byte-identical. :attr:`ProfileExportBundle.content_hash`
    depends on that.

    Pass ``papers`` to skip selection entirely. That is the override for a
    caller that has embeddings and can choose better than this module can.

    Raises :class:`ExportVisibilityError` when the profile document's
    ``visibility`` is above ``public`` and ``options.allow_nonpublic`` is not set.
    """
    opts = options or ExportOptions()
    meta = profile.metadata

    if meta.visibility != "public" and not opts.allow_nonpublic:
        raise ExportVisibilityError(
            f"profile {profile.slug!r} has visibility {meta.visibility!r}; "
            "exporting it requires allow_nonpublic=True (CLI: --allow-nonpublic)"
        )

    selected = list(papers) if papers is not None else select_export_papers(profile, opts)

    doc_tiers = chunk_source_tiers(
        None if opts.allow_nonpublic else meta,
        [("soul", "soul"), ("expertise", "expertise")],
    )

    out: list[str] = []
    out.extend(_identity_lines(meta, opts))
    out.extend(_overview_and_field_lines(meta))
    out.extend(_profile_bullet_sections(meta))
    out.extend(_bullets("Career", _career_lines(meta.career)))
    out.extend(_bullets("Training", _training_lines(meta.training)))
    out.extend(_persona_doc_sections(profile, opts, doc_tiers))

    head = _normalize_text("\n".join(out))
    blocks = [_paper_block(profile, p, opts) for p in selected]
    blocks = [b for b in blocks if b.strip()]
    return _fit_to_budget(head, blocks, opts.char_budget)


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------


def export_content_hash(payload: Mapping[str, Any]) -> str:
    """``"sha256:<64 hex>"`` over ``payload`` in canonical JSON form.

    ``jsonld.canonical_dumps`` fixes key order and formatting, so the digest is
    reproducible across machines and Python versions rather than depending on
    dict insertion order.
    """
    digest = hashlib.sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_export_bundle(
    profile: ResearcherProfile,
    options: ExportOptions | None = None,
    *,
    papers: Sequence[PaperRecord] | None = None,
    now: str | None = None,
) -> ProfileExportBundle:
    """Render a profile and wrap it with the metadata a knowledge base needs.

    The returned bundle is the whole handoff: a connector maps its fields onto
    the destination's document schema, upserts keyed on :attr:`rid`, and uses
    :attr:`content_hash` as the change detector. Re-running a backfill is then
    a no-op, which is what makes a first load and a later refresh the same
    operation.
    """
    opts = options or ExportOptions()
    # Renders first, so a visibility refusal happens before any work.
    selected = list(papers) if papers is not None else select_export_papers(profile, opts)
    text = render_export_text(profile, opts, papers=selected)

    meta = profile.metadata
    profile_url = opts.profile_url or meta.url

    dois: list[str] = []
    seen: set[str] = set()
    for p in profile.papers:
        doi = normalize_doi(p.doi)
        if doi:
            # DOIs are case-insensitive per the DOI spec; store lowercase for
            # deduplication and consistent comparison across systems.
            doi_lower = doi.lower()
            if doi_lower not in seen:
                seen.add(doi_lower)
                dois.append(doi_lower)

    refs = [
        ExportPaperRef(
            paper_id=p.paper_id,
            doi=normalize_doi(p.doi),
            title=p.title,
            year=p.year,
            body_source=export_paper_body(profile, p, opts)[1],  # type: ignore[arg-type]
        )
        for p in selected
    ]

    from .. import __version__  # the top-level package carries the version

    bundle = ProfileExportBundle(
        export_version=EXPORT_VERSION,
        sdk_version=__version__,
        conforms_to=PROFILE_FORMAT_IRI,
        rid=meta.rid,
        name=meta.name,
        slug=profile.slug,
        orcid=orcid_of(meta.rid),
        affiliation=meta.affiliation,
        field=meta.field,
        summary=meta.summary,
        level=profile.level,
        provenance=profile.provenance,
        license=profile.license,
        visibility=str(meta.visibility),
        profile_url=profile_url,
        explore_url=explore_url(profile_url, explore_base=opts.explore_base),
        dois=dois,
        paper_count=len(profile.papers),
        papers=refs,
        text=text,
        text_chars=len(text),
        content_hash="",
        built_at=now_iso(now),
    )
    payload = bundle.model_dump(mode="json")
    payload.pop("built_at", None)
    payload.pop("content_hash", None)
    bundle.content_hash = export_content_hash(payload)
    return bundle


__all__ = [
    "EXPORT_VERSION",
    "ExportError",
    "ExportOptions",
    "ExportPaperRef",
    "ExportVisibilityError",
    "ProfileExportBundle",
    "build_export_bundle",
    "explore_url",
    "export_content_hash",
    "export_paper_body",
    "render_export_text",
    "select_export_papers",
]
