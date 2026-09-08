"""``resolve_rid``: the consumer side of the identity authority."""

import os
from typing import Any, Optional

from ..resolve import Candidate, ResolveResult
from ._http import _compact, _server_base, auth_headers


def resolve_rid(
    base_url: str,
    *,
    rid: Optional[str] = None,
    name: Optional[str] = None,
    affiliation: Optional[str] = None,
    create_new: bool = False,
    token: Optional[str] = None,
    timeout: float = 30.0,
    client: Any = None,
) -> ResolveResult:
    """Resolve a person descriptor to a ``rid`` against the identity authority.

    The consumer-side half of ``POST /api/v1/identity/resolve``: a service
    holding a free-text name or a bare ORCID calls this at its boundary and
    stores the returned ``rid`` (as a scholarcore ``PersonRef``), instead of
    hand-rolling HTTP or minting its own local id, which is worse. Resolution
    is authoritative and idempotent server-side: the same person resolved
    from two services gets the same ``rid``.

    ``rid`` is an ORCID or a ``local:`` id the resolver minted. ``token`` must
    carry the ``resolve`` scope (falls back to the
    ``RESEARCHER_PROFILES_TOKEN`` env var). A ``rid`` of ``None`` on the
    result means the name was undecidable. Inspect ``candidates`` and
    re-call with the chosen profile's ``rid``, or with ``create_new=True``
    when none of the candidates is your person (the server mints a fresh
    identity; never guess between candidates).
    """
    import httpx

    base_url = _server_base(base_url)
    if token is None:
        token = os.environ.get("RESEARCHER_PROFILES_TOKEN") or None

    body = _compact(rid=rid, name=name, affiliation=affiliation)
    if create_new:
        body["create_new"] = True
    http = (
        client
        if client is not None
        else httpx.Client(base_url=base_url, headers=auth_headers(token), timeout=timeout)
    )
    try:
        resp = http.post("/api/v1/identity/resolve", json=body)
        if resp.status_code in (401, 403):
            raise PermissionError(resp.text)
        if resp.status_code >= 400:
            raise RuntimeError(f"resolve failed ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
    finally:
        if client is None:
            http.close()
    return ResolveResult(
        rid=data.get("rid"),
        created=bool(data.get("created")),
        confidence=str(data.get("confidence", "")),
        candidates=tuple(
            Candidate(rid=c["rid"], name=c.get("name", ""), affiliation=c.get("affiliation"))
            for c in data.get("candidates", [])
        ),
    )
