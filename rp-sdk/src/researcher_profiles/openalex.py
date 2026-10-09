"""OpenAlex record parsing plus narrow fetch helpers.

The parsing functions are pure: no HTTP, no pagination, no dedupe, and no
``paper_id`` policy (``paper_id`` is the caller's job). Every fetch takes an
:class:`~researcher_profiles.openalex_client.OpenAlexClient` as its first
argument. This module never imports ``httpx``, so importing it stays cheap.
"""

import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .errors import ProfileError
from .openalex_client import OpenAlexHTTPError
from .schema import PaperRecord, ResearchInterest, effective_interests

if TYPE_CHECKING:
    from .openalex_client import OpenAlexClient

logger = logging.getLogger("researcher_profiles.openalex")
_abstract_logger = logging.getLogger("researcher_profiles.openalex.abstract_decode")


# ---------------------------------------------------------------------------
# Pure function: decode_abstract_inverted_index
# ---------------------------------------------------------------------------


def decode_abstract_inverted_index(
    idx: dict[str, list[int]] | None,
) -> str:
    """Reconstruct an abstract string from an OpenAlex ``abstract_inverted_index``.

    The OpenAlex inverted index format maps each word to the list of positions
    (0-based) where that word appears in the abstract:

        {"Despite": [0], "decades": [1], "of": [2, 20, 38], ...}

    A position claimed by two words keeps the first (sorted by word) and logs
    a warning. Gaps are skipped. Whitespace is collapsed.

    Returns:
        The reconstructed abstract string, or ``""`` when ``idx`` is empty or
        malformed. Never raises.
    """
    if not idx:
        return ""

    try:
        pairs: list[tuple[int, str]] = []
        for word, positions in idx.items():
            for pos in positions:
                pairs.append((pos, word))

        if not pairs:
            return ""

        pairs.sort(key=lambda p: (p[0], p[1]))

        max_pos = pairs[-1][0]

        pos_to_word: dict[int, str] = {}
        for pos, word in pairs:
            if pos in pos_to_word:
                _abstract_logger.warning(
                    "Duplicate position %d in abstract_inverted_index (keeping %r, ignoring %r)",
                    pos,
                    pos_to_word[pos],
                    word,
                )
            else:
                pos_to_word[pos] = word

        tokens: list[str] = []
        for i in range(max_pos + 1):
            if i in pos_to_word:
                tokens.append(pos_to_word[i])
            else:
                _abstract_logger.debug(
                    "Gap at position %d in abstract_inverted_index; filling with space",
                    i,
                )
                tokens.append(" ")

        abstract = " ".join(tokens)
        abstract = re.sub(r"\s+", " ", abstract)
        return abstract.strip()

    except (TypeError, ValueError, AttributeError) as exc:
        _abstract_logger.warning("Failed to decode abstract_inverted_index: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Full parse: raw OpenAlex work -> PaperRecord
# ---------------------------------------------------------------------------


def _first_author_last(raw: dict) -> str:
    """The first authorship's last name."""
    authorships = raw.get("authorships") or []
    first_authorship = authorships[0] if authorships else {}
    name = ((first_authorship.get("author") or {}).get("display_name") or "").strip()
    return name.split()[-1] if name else ""


def _journal_name(raw: dict) -> str:
    """``primary_location.source.display_name``, then ``host_venue``, else ``""``."""
    primary_source = (raw.get("primary_location") or {}).get("source") or {}
    return (
        primary_source.get("display_name")
        or (raw.get("host_venue") or {}).get("display_name")
        or ""
    )


def _pdf_url(raw: dict) -> str | None:
    """Preferred full-text link: ``primary_location`` before ``best_oa_location``."""
    primary = raw.get("primary_location") or {}
    best_oa = raw.get("best_oa_location") or {}
    return (
        primary.get("pdf_url") or best_oa.get("pdf_url") or best_oa.get("landing_page_url") or None
    )


def _bare_doi(raw: dict) -> str | None:
    """The lowercased DOI with only the ``https://doi.org/`` resolver form stripped."""
    return (raw.get("doi") or "").replace("https://doi.org/", "").strip().lower() or None


def _extract_pmid(raw: dict) -> str | None:
    """The PMID from the ``ids`` block ("https://pubmed.ncbi.nlm.nih.gov/12345678")."""
    pmid_raw = (raw.get("ids") or {}).get("pmid") or ""
    return pmid_raw.rsplit("/", 1)[-1] if pmid_raw else None


def _extract_pmcid(raw: dict) -> str | None:
    """The ``PMC<digits>`` id from the first PubMed Central location, if any.

    Two location shapes carry it: an OAI id
    (``pmh:oai:pubmedcentral.nih.gov:6772529``) and a PubMed Central landing
    page URL ending in ``/pmc/articles/PMC6772529``.
    """
    for loc in raw.get("locations") or []:
        loc_id = loc.get("id") or ""
        if "pubmedcentral.nih.gov:" in loc_id:
            pmc_num = loc_id.rsplit(":", 1)[-1]
            if pmc_num.isdigit():
                return f"PMC{pmc_num}"
        source = loc.get("source") or {}
        if source.get("display_name") == "PubMed Central":
            landing = loc.get("landing_page_url") or ""
            if "/pmc/articles/" in landing:
                pmc_num = landing.rsplit("/", 1)[-1].replace("PMC", "")
                if pmc_num.isdigit():
                    return f"PMC{pmc_num}"
    return None


def work_topic_ids(raw: dict) -> list[str]:
    """Bare OpenAlex topic ids, primary topic first, each once."""
    out: list[str] = []
    for t in [raw.get("primary_topic") or {}, *(raw.get("topics") or [])]:
        tid = str((t or {}).get("id") or "").rsplit("/", 1)[-1]
        if tid and tid not in out:
            out.append(tid)
    return out


def parse_work(raw: dict) -> PaperRecord | None:
    """Parse a raw OpenAlex work dict into a :class:`PaperRecord`.

    ``paper_id`` is left ``None`` for the caller to fill. ``cited_by_count``,
    ``pmid`` and ``pmcid`` are carried as extra fields.

    Returns:
        A ``PaperRecord`` (``status="pending"``), or ``None`` when the work has
        no title or no publication year (not usable).
    """
    title = (raw.get("title") or "").strip()
    year = raw.get("publication_year")
    if not title or year is None:
        return None

    return PaperRecord(
        title=title,
        year=int(year),
        first_author=_first_author_last(raw),
        journal=_journal_name(raw),
        pdf_url=_pdf_url(raw),
        abstract=decode_abstract_inverted_index(raw.get("abstract_inverted_index")),
        openalex_id=(raw.get("id") or "").rsplit("/", 1)[-1],
        doi=_bare_doi(raw),
        type=(raw.get("type") or "").lower() or None,
        topics=work_topic_ids(raw),
        status="pending",
        pmid=_extract_pmid(raw),
        pmcid=_extract_pmcid(raw),
        cited_by_count=int(raw.get("cited_by_count") or 0),
    )


# ---------------------------------------------------------------------------
# Topic prevalence: inferred interests from a corpus's OpenAlex topics
# ---------------------------------------------------------------------------

#: The generator prefix every topic-count entry carries; the pinned release
#: follows the ``@``. A rebuild replaces exactly the entries under this prefix.
TOPIC_GENERATOR = "openalex-topics"


def _get(paper: Any, key: str) -> Any:
    return paper.get(key) if isinstance(paper, dict) else getattr(paper, key, None)


def topic_prevalence(
    papers: Iterable[Any],
    *,
    top_n: int = 15,
    min_share: float = 0.05,
    names: dict[str, str] | None = None,
    asserted_at: datetime | None = None,
) -> list[ResearchInterest]:
    """The corpus's most common OpenAlex topics as inferred interests.

    Each paper counts once per topic, whether the topic is its primary topic,
    a secondary one, or both (a distinct-paper prevalence, not the
    double-counting sum over topic slots). ``share`` is the fraction of
    the papers that carry topic data at all, so a paper OpenAlex never tagged
    neither helps nor hurts a topic.

    Entries are ``method: inferred``, ``generator: openalex-topics@<release>``,
    with **no weight**: how often a topic appears is not how much the person
    cares about it, so the share goes in ``evidence`` only.

    A topic id the pinned copy does not know is kept, with its display name
    from ``names`` (or the id itself) and no ``version``, and logged.
    """
    from . import vocab
    from .schema import InterestConcept

    tagged = [p for p in papers if _get(p, "topics")]
    if not tagged:
        return []
    by_topic: dict[str, list[str]] = {}
    for i, p in enumerate(tagged):
        work = str(_get(p, "openalex_id") or _get(p, "paper_id") or i).rsplit("/", 1)[-1]
        for tid in dict.fromkeys(_get(p, "topics")):
            by_topic.setdefault(str(tid).rsplit("/", 1)[-1].upper(), []).append(work)
    n = len(tagged)
    ranked = sorted(by_topic.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    at = asserted_at or datetime.now(timezone.utc).replace(microsecond=0)
    generator = f"{TOPIC_GENERATOR}@{vocab.OPENALEX_RELEASE}"
    out: list[ResearchInterest] = []
    for tid, works in ranked[:top_n]:
        share = len(works) / n
        if share < min_share:
            break
        concept = vocab.lookup(vocab.OPENALEX_SYSTEM, tid)
        if concept is None:
            logger.warning(
                "OpenAlex topic %s is not in the pinned topic list (release %s); "
                "keeping it unversioned",
                tid,
                vocab.OPENALEX_RELEASE,
            )
            concept = InterestConcept.model_validate(
                {
                    "@id": vocab.concept_iri(vocab.OPENALEX_SYSTEM, tid),
                    "system": vocab.OPENALEX_SYSTEM,
                    "code": tid,
                    "display": (names or {}).get(tid) or tid,
                }
            )
        out.append(
            ResearchInterest(
                concept=concept,
                method="inferred",
                generator=generator,
                assertedAt=at,
                evidence={"papers": works, "share": round(share, 4)},
            )
        )
    return out


#: How much an inferred topic's share of papers counts, next to a declared
#: weight: it is evidence of what someone works on, not a statement of interest.
INFERRED_TOPIC_FACTOR = 0.5


def topic_score(work_topics: Iterable[str], interests: Iterable[ResearchInterest]) -> float | None:
    """How a work's OpenAlex topics sit against a person's interests.

    For each of the work's topics that the person has an effective entry on:

    - a declared weight adds that weight (a negative one pushes the work down);
    - a declared weight of -1 is a hard exclude: the answer is ``None`` and the
      caller drops the work;
    - an entry with no declared weight adds ``0.5 * evidence.share``, a weaker
      inferred signal; with neither it adds nothing.

    Matching is on the exact topic id only. A weight is never carried across a
    vocabulary mapping or up to a parent subfield, so a hard exclude can only
    come from the person declaring -1 on that very topic.
    """
    from .vocab import OPENALEX_SYSTEM

    by_code = {
        e.concept.code: e
        for e in effective_interests(interests)
        if not e.concept.unmapped and e.concept.system == OPENALEX_SYSTEM
    }
    score = 0.0
    for t in dict.fromkeys(str(t).rsplit("/", 1)[-1].upper() for t in work_topics):
        ri = by_code.get(t)
        if ri is None:
            continue
        if ri.weight is not None and ri.method == "declared":
            if ri.weight <= -1:
                return None
            score += ri.weight
        elif ri.evidence and ri.evidence.share:
            score += INFERRED_TOPIC_FACTOR * ri.evidence.share
    return score


def topic_matches(work_topics: Iterable[str], interests: Iterable[ResearchInterest]) -> list[str]:
    """Display names of a work's topics that raise it under :func:`topic_score`.

    The same exact-id lookup: a topic counts when the person declared a
    positive weight on it, or has an unweighted inferred entry with a share.
    Order follows the work's topics (primary first).
    """
    from .vocab import OPENALEX_SYSTEM

    by_code = {
        e.concept.code: e
        for e in effective_interests(interests)
        if not e.concept.unmapped and e.concept.system == OPENALEX_SYSTEM
    }
    out: list[str] = []
    for t in dict.fromkeys(str(t).rsplit("/", 1)[-1].upper() for t in work_topics):
        ri = by_code.get(t)
        if ri is None:
            continue
        if ri.weight is not None and ri.method == "declared":
            if ri.weight > 0:
                out.append(ri.concept.display or t)
        elif ri.evidence and ri.evidence.share:
            out.append(ri.concept.display or t)
    return out


# ---------------------------------------------------------------------------
# Authorship evidence: the corpus contamination filter's inputs
# ---------------------------------------------------------------------------


def authorship_evidence(raw: dict, orcid: str) -> dict:
    """Extract the identity evidence the corpus authorship filter needs.

    Locates the authorship carrying ``orcid`` (bare or URL form) and returns::

        {
          "orcid_present":     bool,
          "display_name":      str,        # matched author's display name, or ""
          "raw_affiliations":  [str, ...], # matched authorship's raw strings
          "institution_ids":   [str, ...], # matched authorship's institution IRIs
          "institution_names": [str, ...], # matched authorship's institution names
          "coauthor_ids":      [str, ...], # author-entity IRIs of the others
        }

    A warning about what ``orcid_present`` means: OpenAlex denormalizes its
    disambiguated Author entity into every work's authorships, ORCID included.
    When the entity is over-merged, foreign works carry the ORCID too, so on
    a corpus enumerated BY that ORCID the flag is true for every work by
    construction and has no discriminating power. It is evidence for a report,
    never a confirmation signal. Confirmation comes from the person's own
    ORCID registry works list.
    """
    bare = str(orcid).rstrip("/").rsplit("/", 1)[-1]
    matched: dict = {}
    coauthor_ids: list[str] = []
    for a in raw.get("authorships") or []:
        au = a.get("author") or {}
        if (au.get("orcid") or "").endswith(bare) and bare:
            matched = a
        elif au.get("id"):
            coauthor_ids.append(au["id"])
    au = matched.get("author") or {}
    institutions = matched.get("institutions") or []
    return {
        "orcid_present": bool(matched),
        "display_name": au.get("display_name") or "",
        "raw_affiliations": [s for s in (matched.get("raw_affiliation_strings") or []) if s],
        "institution_ids": [i["id"] for i in institutions if i.get("id")],
        "institution_names": [i["display_name"] for i in institutions if i.get("display_name")],
        "coauthor_ids": coauthor_ids,
    }


# ---------------------------------------------------------------------------
# Lite adapters: flat record shapes for enrichment consumers
# ---------------------------------------------------------------------------


def _lite_dict(raw: dict, *, author_key: str) -> dict:
    """The shared six-field lite shape; ``author_key`` names the author list.

    ``doi`` is the bare DOI (no ``https://doi.org/`` prefix, case preserved).
    ``abstract`` is ``None`` (not ``""``) when the de-inversion is empty.
    """
    names = [
        a.get("author", {}).get("display_name")
        for a in (raw.get("authorships") or [])
        if a.get("author", {}).get("display_name")
    ]
    abstract = decode_abstract_inverted_index(raw.get("abstract_inverted_index")) or None
    return {
        "title": raw.get("title") or "",
        "abstract": abstract,
        "year": raw.get("publication_year"),
        "doi": (raw.get("doi") or "").replace("https://doi.org/", "") or None,
        "openalex_id": raw.get("id"),
        author_key: names,
    }


def to_work_dict(raw: dict) -> dict:
    """The ``{title, abstract, year, doi, openalex_id, coauthors}`` lite shape."""
    return _lite_dict(raw, author_key="coauthors")


def to_normalized_dict(raw: dict) -> dict:
    """Same as :func:`to_work_dict` but with ``authors`` in place of ``coauthors``."""
    return _lite_dict(raw, author_key="authors")


# ---------------------------------------------------------------------------
# Candidate fetching: the query client behind work ranking
# ---------------------------------------------------------------------------

#: OpenAlex entity ids for topics/concepts ("T10002", "C2778112365"), bare or
#: as full IRIs. Anything else is treated as a free-text search term.
_TOPIC_ID_RE = re.compile(r"^(?:https://openalex\.org/)?([TC]\d+)$", re.IGNORECASE)

#: Cap on seed work ids per ``cites:`` query. OpenAlex rejects unbounded
#: filter values, and a specialist's most recent works carry the signal.
_MAX_CITES_SEEDS = 100

#: OpenAlex ``type`` values that are not scholarly narrative outputs: bare
#: software/code dumps, catch-all deposits, review reports, grant records, etc.
#: ``dataset`` is kept: OpenAlex types some genuine method papers as datasets.
#: ``book`` is dropped: in practice these are self-published volumes, not
#: papers. A deposit mis-typed as ``article`` still slips through.
DEFAULT_EXCLUDE_TYPES = frozenset(
    {"software", "other", "paratext", "libguides", "grant", "peer-review", "book"}
)


#: How many inferred topics (by share of papers) join the feed query. They have
#: no weight, so they are a weaker, bounded signal next to declared topics.
INFERRED_QUERY_TOPICS = 5

#: With at least this many OpenAlex topic ids to query on, the broad free-text
#: ``subfields`` add noise rather than recall, so they are left out.
SUBFIELDS_DROPPED_AT = 3


def _typed_query_terms(md) -> tuple[list[str], list[str]]:
    """``(topic ids, free-text terms)`` from a profile's typed interests.

    Topic ids: OpenAlex topics with a positive effective weight, then the top
    inferred (unweighted) topics by ``evidence.share``. A topic the person
    weighed at 0 or below is never queried, even if their papers carry it.
    Free text: the labels of other positively weighted concepts (text-only and
    non-OpenAlex coded ones).
    """
    from .schema import effective_interests
    from .vocab import OPENALEX_SYSTEM

    ids: list[str] = []
    text: list[str] = []
    inferred = []
    for e in effective_interests(md.research_interests):
        c = e.concept
        if not c.unmapped and c.system == OPENALEX_SYSTEM:
            if e.weight is not None:
                if e.weight > 0:
                    ids.append(c.code)
            elif e.evidence and e.evidence.share:
                inferred.append(e)
        elif e.weight is not None and e.weight > 0:
            text.append(c.text)
    inferred.sort(key=lambda e: -(e.evidence.share or 0))
    ids += [e.concept.code for e in inferred[:INFERRED_QUERY_TOPICS] if e.concept.code not in ids]
    return ids, text


#: Most free-text terms one query carries. They are OR-joined into a single
#: ``title_and_abstract.search`` clause, which gets slow and vague past this.
MAX_FREE_TEXT_TERMS = 12

#: How many key phrases from paper titles and summaries the last-resort
#: search uses, and in how many papers a phrase must appear to count.
PAPER_PHRASES = 8
PAPER_PHRASE_MIN_PAPERS = 2

#: Words that end a key phrase: generic English plus the boilerplate of
#: scientific titles and summaries. Domain words ("data", "analysis") stay,
#: since they are often half of a real phrase ("single-cell data").
_PHRASE_STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or of for to in on at by as is are was were be been being it
    its this that these those with from into onto over under than then thus
    also such same each other more most much many both few own out off via per
    upon about above after again against among because before below between
    during further here once only some very will would could should may might
    must can not no but all any has have had do does did we our us they their
    them he his she her who whom which what when where why how while toward
    towards across within without using use used uses based new novel study
    studies show shows shown showed provide provides present presents propose
    proposed proposes paper work works approach approaches toward via versus
    vs through its their one two three first second
    """.split()
)

_PHRASE_WORD_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_PHRASE_BREAK_RE = re.compile(r"[^a-z0-9\s-]+")


def _phrase_chunks(text: str) -> list[list[str]]:
    """Runs of content words, split at punctuation, stopwords and bare numbers."""
    chunks: list[list[str]] = []
    for segment in _PHRASE_BREAK_RE.split(text.lower()):
        run: list[str] = []
        for w in _PHRASE_WORD_RE.findall(segment):
            w = w.strip("-")
            if not w or w in _PHRASE_STOPWORDS or w.isdigit() or len(w) < 2:
                if run:
                    chunks.append(run)
                run = []
            else:
                run.append(w)
        if run:
            chunks.append(run)
    return chunks


def paper_key_phrases(
    papers: Iterable[Any],
    *,
    top_n: int = PAPER_PHRASES,
    min_papers: int = PAPER_PHRASE_MIN_PAPERS,
) -> list[str]:
    """The 2- and 3-word phrases most shared across paper titles and summaries.

    Each paper counts once per phrase. A phrase must appear in at least
    ``min_papers`` papers. Ranked by paper count, longer phrases first on a
    tie; a phrase inside one already kept (or containing one) is skipped, so
    "single cell" and "single cell atac" do not both use up a slot.
    """
    counts: dict[str, int] = {}
    for p in papers:
        title = _get(p, "title") or _get(p, "name") or ""
        text = f"{title}. {_get(p, 'summary') or ''}"
        found: set[str] = set()
        for chunk in _phrase_chunks(text):
            for n in (2, 3):
                for i in range(len(chunk) - n + 1):
                    found.add(" ".join(chunk[i : i + n]))
        for ph in found:
            counts[ph] = counts.get(ph, 0) + 1
    ranked = sorted(
        (ph for ph, c in counts.items() if c >= min_papers),
        key=lambda ph: (-counts[ph], -len(ph.split()), ph),
    )
    out: list[str] = []
    for ph in ranked:
        if any(f" {ph} " in f" {k} " or f" {k} " in f" {ph} " for k in out):
            continue
        out.append(ph)
        if len(out) >= top_n:
            break
    return out


def _profile_papers(profile) -> list:
    try:
        return list(profile.papers)
    except (OSError, ProfileError, ValidationError):
        logger.warning(
            "could not read papers for %r; querying on metadata only",
            getattr(profile, "slug", profile),
            exc_info=True,
        )
        return []


def _term_sources(md, papers: list):
    """``(source, terms)`` in fallback order; later ones are built only if reached."""
    subfields = list(getattr(md, "subfields", None) or [])
    if getattr(md, "research_interests", None):
        ids, text = _typed_query_terms(md)
        if ids or text:
            yield "interests", ids + ([] if len(ids) >= SUBFIELDS_DROPPED_AT else subfields) + text
    yield "subfields", subfields + list(getattr(md, "interests", None) or [])
    yield "expertise", list(getattr(md, "expertise", None) or [])
    yield "field", [getattr(md, "field", None) or ""]
    yield "paper_text", paper_key_phrases(papers)


def _dedupe_terms(terms: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        term = (term or "").strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


def profile_query_terms(profile) -> dict:
    """Extract the OpenAlex query seeds a profile offers.

    Returns ``{"topics": [...], "seed_work_ids": [...], "source": str}``.
    ``topics`` mixes OpenAlex topic ids (``T…``, queried as ``topics.id:``
    filters) and free-text labels (queried by title/abstract search),
    order-preserving and deduped. They come from the FIRST of these sources
    that yields anything, named in ``source``:

    - ``interests``: typed ``research_interests``: positively weighted OpenAlex
      topics, the top inferred topics by share of papers, the labels of other
      positively weighted interests, and ``subfields`` unless there are at
      least :data:`SUBFIELDS_DROPPED_AT` topic ids;
    - ``subfields``: ``subfields`` + free-text ``interests``;
    - ``expertise``: the expertise entries;
    - ``field``: the profile's field;
    - ``paper_text``: :func:`paper_key_phrases` over paper titles and summaries;
    - ``none``: nothing at all.

    Free-text terms are capped at :data:`MAX_FREE_TEXT_TERMS`; topic ids are not.
    So the feed works to some degree on any profile with papers, and OpenAlex
    ids are never required.

    ``seed_work_ids`` are the OpenAlex work ids of the profile's own papers
    (the citation neighborhood, usually higher precision for specialists),
    whatever the source.
    """
    papers = _profile_papers(profile)
    source, topics = "none", []
    for name, terms in _term_sources(profile.metadata, papers):
        terms = _dedupe_terms(terms)
        if terms:
            source, topics = name, terms
            break
    ids = [t for t in topics if _TOPIC_ID_RE.match(t)]
    text = [t for t in topics if not _TOPIC_ID_RE.match(t)][:MAX_FREE_TEXT_TERMS]
    keep = set(ids) | set(text)
    topics = [t for t in topics if t in keep]
    seed_work_ids = [
        str(oid).rsplit("/", 1)[-1] for p in papers if (oid := getattr(p, "openalex_id", None))
    ]
    return {"topics": topics, "seed_work_ids": seed_work_ids, "source": source}


def _topic_filters(topics: list[str]) -> list[str]:
    """Filter clauses for a topic list: entity ids by id, labels by search."""
    topic_ids: list[str] = []
    concept_ids: list[str] = []
    terms: list[str] = []
    for t in topics:
        m = _TOPIC_ID_RE.match(t.strip())
        if m:
            eid = m.group(1).upper()
            (topic_ids if eid.startswith("T") else concept_ids).append(eid)
        elif t.strip():
            terms.append(t.strip())
    out = []
    if topic_ids:
        out.append("topics.id:" + "|".join(topic_ids))
    if concept_ids:
        out.append("concepts.id:" + "|".join(concept_ids))
    if terms:
        out.append("title_and_abstract.search:" + "|".join(terms))
    return out


def fetch_work(client: "OpenAlexClient", openalex_id: str) -> PaperRecord | None:
    """Fetch a single OpenAlex work by id and parse it into a ``PaperRecord``.

    Accepts a bare id or a full OpenAlex URL. A singleton lookup costs nothing
    against the daily budget. Returns ``None`` when the work is missing (any
    :class:`OpenAlexHTTPError`, e.g. 404) or has no title/year. A budget (429)
    or key (401) error still raises.
    """
    wid = str(openalex_id).strip().rsplit("/", 1)[-1]
    if not wid:
        return None
    try:
        raw = client.get(f"/works/{wid}")
    except OpenAlexHTTPError:
        return None
    return parse_work(raw)


def fetch_new_works(
    client: "OpenAlexClient",
    *,
    since,
    topics: list[str] | None = None,
    seed_work_ids: list[str] | None = None,
    per_page: int = 100,
    max_pages: int = 5,
    exclude_types: set[str] | frozenset[str] | None = None,
) -> list[PaperRecord]:
    """Query OpenAlex for works published since ``since`` relevant to a profile.

    Two independent query modes, run separately (OpenAlex cannot OR across
    filters) and deduped by OpenAlex id:

    - ``topics``: entity ids (``T…``/``C…``, bare or IRI) become ``topics.id:``
      / ``concepts.id:`` filters; free-text labels become one OR-joined
      ``title_and_abstract.search:`` filter.
    - ``seed_work_ids``: the profile's own works, as a ``cites:`` filter for
      new works citing them (capped at the first 100 seeds).

    ``since`` is a ``datetime.date`` or a ``YYYY-MM-DD`` string. Pagination is
    cursor-based, capped at ``max_pages`` pages per query mode; each page is
    one billed call whatever ``per_page`` is (OpenAlex's max is 100). Raw results run through
    :func:`parse_work`, so callers get ``PaperRecord`` objects; unusable works
    (no title/year) are dropped.

    ``exclude_types`` drops non-paper deposit types (software/dataset dumps,
    grant records, review reports…) from the candidate stream. ``None`` applies
    :data:`DEFAULT_EXCLUDE_TYPES`; pass an empty set to keep every type. Each
    excluded value is negated at the OpenAlex query level (``type:!x``, which
    shrinks the fetch so more real papers fit the page budget) and re-checked
    post-parse against :attr:`PaperRecord.type` as a guarantee.

    Each work carries two extra fields saying why it was found: ``found_by``
    (query kinds ``topic``, ``text``, ``cites``) and, for a citing work,
    ``cites_works`` (which seed ids it references).

    A failure on any page (an ``OpenAlexError``) propagates and discards the
    works already collected.
    """
    if exclude_types is None:
        exclude_types = DEFAULT_EXCLUDE_TYPES
    drop_types = {t.strip().lower() for t in exclude_types if t and t.strip()}
    queries = _new_works_queries(since, topics, seed_work_ids, drop_types)
    seeds = {str(s).rsplit("/", 1)[-1] for s in (seed_work_ids or []) if s}

    by_key: dict[str, PaperRecord] = {}
    for kind, filt in queries:
        for raw in _iter_work_results(client, filt, per_page=per_page, max_pages=max_pages):
            rec = _accept_new_work(raw, drop_types)
            if rec is None:
                continue
            rec = by_key.setdefault(rec.openalex_id or f"title:{rec.title.lower()}", rec)
            _tag_found_by(rec, kind, raw, seeds)
    return list(by_key.values())


def _tag_found_by(rec: PaperRecord, kind: str, raw: dict, seeds: set[str]) -> None:
    """Set ``found_by`` and ``cites_works`` (see :func:`fetch_new_works`)."""
    found = list(getattr(rec, "found_by", None) or [])
    if kind not in found:
        found.append(kind)
    rec.found_by = found
    if kind == "cites":
        refs = {str(r).rsplit("/", 1)[-1] for r in raw.get("referenced_works") or []}
        rec.cites_works = sorted(seeds & refs)


def _new_works_queries(
    since,
    topics: list[str] | None,
    seed_work_ids: list[str] | None,
    drop_types: set[str],
) -> list[tuple[str, str]]:
    """``(kind, filter)`` per query mode: topic ids, free text, then cites."""
    since_str = since.isoformat() if hasattr(since, "isoformat") else str(since)
    base_filter = f"from_publication_date:{since_str}"
    type_clause = "".join(f",type:!{t}" for t in sorted(drop_types))

    queries: list[tuple[str, str]] = []
    for clause in _topic_filters(list(topics or [])):
        kind = "text" if clause.startswith("title_and_abstract.search:") else "topic"
        queries.append((kind, f"{base_filter},{clause}{type_clause}"))
    seeds = [str(s).rsplit("/", 1)[-1] for s in (seed_work_ids or []) if s]
    if seeds:
        cites = "|".join(seeds[:_MAX_CITES_SEEDS])
        queries.append(("cites", f"{base_filter},cites:{cites}{type_clause}"))
    if not queries:
        raise ValueError("fetch_new_works needs topics and/or seed_work_ids")
    return queries


def _iter_work_results(
    client: "OpenAlexClient",
    filt: str,
    *,
    per_page: int,
    max_pages: int,
):
    """Yield each raw work of a cursor-paged query, capped at ``max_pages``."""
    cursor = "*"
    for _ in range(max_pages):
        params = {"filter": filt, "per-page": per_page, "cursor": cursor}
        page = client.get("/works", params)
        yield from page.get("results") or []
        cursor = (page.get("meta") or {}).get("next_cursor")
        if not cursor:
            break


# ---------------------------------------------------------------------------
# Matching a profile's papers to OpenAlex works
# ---------------------------------------------------------------------------

#: How close two normalized titles must be (``difflib`` ratio) to count as the
#: same work when they are not identical; the years must also agree.
TITLE_MATCH_RATIO = 0.92

_TITLE_STRIP_RE = re.compile(r"[^a-z0-9 ]+")


def normalize_title(title: str | None) -> str:
    """Lowercased, punctuation stripped, whitespace collapsed."""
    return " ".join(_TITLE_STRIP_RE.sub(" ", (title or "").lower()).split())


def title_match(paper: Any, work: Any) -> bool:
    """Whether ``work`` is ``paper``: exact normalized title, else a close one
    (:data:`TITLE_MATCH_RATIO`) with the same year."""
    from difflib import SequenceMatcher

    a = normalize_title(_get(paper, "title") or _get(paper, "name"))
    b = normalize_title(_get(work, "title") or _get(work, "name"))
    if not a or not b:
        return False
    if a == b:
        return True
    ya, yb = _get(paper, "year"), _get(work, "year")
    if ya is None or yb is None or int(ya) != int(yb):
        return False
    return SequenceMatcher(None, a, b).ratio() >= TITLE_MATCH_RATIO


def fetch_author_works(
    client: "OpenAlexClient",
    orcid: str,
    *,
    max_pages: int = 10,
) -> list[PaperRecord]:
    """Every work OpenAlex attributes to an ORCID (``author.orcid:`` filter)."""
    bare = str(orcid).strip().rsplit("/", 1)[-1]
    out: list[PaperRecord] = []
    for raw in _iter_work_results(
        client, f"author.orcid:{bare}", per_page=100, max_pages=max_pages
    ):
        rec = parse_work(raw)
        if rec is not None:
            out.append(rec)
    return out


def search_work_by_title(client: "OpenAlexClient", title: str) -> PaperRecord | None:
    """OpenAlex's top ``title.search`` hit for ``title``, or ``None``."""
    # Commas and pipes are filter syntax; inside a title they only confuse it.
    clean = re.sub(r"[,|:]+", " ", title or "").strip()
    if not clean:
        return None
    params: dict = {"filter": f"title.search:{clean}", "per-page": 1}
    results = client.get("/works", params).get("results") or []
    return parse_work(results[0]) if results else None


def match_papers(
    papers: Iterable[Any], works: Iterable[PaperRecord]
) -> list[tuple[Any, PaperRecord]]:
    """Pair each paper with the first work that :func:`title_match` accepts.

    Exact title matches are taken before close ones, and each work is used
    once, so two similar titles cannot both claim the same work.
    """
    works = list(works)
    by_title: dict[str, list[int]] = {}
    for i, w in enumerate(works):
        by_title.setdefault(normalize_title(w.title), []).append(i)
    used: set[int] = set()
    pairs: list[tuple[Any, PaperRecord]] = []
    pending = []
    for p in papers:
        exact = [
            i
            for i in by_title.get(normalize_title(_get(p, "title") or _get(p, "name")), [])
            if i not in used
        ]
        if exact:
            used.add(exact[0])
            pairs.append((p, works[exact[0]]))
        else:
            pending.append(p)
    for p in pending:
        for i, w in enumerate(works):
            if i not in used and title_match(p, w):
                used.add(i)
                pairs.append((p, w))
                break
    return pairs


def _accept_new_work(raw: dict, drop_types: set[str]) -> PaperRecord | None:
    """Parse one raw work; None when unusable or an excluded type."""
    rec = parse_work(raw)
    if rec is None or rec.type in drop_types:
        return None
    return rec


__all__ = [
    "DEFAULT_EXCLUDE_TYPES",
    "authorship_evidence",
    "decode_abstract_inverted_index",
    "fetch_author_works",
    "fetch_new_works",
    "fetch_work",
    "match_papers",
    "normalize_title",
    "search_work_by_title",
    "title_match",
    "paper_key_phrases",
    "parse_work",
    "profile_query_terms",
    "topic_matches",
    "topic_prevalence",
    "topic_score",
    "to_work_dict",
    "to_normalized_dict",
]
