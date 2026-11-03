"""GA4GH paging for every list endpoint: ``{results, pagination}``.

Two kinds, one envelope (GA4GH-REC-01 and the GA4GH schema registry's
``PagedResponse``):

- page-based, ``?page=`` (from 0) and ``?page_size=``, answered with
  ``pagination.page``, ``page_size``, ``total`` and ``total_pages``: for
  listings with a real total;
- token-based, ``?page_token=`` and ``?page_size=``, answered with
  ``pagination.next_page_token`` (``null`` on the last page): for large or
  changing sets.

``page_size`` is clamped, never refused; ``pagination.page_size`` is the value
used and ``note`` says when it was lowered.

Filters are exact matches on the field of the same name in the result row.
Different parameters AND together; a repeated parameter ORs its values.
Identifiers match exactly; human text (an affiliation) matches after trimming,
ignoring case. A query parameter a route does not know is a 400
``invalid_argument``, never silently ignored.

Sort order is fixed per endpoint and ends in a unique key, so pages do not
overlap. A page-based list can shift if the data changes between pages. Every
page re-runs the route's authorization; nothing here caches per caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, TypeVar

from fastapi import Query, Request

from ..errors import Invalid
from ..models.api import Paged, Pagination

T = TypeVar("T")


@dataclass(frozen=True)
class PageRequest:
    """A page-based request after clamping."""

    page: int
    page_size: int
    #: What the caller sent, for the clamp note.
    asked: int


def page_params(default: int = 100, maximum: int = 500, filters: Optional[Iterable[str]] = None):
    """FastAPI dependency: ``?page=`` (from 0) and ``?page_size=``, clamped to ``maximum``.

    With ``filters`` (the route's own query parameter names), any other
    parameter is a 400 first (:func:`reject_unknown_params`).
    """
    allowed = None if filters is None else PAGE_PARAMS | set(filters)

    def dep(
        request: Request,
        page: int = Query(0, ge=0),
        page_size: int = Query(default, ge=1),
    ) -> PageRequest:
        if allowed is not None:
            reject_unknown_params(request, allowed)
        return PageRequest(page=page, page_size=min(page_size, maximum), asked=page_size)

    return dep


def page_request(page: int = 0, page_size: int = 100, maximum: int = 1000) -> PageRequest:
    """A :class:`PageRequest` built outside FastAPI (MCP tools, in-process callers)."""
    if page < 0:
        raise Invalid("page must be 0 or more", code="invalid_argument")
    if page_size < 1:
        raise Invalid("page_size must be 1 or more", code="invalid_argument")
    return PageRequest(page=page, page_size=min(page_size, maximum), asked=page_size)


def clamp_note(asked: int, used: int, hint: str = "walk pages or add a filter") -> Optional[str]:
    """The ``note`` for a clamped ``page_size``, else ``None``."""
    if asked > used:
        return f"page_size clamped to {used}; {hint}"
    return None


def _total_pages(total: int, page_size: int) -> int:
    return -(-total // page_size)


def paginate(rows: Sequence[T], req: PageRequest) -> Paged[T]:
    """Page an in-memory, already-filtered, already-sorted list."""
    limit, offset = sql_window(req)
    return paged_from_sql(rows[offset : offset + limit], len(rows), req)


def sql_window(req: PageRequest) -> tuple[int, int]:
    """``(LIMIT, OFFSET)`` for a SQL-backed page."""
    return req.page_size, req.page * req.page_size


def paged_from_sql(rows: Sequence[T], total: int, req: PageRequest) -> Paged[T]:
    """Wrap one SQL page (fetched with :func:`sql_window`) and its ``COUNT``."""
    return Paged(
        results=list(rows),
        pagination=Pagination(
            page=req.page,
            page_size=req.page_size,
            total=total,
            total_pages=_total_pages(total, req.page_size),
        ),
        note=clamp_note(req.asked, req.page_size),
    )


def reject_unknown_params(request: Request, allowed: set[str]) -> None:
    """400 ``invalid_argument`` on a query parameter this route does not know.

    FastAPI ignores unknown parameters, which let a caller send ``?orcid=``
    believing it filtered. A typo'd filter must fail loudly, not return
    everything.
    """
    unknown = sorted(set(request.query_params) - set(allowed))
    if unknown:
        raise Invalid(
            f"unknown query parameter(s): {', '.join(unknown)}",
            code="invalid_argument",
            hint=f"this route accepts: {', '.join(sorted(allowed))}",
        )


#: The page-based paging parameter names, for :func:`reject_unknown_params`.
PAGE_PARAMS = frozenset({"page", "page_size"})
#: The token-based paging parameter names.
TOKEN_PARAMS = frozenset({"page_token", "page_size"})


__all__ = [
    "PAGE_PARAMS",
    "TOKEN_PARAMS",
    "PageRequest",
    "Paged",
    "Pagination",
    "clamp_note",
    "page_params",
    "page_request",
    "paged_from_sql",
    "paginate",
    "reject_unknown_params",
    "sql_window",
]
