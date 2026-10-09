"""Retrieval over one profile, the whole store, and the graph; all gated by the ``match`` scope."""

import logging
import os
from typing import Any

import pydantic
from fastapi import Depends, HTTPException, Request

from ...errors import Invalid, NotFound
from ...models.api import (
    CoiBlock,
    CoiCheckRequest,
    CoiCheckResponse,
    CoiReasonPayload,
    GraphEdgePayload,
    GraphNodePayload,
    MatchEvidencePayload,
    MatchRequest,
    MatchResponse,
    MatchResult,
    NeighborPayload,
    NeighborsResponse,
    RankedWorkPayload,
    RankWorksRequest,
    RankWorksResponse,
    ReviewerMatchRequest,
    ReviewerMatchResponse,
    ReviewerMatchResult,
    SearchHitPayload,
    SearchRequest,
    SearchResponse,
)
from ...privacy import CHUNK_SOURCE_TYPE_ROLE
from ...schema import ResearchInterest
from .. import _semantic
from .._limits import (
    MATCH_K,
    MATCH_PREFILTER_CAP,
    RANK_K,
    RANK_MAX_PAGES_CAP,
    SEARCH_K,
    TOPK_CHUNKS_CAP,
    clamp,
    truncate_words,
)
from .._projection import _allowed_source_types, _visible_hits
from ..caller import Caller
from ..deps import get_graph, get_match_store, get_read_caller, get_service, require_scope
from ..service import Service
from ._routers import router

logger = logging.getLogger(__name__)


@router.post(
    "/profiles/{slug}/search",
    response_model=SearchResponse,
    dependencies=[Depends(require_scope("match"))],
)
def search_profile(
    slug: str,
    body: SearchRequest,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> SearchResponse:
    """Semantic search over one profile, projected through the viewer's tier.

    Chunks this viewer may not read are excluded from the query (so they do
    not crowd out allowed hits) and dropped from the result (the guarantee).
    ``k`` defaults to 5 and is clamped to 20 (``k_applied``); hit text is cut to
    ``SNIPPET_CHARS`` (``truncated``).
    """
    store, prof, viewer = service.read(caller, slug)
    k = clamp(body.k, *SEARCH_K)
    allowed = _allowed_source_types(prof.metadata, viewer)
    types = list(allowed) if allowed is not None else list(CHUNK_SOURCE_TYPE_ROLE)
    requested = (body.filter or {}).get("source_type")
    if requested is not None:
        requested = [requested] if isinstance(requested, str) else list(requested)
        types = [st for st in types if st in requested]
    hits, note = _semantic.search_chunks(store, prof, viewer, body.query, source_types=types, k=k)
    if hits is None:
        # No vectors or no encoder: unavailable, not a server fault.
        raise HTTPException(status_code=503, detail=note or "semantic search unavailable")
    visible = _visible_hits(prof.metadata, viewer, hits)
    return SearchResponse(hits=[_search_hit_payload(h) for h in visible], k_applied=k)


def _search_hit_payload(h: Any) -> SearchHitPayload:
    text, truncated = truncate_words(h.text or "")
    return SearchHitPayload(
        text=text,
        truncated=truncated,
        source_type=h.source_type,
        source_id=h.source_id,
        chunk_index=h.chunk_index,
        section=h.section,
        cosine=h.cosine,
        score=h.score,
        meta=getattr(h, "meta", None) or {},
    )


@router.post(
    "/match",
    response_model=MatchResponse,
    dependencies=[Depends(require_scope("match"))],
)
def match_profiles(
    body: MatchRequest,
    vstore=Depends(get_match_store),
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> MatchResponse:
    """Rank all indexed profiles against a free-text query.

    Chunk-level evidence is included only when ``include_chunks=True``.
    ``interests`` add a topic component (see ``MatchManager.rank``).
    """
    try:
        interests = [ResearchInterest.model_validate(i) for i in body.interests]
    except pydantic.ValidationError as e:
        raise HTTPException(status_code=422, detail=f"invalid interests: {e}") from e
    k, prefilter, topk_chunks = _match_caps(body)
    try:
        matches = vstore.match.rank(
            body.query,
            k=k,
            prefilter=prefilter,
            require_topics=body.require_topics,
            diversify=body.diversify,
            lambda_=body.lambda_,
            topk_chunks=topk_chunks,
            normalize=body.normalize,
            interests=interests or None,
            topic_alpha=body.topic_alpha,
        )
    # Boundary: the whole store-wide match stack.
    except Exception as e:
        logger.exception("match failed for query %r", body.query)
        raise HTTPException(status_code=500, detail=f"match failed: {e}") from e

    results: list[MatchResult] = []
    for m in matches:
        prof = m.profile
        # Naming a hidden profile in a ranking would disclose that it exists.
        allowed, per_profile = service.visible(caller, prof)
        if not allowed:
            continue
        ev = m.evidence
        evidence = MatchEvidencePayload(
            centroid_score=ev.centroid_score,
            top_papers=list(ev.top_papers or []),
            overlapping_topics=list(ev.overlapping_topics or []),
            matched_topics=list(ev.matched_topics or []),
            top_chunks=(
                [
                    _search_hit_payload(h)
                    for h in _visible_hits(
                        getattr(prof, "metadata", None), per_profile, ev.top_chunks
                    )
                ]
                if body.include_chunks
                else []
            ),
        )
        orcid = prof.orcid
        results.append(
            MatchResult(
                slug=prof.slug,
                rid=getattr(prof, "rid", None),
                name=getattr(prof, "name", prof.slug),
                orcid=orcid,
                score=m.score,
                evidence=evidence,
            )
        )

    total_profiles = len(vstore.list_slugs())
    ranked_profiles = len(results)
    floor = _match_ranked_floor()
    if floor > 0 and total_profiles > 0 and (ranked_profiles / total_profiles) < floor:
        raise HTTPException(
            status_code=503,
            detail=(
                f"match ranked {ranked_profiles}/{total_profiles} profiles, below the "
                f"configured floor ({floor:.0%}) set by "
                "RESEARCHER_PROFILES_MATCH_MIN_RANKED_FRACTION: this looks like a "
                "broken embedding path rather than a genuinely narrow result"
            ),
        )

    return MatchResponse(
        matches=results,
        ranked_profiles=ranked_profiles,
        total_profiles=total_profiles,
        k_applied=k,
        prefilter_applied=prefilter,
    )


def _match_caps(body) -> tuple[int, int, int]:
    """``(k, prefilter, topk_chunks)`` clamped to their caps (``_limits``)."""
    k = clamp(body.k, *MATCH_K)
    prefilter = clamp(body.prefilter, max(k, 10), MATCH_PREFILTER_CAP)
    topk_chunks = clamp(body.topk_chunks, 5, TOPK_CHUNKS_CAP)
    return k, prefilter, topk_chunks


def _match_ranked_floor() -> float:
    """The minimum ``ranked/total`` fraction ``/match`` must clear, or fail loud.

    ``RESEARCHER_PROFILES_MATCH_MIN_RANKED_FRACTION``, default 0 (off), since
    tier filtering and low relevance make narrow results normal.
    """
    raw = os.environ.get("RESEARCHER_PROFILES_MATCH_MIN_RANKED_FRACTION")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid RESEARCHER_PROFILES_MATCH_MIN_RANKED_FRACTION=%r; ignoring", raw)
        return 0.0


# Profile graph: COI check, COI-filtered reviewer match, neighborhood.


def _coi_reason_payload(r) -> CoiReasonPayload:
    return CoiReasonPayload(
        author_key=r.author_key,
        type=r.type,
        author_name=r.author_name,
        author_rid=r.author_rid,
        last_year=r.last_year,
        in_window=r.in_window,
        paper_count=r.paper_count,
        institution=r.institution,
        overlap_years=r.overlap_years,
        direction=r.direction,
        confidence=r.confidence,
    )


def _graph_node_payload(node) -> GraphNodePayload:
    return GraphNodePayload(
        key=node.key,
        kind=node.kind,
        rid=node.rid,
        slug=node.slug,
        name=node.name,
    )


def _graph_edge_payload(e) -> GraphEdgePayload:
    return GraphEdgePayload(
        src=e.src,
        dst=e.dst,
        type=e.type.value,
        directed=e.directed,
        confidence=e.confidence,
        paper_count=e.paper_count,
        first_year=e.first_year,
        last_year=e.last_year,
        institution=e.institution,
        overlap_years=e.overlap_years,
        training_kind=e.training_kind,
        evidence=list(e.evidence or []),
    )


@router.post(
    "/coi/check",
    response_model=CoiCheckResponse,
    dependencies=[Depends(require_scope("match"))],
)
def coi_check(
    body: CoiCheckRequest,
    graph=Depends(get_graph),
) -> CoiCheckResponse:
    """Conflict-of-interest check between a candidate and a set of authors.

    Returns every COI edge (coauthorship within ``years``, shared institution,
    advising) with its reason. An author given only by name and affiliation
    still trips a same-institution COI.
    """
    try:
        candidate_key = graph.resolve(body.candidate)
    except (KeyError, ValueError) as e:
        raise NotFound(f"candidate {body.candidate!r} not found in graph") from e
    author_descriptors = [a.model_dump() for a in body.author_set]
    verdict = graph.coi_edges(author_descriptors, candidate_key, years=body.years)
    node = graph.get_node(candidate_key)
    return CoiCheckResponse(
        candidate=(node.slug if node and node.slug else candidate_key),
        rid=(node.rid if node else None),
        has_coi=verdict.has_coi,
        reasons=[_coi_reason_payload(r) for r in verdict.reasons],
    )


@router.post(
    "/match/reviewers",
    response_model=ReviewerMatchResponse,
    dependencies=[Depends(require_scope("match"))],
)
def match_reviewers(
    body: ReviewerMatchRequest,
    vstore=Depends(get_match_store),
    graph=Depends(get_graph),
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> ReviewerMatchResponse:
    """Expertise ranking (``/match``) composed with a COI filter.

    ``mode='drop'`` (default) removes conflicted candidates; ``mode='annotate'``
    keeps them with a ``coi`` block.
    """
    if body.mode not in ("drop", "annotate"):
        raise Invalid("mode must be 'drop' or 'annotate'")
    k, prefilter, topk_chunks = _match_caps(body)
    try:
        matches = vstore.match.rank(
            body.query,
            k=k,
            prefilter=prefilter,
            require_topics=body.require_topics,
            diversify=body.diversify,
            lambda_=body.lambda_,
            topk_chunks=topk_chunks,
            normalize=body.normalize,
        )
    # Boundary: the match stack plus the derived graph.
    except Exception as e:
        logger.exception("reviewer match failed for query %r", body.query)
        raise HTTPException(status_code=500, detail=f"match failed: {e}") from e

    author_descriptors = [a.model_dump() for a in body.author_set]
    results: list[ReviewerMatchResult] = []
    for m in matches:
        prof = m.profile
        allowed, per_profile = service.visible(caller, prof)
        if not allowed:
            continue
        ev = m.evidence
        evidence = MatchEvidencePayload(
            centroid_score=ev.centroid_score,
            top_papers=list(ev.top_papers or []),
            overlapping_topics=list(ev.overlapping_topics or []),
            top_chunks=(
                [
                    _search_hit_payload(h)
                    for h in _visible_hits(
                        getattr(prof, "metadata", None), per_profile, ev.top_chunks
                    )
                ]
                if body.include_chunks
                else []
            ),
        )
        orcid = prof.orcid
        rid = getattr(prof, "rid", None)

        coi_block = None
        if author_descriptors and rid is not None:
            try:
                verdict = graph.coi_edges(author_descriptors, rid, years=body.years)
            except (KeyError, ValueError):
                verdict = None
            if verdict is not None and verdict.has_coi:
                coi_block = CoiBlock(
                    has_coi=True,
                    reasons=[_coi_reason_payload(r) for r in verdict.reasons],
                )
        if coi_block is not None and body.mode == "drop":
            continue
        results.append(
            ReviewerMatchResult(
                slug=prof.slug,
                rid=rid,
                name=getattr(prof, "name", prof.slug),
                orcid=orcid,
                score=m.score,
                evidence=evidence,
                coi=coi_block if body.mode == "annotate" else None,
            )
        )
    return ReviewerMatchResponse(matches=results, k_applied=k, prefilter_applied=prefilter)


@router.get(
    "/graph/neighbors/{ref}",
    response_model=NeighborsResponse,
    dependencies=[Depends(require_scope("match"))],
)
def graph_neighbors(
    ref: str,
    request: Request,
    graph=Depends(get_graph),
) -> NeighborsResponse:
    """Neighborhood of one person: coauthors and collaborators-of-collaborators.

    Query params: ``types`` (comma-separated edge types, default all),
    ``since_year`` (recency filter on coauthor edges), ``max_hops`` (1 for direct
    neighbors, 2 for the reachable-but-not-direct frontier).
    """
    from ...graph import EdgeType

    params = request.query_params
    types = None
    raw_types = params.get("types")
    if raw_types:
        try:
            types = [EdgeType(t.strip()) for t in raw_types.split(",") if t.strip()]
        except ValueError as e:
            raise Invalid(f"invalid edge type: {e}") from e
    since_year = None
    if params.get("since_year"):
        try:
            since_year = int(params["since_year"])
        except ValueError as e:
            raise Invalid("since_year must be an integer") from e
    max_hops = 1
    if params.get("max_hops"):
        try:
            max_hops = int(params["max_hops"])
        except ValueError as e:
            raise Invalid("max_hops must be an integer") from e
    if max_hops < 1 or max_hops > 3:
        raise Invalid("max_hops must be between 1 and 3")

    try:
        center_key = graph.resolve(ref)
    except (KeyError, ValueError) as e:
        raise NotFound(f"ref {ref!r} not found in graph") from e
    center = graph.get_node(center_key)
    neighbors = graph.neighbors(center_key, types=types, since_year=since_year, max_hops=max_hops)
    return NeighborsResponse(
        center=_graph_node_payload(center),
        neighbors=[
            NeighborPayload(
                node=_graph_node_payload(n.node),
                hops=n.hops,
                edges=[_graph_edge_payload(e) for e in n.edges],
            )
            for n in neighbors
        ],
    )


def _rank_works():
    """The ranking function, lazily resolved so a core-only install gets a 503, not a 500."""
    try:
        from ...embeddings.rank import rank_works_against_profile
    except ImportError as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "matching unavailable: install the 'vectors' and 'st' extras and build indexes"
            ),
        ) from e
    return rank_works_against_profile


@router.post(
    "/profiles/{slug}/rank-works",
    response_model=RankWorksResponse,
    dependencies=[Depends(require_scope("match"))],
)
def rank_works_for_profile(
    slug: str,
    body: RankWorksRequest,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> RankWorksResponse:
    """Rank candidate works against one profile: the inverse of ``/match``.

    Mode (a): the caller supplies candidate ``works`` (PaperRecord-shaped
    dicts). Mode (b): ``use_openalex=true`` fetches works published since
    ``since`` (default: the last 30 days) from OpenAlex, seeded by the
    profile's topics and citation neighborhood, then ranks them.
    """
    store, prof, _viewer = service.read(caller, slug)
    rank = _rank_works()
    k = clamp(body.k, *RANK_K)
    max_pages = clamp(body.max_pages, 5, RANK_MAX_PAGES_CAP)

    if body.works:
        from ...schema import PaperRecord

        try:
            works = [PaperRecord.model_validate(w) for w in body.works]
        except pydantic.ValidationError as e:
            raise Invalid(f"invalid candidate work: {e}") from e
    elif body.use_openalex:
        import os
        from datetime import date, timedelta

        from ...openalex import fetch_new_works, profile_query_terms
        from ...openalex_client import OpenAlexBudgetError, OpenAlexClient

        since = body.since or (date.today() - timedelta(days=30)).isoformat()
        terms = profile_query_terms(prof)
        if not terms["topics"] and not terms["seed_work_ids"]:
            raise Invalid("profile has no subfields/interests or OpenAlex work ids to query with")
        try:
            with OpenAlexClient(os.environ.get("OPENALEX_API_KEY")) as client:
                works = fetch_new_works(
                    client,
                    since=since,
                    topics=terms["topics"],
                    seed_work_ids=terms["seed_work_ids"],
                    max_pages=max_pages,
                )
        except OpenAlexBudgetError as e:
            raise HTTPException(
                status_code=503,
                detail="OpenAlex's daily limit is used up; try again after midnight UTC",
            ) from e
        # Boundary: a live third-party HTTP API. The detail is a fixed string:
        # an exception message can carry the request URL.
        except Exception as e:
            logger.exception("OpenAlex fetch failed for %s", slug)
            raise HTTPException(status_code=502, detail="OpenAlex fetch failed") from e
    else:
        raise Invalid("supply candidate works or set use_openalex=true")

    from ...embeddings.cache import IndexNotBuiltError

    try:
        ranked = rank(
            prof,
            works,
            k=k,
            kind=body.kind,
            diversify=body.diversify,
            lambda_=body.lambda_,
            threshold=body.threshold,
        )
    except IndexNotBuiltError as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "matching unavailable: install the 'vectors' and 'st' extras and build indexes"
            ),
        ) from e
    # Boundary: the whole ranking stack.
    except Exception as e:
        logger.exception("work ranking failed for %s", slug)
        raise HTTPException(status_code=500, detail=f"work ranking failed: {e}") from e

    return RankWorksResponse(
        slug=store.resolve_slug(slug),
        rid=getattr(prof, "rid", None),
        works=[RankedWorkPayload(**r.to_dict()) for r in ranked],
        k_applied=k,
        max_pages_applied=max_pages,
    )
