"""The persona endpoints (LLM answers in one profile's voice), gated by the ``persona`` scope.

All four answer 409 when no persona is available.
"""

import logging
from typing import Any

from fastapi import Depends, HTTPException

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
from .._limits import PERSONA_K_CAP, clamp
from .._projection import _allowed_source_types
from ..caller import Caller
from ..deps import get_read_caller, get_service, require_scope
from ..service import Service
from ._routers import router

logger = logging.getLogger(__name__)


def _llm_response(text_obj: Any, k_applied: int) -> LLMTextResponse:
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
        k_applied=k_applied,
    )


@router.post(
    "/profiles/{slug}/ask",
    response_model=LLMTextResponse,
    dependencies=[Depends(require_scope("persona"))],
)
def ask_profile(
    slug: str,
    body: AskRequest,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> LLMTextResponse:
    _store, prof, viewer = service.read(caller, slug)
    k = clamp(body.k, 5, PERSONA_K_CAP)
    try:
        resp = prof.persona.ask(
            body.question,
            k=k,
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
    return _llm_response(resp, k)


@router.post(
    "/profiles/{slug}/review",
    response_model=LLMTextResponse,
    dependencies=[Depends(require_scope("persona"))],
)
def review_profile(
    slug: str,
    body: ReviewRequest,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> LLMTextResponse:
    _store, prof, viewer = service.read(caller, slug)
    k = clamp(body.k, 5, PERSONA_K_CAP)
    try:
        resp = prof.persona.review(
            body.material,
            source_types=_allowed_source_types(prof.metadata, viewer),
            focus=body.focus,
            k=k,
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
    return _llm_response(resp, k)


@router.post(
    "/profiles/{slug}/innovate",
    response_model=IdeaList,
    dependencies=[Depends(require_scope("persona"))],
)
def innovate_profile(
    slug: str,
    body: InnovateRequest,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> IdeaList:
    _store, prof, viewer = service.read(caller, slug)
    k = clamp(body.k, 12, PERSONA_K_CAP)
    try:
        ideas = prof.persona.innovate(
            body.topic,
            n=body.n,
            source_types=_allowed_source_types(prof.metadata, viewer),
            k=k,
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
        ],
        k_applied=k,
    )


@router.post(
    "/profiles/{slug}/riff",
    response_model=RiffList,
    dependencies=[Depends(require_scope("persona"))],
)
def riff_profile(
    slug: str,
    body: RiffRequest,
    service: Service = Depends(get_service),
    caller: Caller = Depends(get_read_caller),
) -> RiffList:
    _store, prof, viewer = service.read(caller, slug)
    k = clamp(body.k, 4, PERSONA_K_CAP)
    try:
        riffs = prof.persona.riff(
            body.seed,
            n=body.n,
            source_types=_allowed_source_types(prof.metadata, viewer),
            k=k,
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
        items=[RiffPayload(angle=r.angle, text=r.text, related_work=r.related_work) for r in riffs],
        k_applied=k,
    )
