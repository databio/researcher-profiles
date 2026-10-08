"""The HTTP mapper: one exception handler turns every typed service error into a reply.

The service functions raise :mod:`researcher_profiles.errors` types and know
nothing about status codes; this is the one place they become HTTP. The wire
shapes are the ones the routes answered with before the service layer existed,
so the SPA and the ``rp`` CLI see no change:

=====================  ======  ==================================================
error                  status  body
=====================  ======  ==================================================
``NotFound``           404     ``{"detail": what}``
``Unauthenticated``    401     ``{"detail": message}``
``Forbidden``          403     ``{"detail": detail or message}``
``InsufficientScope``  403     ``{"detail": {"error": "insufficient_access",
                               "required", "missing", "hint"}}``
``Conflict``           409     ``{"detail": {"error": "conflict", "current",
                               "message"}}`` (or the error's own ``detail``) +
                               ``X-RP-Content-Hash`` (profile) or
                               ``X-RP-Paper-Version`` (paper)
``Invalid``            400     ``{"detail": {"error": code, "message", "valid"}}``
                               when ``code`` is set, else ``{"detail": message}``
``RateLimited``        429     ``{"detail": message}`` + ``Retry-After``
=====================  ======  ==================================================

``Invalid`` is a 400, not a 422: it is the status the edit routes have always
answered a bad patch with.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..errors import (
    Conflict,
    Forbidden,
    InsufficientScope,
    Invalid,
    NotFound,
    RateLimited,
    ServiceError,
    Unauthenticated,
)


def service_error_response(request: Request, exc: ServiceError) -> JSONResponse:  # noqa: ARG001
    """The HTTP reply for one typed service error."""
    headers: dict[str, str] = {}
    if isinstance(exc, NotFound):
        status, detail = 404, exc.what
    elif isinstance(exc, Unauthenticated):
        status, detail = 401, exc.message
    elif isinstance(exc, InsufficientScope):
        status = 403
        detail = {
            "error": "insufficient_access",
            "required": list(exc.required),
            "missing": list(exc.missing),
            "hint": exc.hint,
        }
    elif isinstance(exc, Forbidden):
        status, detail = 403, exc.detail if exc.detail is not None else exc.message
    elif isinstance(exc, Conflict):
        status = 409
        detail = {"error": "conflict", "message": exc.message}
        if exc.detail is not None:
            detail = exc.detail
        elif exc.current is not None:
            detail = {"error": "conflict", "current": exc.current, "message": exc.message}
        if exc.current is not None:
            if exc.kind == "profile":
                headers["X-RP-Content-Hash"] = exc.current
            elif exc.kind == "paper":
                headers["X-RP-Paper-Version"] = exc.current
    elif isinstance(exc, Invalid):
        status = 400
        if exc.code:
            detail = {"error": exc.code, "message": exc.message}
            if exc.valid is not None:
                detail["valid"] = list(exc.valid)
            if exc.hint is not None:
                detail["hint"] = exc.hint
        else:
            detail = exc.message
    elif isinstance(exc, RateLimited):
        status, detail = 429, exc.message
        headers["Retry-After"] = str(exc.retry_after_s)
    else:  # pragma: no cover - every subclass is handled above
        status, detail = 500, "internal error"
    return JSONResponse({"detail": detail}, status_code=status, headers=headers or None)


async def _handler(request: Request, exc: Exception) -> JSONResponse:
    return service_error_response(request, exc)  # type: ignore[arg-type]


def install_service_errors(app: FastAPI) -> None:
    """Register the mapper for every typed service error on ``app``."""
    for cls in (
        NotFound,
        Unauthenticated,
        Forbidden,
        InsufficientScope,
        Conflict,
        Invalid,
        RateLimited,
    ):
        app.add_exception_handler(cls, _handler)


__all__ = ["install_service_errors", "service_error_response"]
