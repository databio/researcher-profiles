"""Opaque paging cursors, bound to the query that made them.

A cursor carries where the last page stopped plus a hash of the filters it was
made under. Reusing it with different filters is a 400 ``cursor_mismatch``,
not a page of some other query's results. A cursor can be decoded by anyone,
so it carries no authority: every page re-runs the route's gates.

Two kinds:

- keyset, ``{"k": [year, paper_id], "q": hash}``: the unranked list, which does
  not skip or repeat rows when the corpus changes between pages;
- offset, ``{"o": offset, "q": hash}``: a ranked list, rebuilt per request.
  Rows can shift if the profile changes between pages.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from ..errors import Invalid


def filters_hash(filters: dict[str, Any]) -> str:
    """First 16 hex of sha256 over the canonical filters."""
    canonical = json.dumps(filters, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def encode_cursor(position: dict[str, Any], filters: dict[str, Any]) -> str:
    """``position`` is ``{"k": [...]}`` or ``{"o": n}``."""
    payload = {**position, "q": filters_hash(filters)}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _bad(message: str) -> Invalid:
    return Invalid(message, code="cursor_mismatch", hint="start again without a cursor")


def decode_cursor(cursor: str, filters: dict[str, Any]) -> dict[str, Any]:
    """The position a cursor carries; ``Invalid(code="cursor_mismatch")`` if not this query's."""
    try:
        pad = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + pad))
    except (ValueError, TypeError) as e:
        raise _bad("this cursor is not valid") from e
    if not isinstance(payload, dict) or payload.get("q") != filters_hash(filters):
        raise _bad("this cursor does not match this query and these filters")
    return payload


__all__ = ["decode_cursor", "encode_cursor", "filters_hash"]
