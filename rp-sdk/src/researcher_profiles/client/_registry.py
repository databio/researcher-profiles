"""Registries and the local profile cache: list, rank, install, seek.

A registry is any server speaking the ``researcher_profiles.api`` contract.
``$RESEARCHER_PROFILES_REGISTRY_URL`` names one or more of them,
comma-separated, tried in order. The local cache is the profiles root
(:func:`researcher_profiles.store.config.resolve_profiles_root`), one directory per
installed profile, and the filesystem is its source of truth: a profile is
installed when ``<root>/<slug>/profile.jsonld`` exists.
"""

import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from ..env import check_retired_env_vars
from ..errors import ProfileError
from ..profile import ResearcherProfile
from ..store.config import PROFILES_ROOT_ENV_VAR, resolve_profiles_root
from ._http import _server_base, _split_profile_url, auth_headers

logger = logging.getLogger(__name__)

#: Env var naming the local profiles root. Shared with anything that writes
#: profiles into the cache, so a written profile and an installed one land in
#: the same tree. Re-exported from :mod:`researcher_profiles.store.config`
#: under this package's own name, not a second literal for the same setting.
CACHE_ENV_VAR = PROFILES_ROOT_ENV_VAR

#: Env var naming the default registry base URL(s), comma-separated.
REGISTRY_ENV_VAR = "RESEARCHER_PROFILES_REGISTRY_URL"


def resolve_registries(url: Optional[str] = None) -> list[str]:
    """Resolve the registry server list (comma-separated), tried in order."""
    check_retired_env_vars()
    raw = url or os.environ.get(REGISTRY_ENV_VAR) or ""
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


# ---------------------------------------------------------------------------
# Listing and ranking
# ---------------------------------------------------------------------------


def _fetch_profiles(
    base_url: str,
    *,
    token: Optional[str],
    timeout: float,
    client: Any = None,
) -> list[dict]:
    """GET ``/api/v1/profiles`` from one server and return the parsed list.

    Behind ``ResearcherProfile.list_remote``. Raises on any transport or HTTP
    failure; ``PermissionError`` on 401, so a private registry is
    distinguishable from an unreachable one. An injected ``client`` is used
    as-is and never closed. It belongs to the caller.
    """
    import httpx

    http = (
        client
        if client is not None
        else httpx.Client(
            base_url=base_url,
            headers=auth_headers(token),
            timeout=timeout,
            follow_redirects=True,
        )
    )
    try:
        resp = http.get("/api/v1/profiles")
        if resp.status_code == 401:
            raise PermissionError(resp.text)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "profiles" in data:
            return data["profiles"]
        return data
    finally:
        if client is None:
            http.close()


def rank_against(
    base_url: str,
    query: str,
    *,
    k: int = 5,
    token: Optional[str] = None,
    prefilter: int = 10,
    require_topics: Optional[list[str]] = None,
    diversify: bool = True,
    lambda_: float = 0.5,
    topk_chunks: int = 5,
    normalize: bool = True,
    include_chunks: bool = False,
    timeout: float = 60.0,
    client: Any = None,
) -> list[dict]:
    """Rank indexed profiles on a remote server against ``query``.

    Registry ranking is not profile-scoped, so this is a module-level function
    (not an :class:`ApiArtifactStorage` method). POSTs to ``/api/v1/match`` and
    returns the parsed ``matches`` list (plain dicts shaped like ``MatchResult``:
    ``{slug, name, orcid, score, evidence{...}}``).
    """
    import httpx

    base_url, _ = _split_profile_url(base_url)
    if token is None:
        token = os.environ.get("RESEARCHER_PROFILES_TOKEN") or None
    payload = {
        "query": query,
        "k": k,
        "prefilter": prefilter,
        "require_topics": require_topics,
        "diversify": diversify,
        "lambda_": lambda_,
        "topk_chunks": topk_chunks,
        "normalize": normalize,
        "include_chunks": include_chunks,
    }
    http = (
        client
        if client is not None
        else httpx.Client(base_url=base_url, headers=auth_headers(token), timeout=timeout)
    )
    try:
        resp = http.post("/api/v1/match", json=payload)
        if resp.status_code == 401:
            raise PermissionError(resp.text)
        resp.raise_for_status()
        data = resp.json()
        return list(data.get("matches", []))
    finally:
        if client is None:
            http.close()


@dataclass(frozen=True)
class RegistryListing:
    """One registry's answer to "what profiles do you hold?".

    ``error`` is the failure text when the request did not complete, and
    ``profiles`` is empty in that case. A reachable registry holding nothing
    is ``profiles=()`` with ``error=None``. The two must stay distinguishable,
    because `rp listr` exits 1 for the first and 0 for the second.
    """

    base_url: str
    profiles: tuple[dict, ...] = ()
    error: str | None = None


def list_registry(
    url: Optional[str] = None,
    *,
    token: Optional[str] = None,
    timeout: float = 30.0,
    client: Any = None,
) -> list[RegistryListing]:
    """Ask every configured registry what profiles it holds.

    ``url`` is the comma-separated form `--url` and ``$RESEARCHER_PROFILES_REGISTRY_URL`` carry;
    it is resolved through :func:`resolve_registries`, so an empty return list
    means "no registry is configured" and nothing was attempted. One listing
    comes back per named server, in order, each either carrying its profiles
    or its failure text. A registry that is down does not abort the others
    and does not raise.
    """
    if token is None:
        token = os.environ.get("RESEARCHER_PROFILES_TOKEN") or None
    out: list[RegistryListing] = []
    for server in resolve_registries(url):
        base = _server_base(server)
        try:
            profiles = _fetch_profiles(base, token=token, timeout=timeout, client=client)
        # Boundary: one remote registry. Anything it can fail with is data, not
        # a crash, so it is reported on the listing rather than raised.
        except Exception as e:
            logger.debug("registry %s listing failed", base, exc_info=True)
            out.append(RegistryListing(base_url=base, error=str(e)))
            continue
        out.append(RegistryListing(base_url=base, profiles=tuple(profiles)))
    return out


# ---------------------------------------------------------------------------
# Local cache (install / seek)
# ---------------------------------------------------------------------------


def seek_profile(slug: str, root: Optional[str | os.PathLike] = None) -> Path:
    """Resolve a slug to its local profile directory.

    Raises ``FileNotFoundError`` if the profile is not cached. Mirrors
    ``refgenie seek`` / ``geniml bbclient seek`` so shell pipelines can do
    ``$(rp seek <slug>)``.
    """
    target = resolve_profiles_root(root) / slug
    if not (target / "profile.jsonld").is_file():
        raise FileNotFoundError(f"profile {slug!r} is not installed at {target}")
    return target


def list_installed(root: Optional[str | os.PathLike] = None) -> list[str]:
    """List slugs present in the local cache (filesystem is the source of truth)."""
    base = resolve_profiles_root(root)
    if not base.is_dir():
        return []
    return sorted(
        p.name
        for p in base.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (p / "profile.jsonld").is_file()
    )


class _RegistryMiss(Exception):
    """One registry cannot supply the profile; ``install_profile`` tries the next.

    Distinct from the errors that abort the install outright: a 401 is
    ``PermissionError`` and a corrupt transfer is ``RuntimeError``, and both
    propagate. A miss is only ever recorded and moved past.
    """


def _install_metadata(http: Any, server: str, slug: str, token: Optional[str]) -> dict:
    """Metadata before bytes: a cheap existence and identity check."""
    resp = http.get(f"/api/v1/profiles/{slug}", headers=auth_headers(token))
    if resp.status_code == 401:
        raise PermissionError(f"{server}: {resp.text[:200]}")
    if resp.status_code == 404:
        raise _RegistryMiss(f"{server}: not found")
    if resp.status_code >= 400:
        raise _RegistryMiss(f"{server}: metadata failed ({resp.status_code})")
    return resp.json()


def _download_archive(
    http: Any, server: str, slug: str, token: Optional[str], part: Path
) -> Optional[str]:
    """Stream the archive to ``part``, hashing as we go, and verify the digest.

    Returns the ``X-RP-Archive-Tier`` header: the privacy tier the server
    projected the archive through. A digest mismatch is ``RuntimeError``: the
    bytes are wrong, and trying another registry would not make them right.
    """
    # Transfer-integrity digest only, not a security control; TLS and signing cover tampering.
    md5 = hashlib.md5()
    with http.stream(
        "GET",
        f"/api/v1/profiles/{slug}/archive",
        headers=auth_headers(token),
    ) as resp:
        if resp.status_code >= 400:
            raise _RegistryMiss(f"{server}: archive failed ({resp.status_code})")
        digest_header = resp.headers.get("X-RP-Archive-Digest")
        archive_tier = resp.headers.get("X-RP-Archive-Tier")
        with open(part, "wb") as fh:
            for chunk in resp.iter_bytes():
                md5.update(chunk)
                fh.write(chunk)
    if digest_header and md5.hexdigest() != digest_header:
        raise RuntimeError(
            f"{server}: archive digest mismatch for {slug} "
            f"(got {md5.hexdigest()}, expected {digest_header})"
        )
    return archive_tier


def _extract_and_swap(
    base_root: Path, server: str, slug: str, part: Path
) -> tuple[Optional[str], Optional[str], Path]:
    """Extract to a staging dir, check the profile loads, then atomic swap.

    Returns ``(name, level, final_path)`` read from the staged profile itself.
    The staging dir is removed whatever happens, so a bad archive never leaves
    a half-written profile in the cache.
    """
    from ..api.upload import UploadError, extract_profile_archive
    from ..store.files import swap_profile_dir

    staging = base_root / f".install-{slug}-{uuid.uuid4().hex}"
    try:
        try:
            extract_profile_archive(part.read_bytes(), staging)
        except UploadError as e:
            raise RuntimeError(f"{server}: bad archive: {e}") from e
        # Loading is lazy, so read the fields here, while the files exist.
        try:
            staged = ResearcherProfile.from_files(staging)
            name = staged.metadata.name
            level = staged.level
        except (OSError, ValueError, ProfileError, ValidationError) as e:
            raise RuntimeError(f"{server}: downloaded profile failed to load: {e}") from e
        final = swap_profile_dir(base_root, slug, staging)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return name, level, final


def install_profile(
    slug: str,
    *,
    url: Optional[str] = None,
    root: Optional[str | os.PathLike] = None,
    token: Optional[str] = None,
    force: bool = False,
    timeout: float = 300.0,
    client: Any = None,
) -> dict:
    """Pull a profile from a registry and cache it in the local profiles root.

    The inverse of :func:`push_profile`. Flow follows refgenie's ``pull``:
    check the local cache first, fetch metadata before bytes, stream the
    archive to a ``.part`` file while hashing, verify the server's digest,
    then extract to a staging dir and atomically swap it into place. A failed
    or interrupted transfer never leaves a half-written profile in the cache.

    Returns a summary dict with ``slug``, ``name``, ``level``, ``path``,
    ``status`` (``"installed"`` or ``"present"``), and ``archive_tier``: the
    privacy tier the server projected the archive through for this caller
    (``X-RP-Archive-Tier``). An anonymous install gets ``public``; a credential
    entitled to more gets more.
    """
    import httpx

    from ..api.upload import SLUG_RE

    if not SLUG_RE.match(slug):
        raise ValueError(f"invalid slug {slug!r} (expected ^[a-z0-9][a-z0-9-]*$)")

    base_root = resolve_profiles_root(root)
    target = base_root / slug

    # Cache-hit oracle is the filesystem, checked before any bytes move.
    if (target / "profile.jsonld").is_file() and not force:
        return {
            "slug": slug,
            "name": None,
            "level": None,
            "path": str(target),
            "status": "present",
            "archive_tier": None,
        }

    servers = resolve_registries(url)
    if not servers:
        raise ValueError(f"no registry URL given (pass url= or set {REGISTRY_ENV_VAR})")
    if token is None:
        token = os.environ.get("RESEARCHER_PROFILES_TOKEN") or None

    base_root.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    for server in servers:
        server = _server_base(server)
        http = (
            client
            if client is not None
            else httpx.Client(base_url=server, timeout=timeout, follow_redirects=True)
        )
        part = base_root / f".download-{slug}-{uuid.uuid4().hex}.part"
        try:
            _install_metadata(http, server, slug, token)
            archive_tier = _download_archive(http, server, slug, token, part)
            name, level, final = _extract_and_swap(base_root, server, slug, part)
        except _RegistryMiss as e:
            errors.append(str(e))
            continue
        finally:
            if part.exists():
                part.unlink()
            if client is None:
                http.close()

        return {
            "slug": slug,
            "name": name,
            "level": level,
            "path": str(final),
            "status": "installed",
            "archive_tier": archive_tier,
        }

    raise RuntimeError(f"could not install {slug!r} from any registry: " + "; ".join(errors))
