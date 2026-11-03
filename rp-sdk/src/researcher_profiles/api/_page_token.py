"""Opaque page tokens, bound to the query that made them.

A ``page_token`` carries where the last page stopped plus a hash of the filters it was
made under. Reusing it with different filters is a 400 ``page_token_mismatch``,
not a page of some other query's results. A token can be decoded by anyone,
so it carries no authority: every page re-runs the route's gates.

A keyset token, ``{"k": [year, paper_id], "q": hash}``, does not skip or
repeat rows when the corpus changes between pages. ``page_size`` is not part
of the hash, so a caller may change it between pages.
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


def encode_token(position: dict[str, Any], filters: dict[str, Any]) -> str:
    """``position`` is ``{"k": [...]}``."""
    payload = {**position, "q": filters_hash(filters)}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _bad(message: str) -> Invalid:
    return Invalid(message, code="page_token_mismatch", hint="start again without a page_token")


def decode_token(token: str, filters: dict[str, Any]) -> dict[str, Any]:
    """The position a token carries; ``Invalid(code="page_token_mismatch")`` if not this query's."""
    try:
        pad = "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(token + pad))
    except (ValueError, TypeError) as e:
        raise _bad("this page_token is not valid") from e
    if not isinstance(payload, dict) or payload.get("q") != filters_hash(filters):
        raise _bad("this page_token does not match this query and these filters")
    return payload


__all__ = ["decode_token", "encode_token", "filters_hash"]
