"""``push_profile``: upload a locally built profile directory to a server."""

import os
from pathlib import Path
from typing import Any, Optional

from ._http import _server_base, auth_headers


def push_profile(
    base_url: str,
    profile_dir: str | os.PathLike,
    *,
    slug: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 120.0,
    client: Any = None,
    include_fulltext: bool = False,
) -> dict:
    """Push a locally built profile directory to a remote server.

    Builds the archive via
    :func:`researcher_profiles.api.upload.build_profile_archive`, the same
    spec-whitelist builder the server's GET serve uses, so push and serve
    share one exclusion implementation. The archive can never carry dotfiles
    or non-spec files such as raw ``sources/html/`` scrape output.

    ``include_fulltext`` defaults to ``False``: the extracted
    ``sources/papers/`` text is a derived copy of publisher-copyrighted works,
    and a push is the moment it would leave this machine. Set it to ``True``
    only for a destination you know is entitled to the fulltext, such as a
    private backup or your own registry running with
    ``accept_fulltext=True``. This flag is a client-side courtesy only: the
    receiving server strips fulltext on ingest unless its own policy admits
    it, so opting in here does not by itself get the text accepted.

    PUTs the archive to ``/api/v1/profiles/{slug}`` and returns the parsed
    summary (``{slug, name, level, indexed}``). ``slug`` defaults to the
    directory name; ``token`` falls back to the ``RESEARCHER_PROFILES_TOKEN``
    env var.
    """
    import httpx

    from ..api.upload import build_profile_archive

    src = Path(profile_dir).expanduser().resolve()
    if not (src / "profile.jsonld").is_file():
        raise FileNotFoundError(f"not a profile directory (no profile.jsonld): {src}")
    slug = slug or src.name
    base_url = _server_base(base_url)
    if token is None:
        token = os.environ.get("RESEARCHER_PROFILES_TOKEN") or None

    data = build_profile_archive(src, include_fulltext=include_fulltext)

    http = (
        client
        if client is not None
        else httpx.Client(base_url=base_url, headers=auth_headers(token), timeout=timeout)
    )
    try:
        resp = http.put(
            f"/api/v1/profiles/{slug}",
            content=data,
            headers={"Content-Type": "application/gzip"},
        )
        if resp.status_code == 401:
            raise PermissionError(resp.text)
        if resp.status_code >= 400:
            raise RuntimeError(f"push failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()
    finally:
        if client is None:
            http.close()
