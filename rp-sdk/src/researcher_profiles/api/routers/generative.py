"""The persona endpoints, gated by the ``persona`` consumer scope.

``ask``, ``review``, ``innovate`` and ``riff`` all put a question to an LLM in
the voice of one profile, and all four degrade the same way when no persona is
available.
"""

import logging
from typing import Any

from fastapi import Depends, HTTPException, Request

from ...models.api import (
    AskRequest,
    IdeaList,
    IdeaPayload,
    InnovateRequest,
    LLMTextResponse,
    ReviewRequest,
    RiffList,
    RiffPayload,
    RiffRequest,
)
from ...models.results import GenerativeParseError, PersonaUnavailableError
from ...privacy import (
    ViewerTier,
)
from ...store import ProfileStore
from .._projection import (
    _allowed_source_types,
    _gate_profile,
)
from ..deps import (
    get_profile,
    get_store,
    get_viewer_tier,
    require_scope,
)
from ._routers import router

logger = logging.getLogger(__name__)


def _llm_response(text_obj: Any) -> LLMTextResponse:
    from ...models.api import CitationRefPayload

    cits = []
    for c in getattr(text_obj, "citations", []) or []:
        if hasattr(c, "paper_id"):
            cits.append(
                CitationRefPayload(
                    paper_id=c.paper_id,
                    relevance=getattr(c, "relevance", None),
                    span=getattr(c, "span", None),
                )
            )
        elif isinstance(c, dict):
            cits.append(CitationRefPayload(**c))
        else:
            cits.append(CitationRefPayload(paper_id=str(c)))
    return LLMTextResponse(
        text=getattr(text_obj, "text", ""),
        citations=cits,
        model=getattr(text_obj, "model", ""),
        usage=dict(getattr(text_obj, "usage", {}) or {}),
        request_id=getattr(text_obj, "request_id", None),
        refused=bool(getattr(text_obj, "refused", False)),
        refusal_reason=getattr(text_obj, "refusal_reason", None),
        grounded=bool(getattr(text_obj, "grounded", True)),
    )


@router.post(
    "/profiles/{slug}/ask",
    response_model=LLMTextResponse,
    dependencies=[Depends(require_scope("persona"))],
)
def ask_profile(
    slug: str,
    body: AskRequest,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> LLMTextResponse:
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    try:
        resp = prof.persona.ask(
            body.question,
            k=body.k,
            source_types=_allowed_source_types(prof.metadata, viewer),
            model=body.model,
            strict_corpus=body.strict_corpus,
            refusal_threshold=body.refusal_threshold,
            history=body.history,
        )
    except PersonaUnavailableError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    # Boundary: the whole persona stack, including a third-party LLM.
    except Exception as e:
        logger.exception("ask failed for %s", slug)
        raise HTTPException(status_code=500, detail=f"ask failed: {e}") from e
    return _llm_response(resp)


@router.post(
    "/profiles/{slug}/review",
    response_model=LLMTextResponse,
    dependencies=[Depends(require_scope("persona"))],
)
def review_profile(
    slug: str,
    body: ReviewRequest,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> LLMTextResponse:
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    try:
        resp = prof.persona.review(
            body.material,
            source_types=_allowed_source_types(prof.metadata, viewer),
            focus=body.focus,
            k=body.k,
            model=body.model,
            strict_corpus=body.strict_corpus,
            refusal_threshold=body.refusal_threshold,
        )
    except PersonaUnavailableError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    # Boundary: the whole persona stack, including a third-party LLM.
    except Exception as e:
        logger.exception("review failed for %s", slug)
        raise HTTPException(status_code=500, detail=f"review failed: {e}") from e
    return _llm_response(resp)


@router.post(
    "/profiles/{slug}/innovate",
    response_model=IdeaList,
    dependencies=[Depends(require_scope("persona"))],
)
def innovate_profile(
    slug: str,
    body: InnovateRequest,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> IdeaList:
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    try:
        ideas = prof.persona.innovate(
            body.topic,
            n=body.n,
            source_types=_allowed_source_types(prof.metadata, viewer),
            k=body.k,
            model=body.model,
            temperature=body.temperature,
        )
    except PersonaUnavailableError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except GenerativeParseError as e:
        raise HTTPException(status_code=502, detail=f"LLM parse error: {e}") from e
    # Boundary: the whole persona stack, including a third-party LLM.
    except Exception as e:
        logger.exception("innovate failed for %s", slug)
        raise HTTPException(status_code=500, detail=f"innovate failed: {e}") from e
    return IdeaList(
        items=[
            IdeaPayload(
                hypothesis=i.hypothesis,
                approach=i.approach,
                rationale=i.rationale,
                related_works=list(i.related_works or []),
            )
            for i in ideas
        ]
    )


@router.post(
    "/profiles/{slug}/riff",
    response_model=RiffList,
    dependencies=[Depends(require_scope("persona"))],
)
def riff_profile(
    slug: str,
    body: RiffRequest,
    request: Request,
    store: ProfileStore = Depends(get_store),
    viewer: ViewerTier = Depends(get_viewer_tier),
) -> RiffList:
    prof = get_profile(slug, store)
    _gate_profile(request, prof, viewer, slug)
    try:
        riffs = prof.persona.riff(
            body.seed,
            n=body.n,
            source_types=_allowed_source_types(prof.metadata, viewer),
            k=body.k,
            model=body.model,
            temperature=body.temperature,
        )
    except PersonaUnavailableError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except GenerativeParseError as e:
        raise HTTPException(status_code=502, detail=f"LLM parse error: {e}") from e
    # Boundary: the whole persona stack, including a third-party LLM.
    except Exception as e:
        logger.exception("riff failed for %s", slug)
        raise HTTPException(status_code=500, detail=f"riff failed: {e}") from e
    return RiffList(
        items=[RiffPayload(angle=r.angle, text=r.text, related_work=r.related_work) for r in riffs]
    )


# Streaming upgrade path (deferred):
# - Add /api/v1/profiles/{slug}/ask/stream returning text/event-stream
#   via sse-starlette EventSourceResponse, and a matching async generator
#   on the client.
