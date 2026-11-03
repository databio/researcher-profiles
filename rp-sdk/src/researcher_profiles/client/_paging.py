"""Walk a paged list endpoint: page-based or token-based, one loop."""

from __future__ import annotations

from typing import Any, Callable, Iterator, Optional


def walk_pages(
    fetch: Callable[[dict[str, Any]], dict],
    params: Optional[dict[str, Any]] = None,
    *,
    page_size: int = 1000,
) -> Iterator[Any]:
    """Yield every row of a ``{results, pagination}`` list.

    ``fetch(query)`` returns one page's parsed JSON. Follows
    ``pagination.next_page_token`` when the server sends one, else
    ``page`` up to ``total_pages``.
    """
    base = dict(params or {})
    page, token = 0, None
    while True:
        q = {**base, "page_size": page_size}
        if token:
            q["page_token"] = token
        elif page:
            q["page"] = page
        body = fetch(q)
        yield from body.get("results") or []
        p = body.get("pagination") or {}
        token = p.get("next_page_token")
        if token:
            continue
        if p.get("page") is not None and p["page"] + 1 < (p.get("total_pages") or 0):
            page = p["page"] + 1
            continue
        return


def iter_results(
    http: Any,
    path: str,
    params: Optional[dict[str, Any]] = None,
    *,
    page_size: int = 1000,
) -> Iterator[Any]:
    """Yield every row of a paged list at ``path`` on an ``httpx.Client``.

    Raises ``PermissionError`` on 401 and the client's error on any other
    HTTP failure.
    """

    def fetch(q: dict[str, Any]) -> dict:
        resp = http.get(path, params=q)
        if resp.status_code == 401:
            raise PermissionError(resp.text)
        resp.raise_for_status()
        return resp.json()

    return walk_pages(fetch, params, page_size=page_size)


__all__ = ["iter_results", "walk_pages"]
