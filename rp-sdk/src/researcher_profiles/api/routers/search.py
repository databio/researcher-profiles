"""Retrieval over the corpus, gated by the ``match`` consumer scope.

One profile's index (``search``, ``rank-works``), the whole store
(``match``, ``match/reviewers``), and the derived graph (``coi/check``,
``graph/neighbors``). ``match_reviewers`` straddles ranking and the graph
and stays here with the rest of its scope.
"""

import logging
import os
from typing import Any

import pydantic
from fastapi import Depends, HTTPException, Request

from ...errors import CapabilityUnavailableError
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
from ...privacy import (
    ViewerTier,
)
from ...store import ProfileStore
from .._projection import (
    _allowed_source_types,
    _gate_profile,
    _ranked_visible,
    _visible_hits,
)
from ..deps import (
    get_graph,
    get_match_store,
    get_profile,
    get_store,
    get_viewer_tier,
    require_scope,
)
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
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> SearchResponse:
    """Semantic search over one profile, projected through the viewer's tier.

    The served index (``.cache/embeddings.sqlite``) holds ``cv``, ``web``, and
    ``grant`` chunks as well as public ones, and this route returns chunk text
    verbatim, so without the projection a ``match``-scoped consumer reads a
    researcher's CV back a chunk at a time. Restricted source types are
    excluded from the query and dropped from the result: the first keeps them
    from crowding out results the caller may actually have, the second is the
    guarantee.
    """
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    allowed = _allowed_source_types(prof.metadata, viewer)
    query_filter = dict(body.filter or {})
    if allowed is not None:
        requested = query_filter.get("source_type")
        if requested is not None:
            requested = [requested] if isinstance(requested, str) else list(requested)
            allowed = [st for st in allowed if st in requested]
        query_filter["source_type"] = allowed
    try:
        hits = prof.index.search(body.query, k=body.k, filter=query_filter or None)
    except CapabilityUnavailableError as e:
        # This backend has no local index at all (a SQL store, a static host).
        # 501, not 500: the server is fine, this operation is not offered here.
        raise HTTPException(status_code=501, detail=str(e)) from e
    # Boundary: the whole embedding stack behind one profile's index.
    except Exception as e:
        logger.exception("search failed for %s", slug)
        raise HTTPException(status_code=500, detail=f"search failed: {e}") from e
    visible = _visible_hits(prof.metadata, viewer, hits)
    return SearchResponse(hits=[_search_hit_payload(h) for h in visible])


def _search_hit_payload(h: Any) -> SearchHitPayload:
    return SearchHitPayload(
        text=h.text,
        source_type=h.source_type,
        source_id=h.source_id,
        chunk_index=h.chunk_index,
        section=h.section,
        cosine=h.cosine,
        score=h.score,
        meta=h.meta or {},
    )


@router.post(
    "/match",
    response_model=MatchResponse,
    dependencies=[Depends(require_scope("match"))],
)
def match_profiles(
    body: MatchRequest,
    request: Request,
    vstore=Depends(get_match_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> MatchResponse:
    """Rank all indexed profiles against a free-text query.

    Runs the store's centroid prefilter + chunk re-rank + optional MMR
    diversification (``store.match.rank``) and returns each match's
    slug, name, ORCID, score, and evidence. Chunk-level evidence is included
    only when ``include_chunks=True``.
    """
    try:
        matches = vstore.match.rank(
            body.query,
            k=body.k,
            prefilter=body.prefilter,
            require_topics=body.require_topics,
            diversify=body.diversify,
            lambda_=body.lambda_,
            topk_chunks=body.topk_chunks,
            normalize=body.normalize,
        )
    # Boundary: the whole store-wide match stack.
    except Exception as e:
        logger.exception("match failed for query %r", body.query)
        raise HTTPException(status_code=500, detail=f"match failed: {e}") from e

    results: list[MatchResult] = []
    for m in matches:
        prof = m.profile
        # A profile this viewer may not see is not a match they may have: a
        # ranking that names it has disclosed it exists. A ranked object that
        # cannot answer for its own tier is skipped rather than trusted: an
        # unanswerable privacy question is a "no".
        allowed, per_profile = _ranked_visible(request, prof, viewer)
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
    )


def _match_ranked_floor() -> float:
    """The minimum ``ranked/total`` fraction ``/match`` must clear, or fail loud.

    ``RESEARCHER_PROFILES_MATCH_MIN_RANKED_FRACTION``, default 0 (disabled).
    Most deployments legitimately see fewer ranked results than the corpus
    size (privacy-tier filtering, low relevance), so this is off unless an
    operator opts in; a wrong default would turn ordinary narrow results into
    false 503s.
    """
    raw = os.environ.get("RESEARCHER_PROFILES_MATCH_MIN_RANKED_FRACTION")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid RESEARCHER_PROFILES_MATCH_MIN_RANKED_FRACTION=%r; ignoring", raw)
        return 0.0


# ---------------------------------------------------------------------------
# Profile graph: COI check, COI-filtered reviewer match, neighborhood
#
# These compose over /match (store.match.rank), never replace it: reviewer matching
# is /match then a COI drop; team/collab discovery is a neighborhood frontier
# then /match ranking. All are privileged (reviewer/COI queries), so they gate on
# the ``match`` consumer scope, like the rest of the heavy read surface.
# ---------------------------------------------------------------------------


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

    Given a manuscript's ``author_set`` and a ``candidate`` reviewer (slug or
    rid), return every COI edge between them (coauthorship within ``years``,
    a shared institution, or an advising relationship) with the reason and the
    parameters that fired. An author passed with only a name + affiliation (no
    profile) still trips a same-institution COI.
    """
    try:
        candidate_key = graph.resolve(body.candidate)
    except (KeyError, ValueError) as e:
        raise HTTPException(
            status_code=404, detail=f"candidate {body.candidate!r} not found in graph"
        ) from e
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
    request: Request,
    vstore=Depends(get_match_store),
    graph=Depends(get_graph),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> ReviewerMatchResponse:
    """Expertise ranking (``/match``) composed with a COI filter.

    Ranks all indexed profiles against ``query`` exactly as ``/match`` does, then
    for each candidate runs the same COI logic as ``/coi/check`` against
    ``author_set`` within ``years``. ``mode='drop'`` (default) removes conflicted
    candidates; ``mode='annotate'`` keeps them and attaches a ``coi`` block. This
    is a thin wrapper over ``store.match.rank``: the ranking code is not forked.
    """
    if body.mode not in ("drop", "annotate"):
        raise HTTPException(status_code=400, detail="mode must be 'drop' or 'annotate'")
    try:
        matches = vstore.match.rank(
            body.query,
            k=body.k,
            prefilter=body.prefilter,
            require_topics=body.require_topics,
            diversify=body.diversify,
            lambda_=body.lambda_,
            topk_chunks=body.topk_chunks,
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
        allowed, per_profile = _ranked_visible(request, prof, viewer)
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
    return ReviewerMatchResponse(matches=results)


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
    neighbors, 2 for the reachable-but-not-direct frontier). Backs team assembly
    and the collaboration recommender.
    """
    from ...graph import EdgeType

    params = request.query_params
    types = None
    raw_types = params.get("types")
    if raw_types:
        try:
            types = [EdgeType(t.strip()) for t in raw_types.split(",") if t.strip()]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"invalid edge type: {e}") from e
    since_year = None
    if params.get("since_year"):
        try:
            since_year = int(params["since_year"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail="since_year must be an integer") from e
    max_hops = 1
    if params.get("max_hops"):
        try:
            max_hops = int(params["max_hops"])
        except ValueError as e:
            raise HTTPException(status_code=400, detail="max_hops must be an integer") from e
    if max_hops < 1 or max_hops > 3:
        raise HTTPException(status_code=400, detail="max_hops must be between 1 and 3")

    try:
        center_key = graph.resolve(ref)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=404, detail=f"ref {ref!r} not found in graph") from e
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
    """The ranking function, lazily resolved so a core-only install 503s.

    Same degradation contract as ``get_match_store``: the embeddings tier is an
    extra, and its absence is "matching unavailable", never a 500.
    """
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
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> RankWorksResponse:
    """Rank candidate works against one profile: the inverse of ``/match``.

    Mode (a): the caller supplies candidate ``works`` (PaperRecord-shaped
    dicts). Mode (b): ``use_openalex=true`` fetches works published since
    ``since`` (default: the last 30 days) from OpenAlex, seeded by the
    profile's topics and citation neighborhood, then ranks them.
    """
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    rank = _rank_works()

    if body.works:
        from ...schema import PaperRecord

        try:
            works = [PaperRecord.model_validate(w) for w in body.works]
        except pydantic.ValidationError as e:
            raise HTTPException(status_code=400, detail=f"invalid candidate work: {e}") from e
    elif body.use_openalex:
        from datetime import date, timedelta

        from ...openalex import fetch_new_works, profile_query_terms

        since = body.since or (date.today() - timedelta(days=30)).isoformat()
        terms = profile_query_terms(prof)
        if not terms["topics"] and not terms["seed_work_ids"]:
            raise HTTPException(
                status_code=400,
                detail="profile has no subfields/interests or OpenAlex work ids to query with",
            )
        try:
            works = fetch_new_works(
                since=since,
                topics=terms["topics"],
                seed_work_ids=terms["seed_work_ids"],
                mailto=body.mailto,
                max_pages=body.max_pages,
            )
        # Boundary: a live third-party HTTP API.
        except Exception as e:
            logger.exception("OpenAlex fetch failed for %s", slug)
            raise HTTPException(status_code=502, detail=f"OpenAlex fetch failed: {e}") from e
    else:
        raise HTTPException(
            status_code=400, detail="supply candidate works or set use_openalex=true"
        )

    from ...embeddings.cache import IndexNotBuiltError

    try:
        ranked = rank(
            prof,
            works,
            k=body.k,
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
    )
