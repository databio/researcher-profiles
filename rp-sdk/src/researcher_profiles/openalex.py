"""OpenAlex record parsing plus a narrow candidate-fetch client.

The parsing half is PURE: no HTTP, no pagination, no dedupe, and no
``paper_id`` policy. It imports only the standard library and ``PaperRecord``
from ``.schema``, so any tool that needs OpenAlex records in the profile shape
can share these functions instead of reimplementing abstract de-inversion and
raw-work-record extraction.

Corpus-building fetch clients and the ``paper_id`` computation stay with their
callers. What lives here beyond parsing is one narrow query client,
:func:`fetch_new_works` ("new works since <date> relevant to a profile"), the
candidate source for ranking works against a profile. It imports ``httpx``
lazily (the ``client`` extra), so importing this module stays core-cheap.

Public API:
    decode_abstract_inverted_index(idx) -> str: the abstract de-inversion
    parse_work(raw) -> PaperRecord | None: raw work -> PaperRecord
    to_work_dict(raw) -> dict: {..., coauthors} lite shape
    to_normalized_dict(raw) -> dict: {..., authors} lite shape
    profile_query_terms(profile) -> dict: {topics, seed_work_ids}
    fetch_new_works(...) -> list[PaperRecord]: query OpenAlex for candidates
    fetch_work(id) -> PaperRecord | None: point lookup of one work
"""

import logging
import re

from pydantic import ValidationError

from .errors import ProfileError
from .schema import PaperRecord

logger = logging.getLogger("researcher_profiles.openalex")
_abstract_logger = logging.getLogger("researcher_profiles.openalex.abstract_decode")


# ---------------------------------------------------------------------------
# Pure function: decode_abstract_inverted_index
# ---------------------------------------------------------------------------

#: The OpenAlex ``abstract_inverted_index`` maps word -> list of 0-based
#: positions. This function reconstructs the abstract string from that mapping.


def decode_abstract_inverted_index(
    idx: dict[str, list[int]] | None,
) -> str:
    """Reconstruct an abstract string from an OpenAlex ``abstract_inverted_index``.

    The OpenAlex inverted index format maps each word to the list of positions
    (0-based) where that word appears in the abstract:

        {"Despite": [0], "decades": [1], "of": [2, 20, 38], ...}

    Algorithm:
    1. If ``idx`` is None or empty -> return ``""`` (no exception raised).
    2. Build a flat list of ``(position, word)`` pairs from all entries.
    3. Sort by position ascending.
    4. Detect duplicate positions: if two words claim the same position, log a
       warning and keep the first occurrence (stable-sort order).
    5. Detect gaps: if positions are not contiguous from 0 to max(pos), fill
       each missing position with a single space (log at DEBUG level).
    6. Join the resolved words with single spaces.
    7. Strip leading/trailing whitespace; collapse repeated internal whitespace.

    Returns:
        The reconstructed abstract string, or ``""`` on any structural failure.
    """
    if not idx:
        return ""

    try:
        # Build (pos, word) pairs
        pairs: list[tuple[int, str]] = []
        for word, positions in idx.items():
            for pos in positions:
                pairs.append((pos, word))

        if not pairs:
            return ""

        # Sort by position; use word as tie-breaker for determinism
        pairs.sort(key=lambda p: (p[0], p[1]))

        max_pos = pairs[-1][0]

        # Deduplicate: build position -> word map (first occurrence wins)
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

        # Build word list, filling gaps with spaces
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

        # Join and clean up
        abstract = " ".join(tokens)
        # Collapse any runs of whitespace (gap spaces + join spaces)
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


def parse_work(raw: dict) -> PaperRecord | None:
    """Parse a raw OpenAlex work dict into a :class:`PaperRecord`.

    Extracts the fields a profile needs: title/year (with a
    guard), first-author last name, journal (``primary_location`` then
    ``host_venue``), preferred ``pdf_url``, bare ``doi``, ``pmid`` (from
    ``ids``), ``pmcid`` (from ``locations``), ``cited_by_count``, lowercased
    ``type``, and the de-inverted ``abstract``.

    This function does not compute ``paper_id``. That is ID policy, not
    OpenAlex parsing, so ``paper_id`` is left ``None`` for the caller to fill.
    ``cited_by_count``, ``pmid`` and ``pmcid`` are carried as extra fields
    (``PaperRecord`` allows extras).

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
        status="pending",
        # Extra fields (PaperRecord allows extras).
        pmid=_extract_pmid(raw),
        pmcid=_extract_pmcid(raw),
        cited_by_count=int(raw.get("cited_by_count") or 0),
    )


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
    """The ``{title, abstract, year, doi, openalex_id, coauthors}`` lite shape.

    ``doi`` is the bare DOI (no ``https://doi.org/`` prefix). ``abstract`` is
    ``None`` (not ``""``) when absent. This adapter maps the empty de-inversion
    result to ``None`` so an absent abstract is distinguishable from an empty one.
    """
    return _lite_dict(raw, author_key="coauthors")


def to_normalized_dict(raw: dict) -> dict:
    """The single-work-match shape: identical to :func:`to_work_dict` but with
    ``authors`` in place of ``coauthors`` (the citation checker keys off author
    names)."""
    return _lite_dict(raw, author_key="authors")


# ---------------------------------------------------------------------------
# Candidate fetching: the query client behind work ranking
# ---------------------------------------------------------------------------

OPENALEX_BASE_URL = "https://api.openalex.org"

#: OpenAlex entity ids for topics/concepts ("T10002", "C2778112365"), bare or
#: as full IRIs. Anything else is treated as a free-text search term.
_TOPIC_ID_RE = re.compile(r"^(?:https://openalex\.org/)?([TC]\d+)$", re.IGNORECASE)

#: Cap on seed work ids per ``cites:`` query. OpenAlex rejects unbounded
#: filter values, and a specialist's most recent works carry the signal.
_MAX_CITES_SEEDS = 100

#: OpenAlex ``type`` values that are not scholarly narrative outputs: bare
#: software/code dumps, catch-all deposits, review reports, grant records, etc.
#: These pollute the candidate stream (on this corpus ``software`` alone is
#: ~half the raw fetch: warp/GATK-style code deposits on Zenodo). ``dataset``
#: is not excluded: OpenAlex types some genuine method deposits as
#: datasets (e.g. the region-centric chromatin/methylation R framework), so
#: dropping the whole type would drop real hits too. ``book`` is excluded: its
#: members here are self-published "X for Bioinformatics" volumes, not papers.
#: The filter is a type gate only; a deposit mis-typed as ``article``/``book-
#: chapter`` still slips through and must be caught downstream.
DEFAULT_EXCLUDE_TYPES = frozenset(
    {"software", "other", "paratext", "libguides", "grant", "peer-review", "book"}
)


def _http_get_json(url: str, params: dict) -> dict:
    """The one networking call site (stub this in tests).

    ``httpx`` is imported here, not at module scope, so a core-only install
    still imports this module and fails cleanly at call time.
    """
    try:
        import httpx
    except ImportError as e:
        raise ImportError(
            "fetch_new_works requires httpx; requires the 'client' extra, see the "
            "install instructions in the README"
        ) from e
    resp = httpx.get(url, params=params, timeout=30.0, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def _transport_errors() -> tuple[type[BaseException], ...]:
    """The fetch failures that mean "no usable work", not "the caller is broken".

    ``httpx`` is imported here rather than at module scope for the same reason
    :func:`_http_get_json` does it: a core-only install must still import this
    module. An ImportError from a missing ``httpx`` is handled before this is
    ever evaluated, so the import cannot fail at this point.
    """
    import httpx

    return (httpx.HTTPError, ValueError)


def profile_query_terms(profile) -> dict:
    """Extract the OpenAlex query seeds a profile offers.

    Returns ``{"topics": [...], "seed_work_ids": [...]}``: the profile's
    ``subfields`` + ``interests`` (free-text labels, order-preserving, deduped)
    and the OpenAlex work ids of its own papers (the citation neighborhood,
    usually higher precision for specialists).
    """
    md = profile.metadata
    topics: list[str] = []
    seen: set[str] = set()
    for term in list(md.subfields or []) + list(md.interests or []):
        term = (term or "").strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            topics.append(term)
    seed_work_ids = []
    try:
        papers = profile.papers
    except (OSError, ProfileError, ValidationError):
        logger.warning(
            "could not read papers for %r; querying on topics only",
            getattr(profile, "slug", profile),
            exc_info=True,
        )
        papers = []
    for p in papers:
        oid = getattr(p, "openalex_id", None)
        if oid:
            seed_work_ids.append(str(oid).rsplit("/", 1)[-1])
    return {"topics": topics, "seed_work_ids": seed_work_ids}


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


def fetch_work(
    openalex_id: str,
    *,
    mailto: str | None = None,
    base_url: str = OPENALEX_BASE_URL,
) -> PaperRecord | None:
    """Fetch a single OpenAlex work by id and parse it into a ``PaperRecord``.

    Accepts either a bare id (``"W2741809807"``) or a full OpenAlex URL
    (``"https://openalex.org/W2741809807"``); only the trailing id segment is
    used. ``mailto`` joins the polite pool when given. Returns the parsed
    :class:`PaperRecord`, or ``None`` when the work is missing (any HTTP error,
    e.g. 404) or unusable (``parse_work`` returns ``None`` for a work with no
    title/year).

    Unlike :func:`fetch_new_works`, this is a point lookup for callers that
    hold only a work id and need the full record, abstract included, and a
    transport failure is reported as ``None`` rather than raised.
    """
    wid = str(openalex_id).strip().rsplit("/", 1)[-1]
    if not wid:
        return None
    url = f"{base_url}/works/{wid}"
    params: dict = {}
    if mailto:
        params["mailto"] = mailto
    try:
        raw = _http_get_json(url, params)
    except ImportError:
        raise
    except _transport_errors():
        return None
    return parse_work(raw)


def fetch_new_works(
    *,
    since,
    topics: list[str] | None = None,
    seed_work_ids: list[str] | None = None,
    mailto: str | None = None,
    per_page: int = 200,
    max_pages: int = 5,
    exclude_types: set[str] | frozenset[str] | None = None,
    base_url: str = OPENALEX_BASE_URL,
) -> list[PaperRecord]:
    """Query OpenAlex for works published since ``since`` relevant to a profile.

    Two independent query modes, run separately (OpenAlex cannot OR across
    filters) and deduped by OpenAlex id:

    - ``topics``: entity ids (``T…``/``C…``, bare or IRI) become ``topics.id:``
      / ``concepts.id:`` filters; free-text labels become one OR-joined
      ``title_and_abstract.search:`` filter.
    - ``seed_work_ids``: the profile's own works, as a ``cites:`` filter for
      new works citing them (capped at the first 100 seeds).

    ``since`` is a ``datetime.date`` or a ``YYYY-MM-DD`` string. ``mailto``
    joins the polite pool when given. Pagination is cursor-based, capped at
    ``max_pages`` pages per query mode. Raw results run through
    :func:`parse_work`, so callers get ``PaperRecord`` objects; unusable works
    (no title/year) are dropped.

    ``exclude_types`` drops non-paper deposit types (software/dataset dumps,
    grant records, review reports…) from the candidate stream. ``None`` applies
    :data:`DEFAULT_EXCLUDE_TYPES`; pass an empty set to keep every type. Each
    excluded value is negated at the OpenAlex query level (``type:!x``, which
    shrinks the fetch so more real papers fit the page budget) and re-checked
    post-parse against :attr:`PaperRecord.type` as a guarantee.

    A transport failure on any page propagates and discards the works already
    collected, unlike :func:`fetch_work`, which returns ``None`` instead.
    """
    if exclude_types is None:
        exclude_types = DEFAULT_EXCLUDE_TYPES
    drop_types = {t.strip().lower() for t in exclude_types if t and t.strip()}
    queries = _new_works_queries(since, topics, seed_work_ids, drop_types)

    url = f"{base_url}/works"
    out: list[PaperRecord] = []
    seen_ids: set[str] = set()
    for filt in queries:
        for raw in _iter_work_results(
            url, filt, mailto=mailto, per_page=per_page, max_pages=max_pages
        ):
            rec = _accept_new_work(raw, drop_types, seen_ids)
            if rec is not None:
                out.append(rec)
    return out


def _new_works_queries(
    since,
    topics: list[str] | None,
    seed_work_ids: list[str] | None,
    drop_types: set[str],
) -> list[str]:
    """One OpenAlex filter string per query mode (topics, then cites)."""
    since_str = since.isoformat() if hasattr(since, "isoformat") else str(since)
    base_filter = f"from_publication_date:{since_str}"
    type_clause = "".join(f",type:!{t}" for t in sorted(drop_types))

    queries: list[str] = []
    for clause in _topic_filters(list(topics or [])):
        queries.append(f"{base_filter},{clause}{type_clause}")
    seeds = [str(s).rsplit("/", 1)[-1] for s in (seed_work_ids or []) if s]
    if seeds:
        queries.append(f"{base_filter},cites:" + "|".join(seeds[:_MAX_CITES_SEEDS]) + type_clause)
    if not queries:
        raise ValueError("fetch_new_works needs topics and/or seed_work_ids")
    return queries


def _iter_work_results(
    url: str,
    filt: str,
    *,
    mailto: str | None,
    per_page: int,
    max_pages: int,
):
    """Yield each raw work of a cursor-paged query, capped at ``max_pages``."""
    cursor = "*"
    for _ in range(max_pages):
        params = {"filter": filt, "per-page": per_page, "cursor": cursor}
        if mailto:
            params["mailto"] = mailto
        page = _http_get_json(url, params)
        yield from page.get("results") or []
        cursor = (page.get("meta") or {}).get("next_cursor")
        if not cursor:
            break


def _accept_new_work(raw: dict, drop_types: set[str], seen_ids: set[str]) -> PaperRecord | None:
    """Parse one raw work; None when unusable, an excluded type, or already seen."""
    rec = parse_work(raw)
    if rec is None:
        return None
    if rec.type in drop_types:
        return None
    key = rec.openalex_id or f"title:{rec.title.lower()}"
    if key in seen_ids:
        return None
    seen_ids.add(key)
    return rec


__all__ = [
    "DEFAULT_EXCLUDE_TYPES",
    "authorship_evidence",
    "decode_abstract_inverted_index",
    "fetch_new_works",
    "fetch_work",
    "parse_work",
    "profile_query_terms",
    "to_work_dict",
    "to_normalized_dict",
]
