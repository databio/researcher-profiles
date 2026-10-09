"""``push_profile``: upload a locally built profile directory to a server.

A push is a preflight and a PUT, in that order. The preflight builds the
archive and diffs it against what the server holds (one
``GET /api/v1/profiles/{slug}``, whose manifest carries ``sha256`` at every
tier). A push that would delete server-side files refuses unless forced.
"""

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ._http import _server_base, auth_headers

#: The diff groups a plan carries, in the order a reader wants them.
#: ``respliced`` is a subset of ``kept``, not a separate group.
PLAN_GROUPS = ("added", "changed", "unchanged", "removed", "kept", "respliced")

#: The capability name a server advertises when it splices a kept file's
#: manifest entry back into the committed document. Without it, an entry the
#: local manifest dropped is deleted no matter how many files the server keeps.
SPLICE_FEATURE = "manifest_splice"

#: Role for an archived file neither manifest names (``rp manifest --write``
#: has not run).
UNKNOWN_ROLE = "other"


class PushRefused(Exception):
    """A preflight check (or the server) said no. Nothing was written.

    ``str(e)`` is one line naming what is wrong, with the numbers. ``code`` is
    a stable slug for the case; ``fix`` is the next step, in ``rp push`` flags;
    ``details`` are extra lines (paths, parts) a reader may want.
    """

    code = "refused"

    def __init__(self, message: str, *, code: str = "", fix: str = "", details=()):
        super().__init__(message)
        self.code = code or type(self).code
        self.fix = fix
        self.details: list[str] = list(details)


#: How many paths a refusal lists before it summarizes the rest.
_DETAIL_CAP = 20


def _capped(paths: Sequence[str]) -> list[str]:
    more = len(paths) - _DETAIL_CAP
    return list(paths[:_DETAIL_CAP]) + ([f"... and {more} more"] if more > 0 else [])


class PushWouldRemove(PushRefused):
    """This push deletes files the server holds. Carries the plan that says which."""

    def __init__(self, plan: "PushPlan"):
        self.plan = plan
        removed = plan.paths("removed")
        shrink = f"manifest {plan.manifest_before} -> {plan.manifest_after} entries"
        if removed:
            super().__init__(
                f"{len(removed)} server file(s) are not in this push and would be deleted "
                f"({shrink})",
                code="would-remove",
                fix="--merge keeps them, --force deletes them, "
                "--prune also drops kept fulltext/index",
                details=_capped(removed),
            )
        else:
            super().__init__(
                f"local manifest is smaller than the server's ({shrink}); likely a partial copy",
                code="manifest-shrink",
                fix=f"`rp where {plan.slug}` and push from the directory that built it, "
                "or --force to drop the entries",
            )


class PushInsufficientAccess(PushRefused):
    """The server refused the push part by part (403 ``insufficient_access``).

    An app or API key without the "Replace whole profiles" switch may push only
    when every part the push changes is Write in its table. ``missing`` names
    parts the key needs Write on; ``needs_replace`` names changes no part
    covers, which only the switch allows. ``hint`` is the server's own text.
    """

    code = "insufficient-access"

    def __init__(self, body: dict, *, only: bool = False):
        self.required: list[str] = list(body.get("required") or [])
        self.missing: list[str] = list(body.get("missing") or [])
        self.needs_replace: list[str] = list(body.get("needs_replace") or [])
        self.hint: str = body.get("hint") or ""
        details = []
        if self.missing:
            details.append(f"needs Write on: {', '.join(self.missing)}")
        if self.needs_replace:
            parts = ", ".join(self.needs_replace)
            details.append(f'needs "Replace whole profiles" for: {parts}')
        if not details and self.hint:
            details.append(self.hint)
        if only:
            fix = (
                "profile.jsonld travels with --only, so its sections must match the "
                "server's; or have the account holder widen this key on the Privacy page"
            )
        else:
            fix = (
                "--only <file> to send only parts you may write, or have the account "
                "holder widen this key on the Privacy page"
            )
        super().__init__(
            "this key may not change every part this push touches", fix=fix, details=details
        )


def _insufficient_access(resp: Any) -> Optional[dict]:
    """The ``insufficient_access`` body of a 403, or ``None`` for any other answer."""
    if resp.status_code != 403:
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and detail.get("error") == "insufficient_access":
        return detail
    return None


@dataclass(frozen=True)
class PushPlan:
    """What a push would do to the server's copy, computed before it runs.

    Every group maps a manifest ``role`` to the profile-relative paths in it.
    ``removed`` is what this push deletes; ``kept`` is what the server holds on
    to anyway, which depends on the push mode (a withheld class under
    ``replace``, everything under ``merge``). ``dropped`` is files on disk that
    never entered the tarball.

    ``respliced`` is the subset of ``kept`` whose entries the local manifest
    no longer lists. The server adds the entry back, but only if it advertises
    ``manifest_splice``. Otherwise they count as ``removed``, because a kept
    file with no manifest entry is a file no reader can fetch.

    ``manifest_before`` / ``manifest_after`` are entry counts now and after the
    push. A shrinking manifest is a removal even when ``removed`` is empty.

    ``profile.jsonld`` is in no group: it is the manifest itself and always
    travels.
    """

    slug: str
    #: Whether the server already holds this profile. ``False`` makes every
    #: local file ``added`` and is the only state in which nothing can be lost.
    exists: bool
    added: dict[str, list[str]] = field(default_factory=dict)
    changed: dict[str, list[str]] = field(default_factory=dict)
    unchanged: dict[str, list[str]] = field(default_factory=dict)
    removed: dict[str, list[str]] = field(default_factory=dict)
    kept: dict[str, list[str]] = field(default_factory=dict)
    respliced: dict[str, list[str]] = field(default_factory=dict)
    dropped: dict[str, list[str]] = field(default_factory=dict)
    #: Manifest entry counts: the server's now, and the predicted post-commit one.
    manifest_before: int = 0
    manifest_after: int = 0
    #: Local manifest entries with no file on disk (``cache/`` excluded).
    local_stale: list[str] = field(default_factory=list)

    @property
    def shrinks(self) -> bool:
        """Whether this push would leave the server indexing fewer artifacts."""
        return self.manifest_after < self.manifest_before

    def counts(self) -> dict[str, int]:
        """``{group: file count}`` for every group in :data:`PLAN_GROUPS` (``respliced`` overlaps ``kept``)."""
        return {g: sum(len(paths) for paths in getattr(self, g).values()) for g in PLAN_GROUPS}

    def paths(self, group: str) -> list[str]:
        """One group's paths, flattened out of their roles and sorted."""
        return sorted(p for paths in getattr(self, group).values() for p in paths)

    def as_dict(self) -> dict:
        """The plan as JSON-able data, for ``rp push --dry-run --json``."""
        return {
            "slug": self.slug,
            "exists": self.exists,
            "counts": self.counts(),
            **{g: getattr(self, g) for g in PLAN_GROUPS},
            "dropped": self.dropped,
            "manifest_before": self.manifest_before,
            "manifest_after": self.manifest_after,
            "local_stale": self.local_stale,
        }


@dataclass(frozen=True)
class PushResult:
    """The outcome of :func:`push_profile`: what it planned, and what happened.

    ``mode`` is the :data:`~researcher_profiles.api.upload.PushMode` the plan
    was computed under and the PUT was sent with. ``summary`` is the server's
    ``PushResponse`` as parsed JSON, or ``None`` for a dry run, which stops
    after the plan.
    """

    plan: PushPlan
    mode: str = "replace"
    summary: Optional[dict] = None


def _sha256(path: Path) -> str:
    """The digest of a file, streamed: an index can run to tens of megabytes."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _remote_manifest(http: Any, slug: str) -> tuple[bool, dict[str, dict]]:
    """``(exists, {contentUrl: manifest entry})`` for the server's copy.

    A 404 is the create case. Any other error raises: pushing without knowing
    what the server holds could silently delete a profile.
    """
    resp = http.get(f"/api/v1/profiles/{slug}/files")
    if resp.status_code == 404:
        return False, {}
    if resp.status_code == 401:
        raise PermissionError(resp.text)
    if resp.status_code >= 400:
        raise RuntimeError(f"push preflight failed ({resp.status_code}): {resp.text[:300]}")
    entries = resp.json().get("files") or []
    return True, {
        e["contentUrl"]: e for e in entries if isinstance(e, dict) and e.get("contentUrl")
    }


def _server_capabilities(http: Any) -> dict:
    """What the server says it supports, or ``{}`` when it will not say.

    ``{}`` covers a server without ``GET /api/v1/capabilities`` (a 404) and a
    failed transport. Never raises.
    """
    try:
        resp = http.get("/api/v1/capabilities")
    # Boundary: any transport failure here is answered by the real requests
    # that follow, which do raise.
    except Exception:  # noqa: BLE001
        return {}
    if getattr(resp, "status_code", 500) >= 400:
        return {}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _local_manifest_entries(src: Path) -> dict[str, dict]:
    """``contentUrl -> entry`` from the profile's own ``profile.jsonld`` manifest.

    This manifest, not the directory, becomes the server's index on commit.
    """
    from ..api.upload import PROFILE_DOCUMENT

    try:
        doc = json.loads((src / PROFILE_DOCUMENT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = [*(doc.get("hasPart") or []), *(doc.get("subjectOf") or [])]
    return {e["contentUrl"]: e for e in entries if isinstance(e, dict) and e.get("contentUrl")}


def _local_stale_entries(src: Path) -> list[str]:
    """Local manifest entries naming a file the directory does not have.

    The sign of a partial copy. ``cache/`` entries are excluded because
    ``push_profile`` refuses on them separately.
    """
    from ..schema.manifest import manifest_drift
    from ..utils.paths import LEGACY_CACHE_DIRNAME

    try:
        from ..schema import ArtifactRef

        recorded = [
            ArtifactRef.model_validate({k: v for k, v in e.items() if k != "effective_visibility"})
            for e in _local_manifest_entries(src).values()
        ]
        drift = manifest_drift(src, recorded)
    # Boundary: an unreadable or malformed manifest is somebody else's error
    # message (``rp validate``); it must not turn a push into a traceback.
    except Exception:  # noqa: BLE001
        return []
    prefix = f"{LEGACY_CACHE_DIRNAME}/"
    return [rel for rel in drift["stale"] if not rel.startswith(prefix)]


def _by_role(paths: Sequence[str], roles: dict[str, str]) -> dict[str, list[str]]:
    """Group profile-relative paths by manifest role, each list sorted."""
    out: dict[str, list[str]] = {}
    for p in sorted(paths):
        out.setdefault(roles.get(p, UNKNOWN_ROLE), []).append(p)
    return out


def _plan_push(
    slug: str,
    src: Path,
    archive: Any,
    *,
    mode: str,
    exists: bool,
    remote: dict[str, dict],
    only: Optional[Sequence[str]] = None,
    splices: bool = True,
) -> PushPlan:
    """Diff the server's manifest against the manifest this push would commit.

    Not against the archive's members: the manifest is what the server
    commits and what a reader can fetch through.

    Local digests are computed from the files, not read from
    ``profile.jsonld``, because the recorded manifest can be stale. A remote
    entry with no digest counts as changed.

    The keep rule mirrors ``upload._keep_omitted`` exactly, mode for mode:
    ``merge`` keeps everything the archive omits, ``prune`` keeps none of it,
    and ``replace`` keeps a withheld class the archive carries no member of
    (one member makes the archive authoritative for the class).

    ``splices`` is the server's ``manifest_splice`` capability. With it, a kept
    file whose entry the local manifest dropped comes back (``respliced``).
    Without it, the entry is gone and the file with it, so the same path is
    counted as ``removed``.
    """
    from ..api.upload import PROFILE_DOCUMENT, WITHHELD_CLASSES

    roles = {url: (e.get("role") or UNKNOWN_ROLE) for url, e in remote.items()}
    local_manifest = _local_manifest_entries(src)
    roles.update({url: (e.get("role") or UNKNOWN_ROLE) for url, e in local_manifest.items()})

    local = {rel: _sha256(src / rel) for rel in archive.members if rel != PROFILE_DOCUMENT}
    added, changed, unchanged = [], [], []
    for rel, digest in local.items():
        entry = remote.get(rel)
        if entry is None:
            added.append(rel)
        elif entry.get("sha256") == digest:
            unchanged.append(rel)
        else:
            changed.append(rel)

    # With ``only``, the archive carries the server's manifest
    # (``upload._project_only_manifest``); otherwise the local one.
    staged_urls = set(remote) if only else set(local_manifest)

    authoritative = {
        cls for cls, match in WITHHELD_CLASSES.items() if any(match(n) for n in archive.members)
    }
    removed, kept, respliced = [], [], []
    for rel in remote:
        if rel in local or rel == PROFILE_DOCUMENT:
            continue
        if mode == "merge":
            survives = True
        elif mode == "prune":
            survives = False
        else:
            cls = next((c for c, match in WITHHELD_CLASSES.items() if match(rel)), None)
            survives = cls is not None and cls not in authoritative
        if not survives:
            removed.append(rel)
            continue
        if rel in staged_urls:
            kept.append(rel)
            continue
        # The bytes are kept, but the manifest this push commits does not list
        # them. Only a server that splices will put the entry back.
        respliced.append(rel)
        (kept if splices else removed).append(rel)

    committed = staged_urls | set(kept) | set(local)

    return PushPlan(
        slug=slug,
        exists=exists,
        added=_by_role(added, roles),
        changed=_by_role(changed, roles),
        unchanged=_by_role(unchanged, roles),
        removed=_by_role(removed, roles),
        kept=_by_role(kept, roles),
        respliced=_by_role(respliced, roles),
        dropped=archive.dropped,
        manifest_before=len(remote),
        manifest_after=len(committed),
        local_stale=_local_stale_entries(src),
    )


def push_profile(
    base_url: str,
    profile_dir: str | os.PathLike,
    *,
    slug: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 120.0,
    client: Any = None,
    include_fulltext: bool = False,
    mode: Optional[str] = None,
    only: Optional[Sequence[str]] = None,
    force: bool = False,
    dry_run: bool = False,
) -> PushResult:
    """Push a locally built profile directory to a remote server.

    Builds the archive with
    :func:`researcher_profiles.api.upload.build_profile_archive`, the same
    spec-whitelist builder the server uses, so it never carries dotfiles or
    non-spec files.

    ``include_fulltext`` defaults to ``False``: ``sources/papers/`` text is a
    copy of publisher-copyrighted works. Set it only for a destination entitled
    to it (a private backup, a registry with ``accept_fulltext=True``). The
    server still strips fulltext on ingest unless its own policy admits it.

    ``mode`` is the :data:`~researcher_profiles.api.upload.PushMode` sent as
    ``?mode=``: what the server does with the live files this archive does not
    carry. ``replace`` (the default) keeps only a withheld class the archive
    carried none of, so a full build push replaces the record; ``merge`` keeps
    every omitted file; ``prune`` keeps none. Passing ``only`` without a mode
    selects ``merge``, so naming one file never deletes the rest.

    ``only`` narrows the archive to ``profile.jsonld`` plus the named
    profile-relative paths, and takes the manifest from the server rather than
    from disk. Inline sections (name, expertise, affiliations) still come from
    the local document; to change one field only, use ``rp work patch``.

    The preflight builds a :class:`PushPlan`. A push that would remove files,
    or shrink the server's manifest, raises :class:`PushWouldRemove` unless
    ``force=True``. A profile with a ``cache/`` directory, or whose manifest
    names files the directory lacks, raises :class:`PushRefused`. A 403
    ``insufficient_access`` raises :class:`PushInsufficientAccess`. In all of
    these the server is untouched. ``dry_run=True`` returns the plan instead
    of raising.

    Returns a :class:`PushResult`: the plan, the mode, and the server's parsed
    summary (``{slug, name, level, indexed, kept, spliced, manifest_counts,
    mode}``), or ``None`` for a dry run. ``slug`` defaults to the directory
    name; ``token`` falls back to the ``RESEARCHER_PROFILES_TOKEN`` env var.
    """
    import httpx

    from ..api.upload import PROFILE_DOCUMENT, build_profile_archive
    from ..utils.paths import CACHE_DIRNAME, LEGACY_CACHE_DIRNAME

    src = Path(profile_dir).expanduser().resolve()
    if not (src / PROFILE_DOCUMENT).is_file():
        raise FileNotFoundError(f"not a profile directory (no {PROFILE_DOCUMENT}): {src}")
    slug = slug or src.name
    mode = mode or ("merge" if only else "replace")
    base_url = _server_base(base_url)
    if token is None:
        token = os.environ.get("RESEARCHER_PROFILES_TOKEN") or None

    archive = build_profile_archive(src, include_fulltext=include_fulltext, only=only)
    legacy = archive.dropped.get("legacy_cache") or []
    if legacy:
        raise PushRefused(
            f"{len(legacy)} file(s) under the retired {LEGACY_CACHE_DIRNAME}/ would not ship",
            code="legacy-cache",
            fix=f"move them to {CACHE_DIRNAME}/, then `rp manifest --write`",
            details=_capped(legacy),
        )

    # Stale entries mean a partial copy, which would re-index the server from
    # an incomplete directory. With ``only`` the local manifest never travels.
    stale = _local_stale_entries(src)
    if stale and not only:
        raise PushRefused(
            f"{len(stale)} manifest entry(ies) name files missing here; "
            "this copy is not the whole profile",
            code="partial-copy",
            fix=f"`rp where {slug}` and push from the directory that built it, "
            "or --only <path> to send only the named files",
            details=_capped(stale),
        )

    http = (
        client
        if client is not None
        else httpx.Client(base_url=base_url, headers=auth_headers(token), timeout=timeout)
    )
    try:
        caps = _server_capabilities(http)
        advertised = caps.get("push_modes") or []
        # A server without ``?mode=`` support always replaces, so asking it for
        # ``merge`` or ``prune`` would answer 200 and do a replace.
        if advertised and mode not in advertised:
            raise PushRefused(
                f"server does not support mode={mode} (offers: {', '.join(advertised)})",
                code="mode-unsupported",
                fix="drop --merge/--prune/--only and push a full build, or upgrade the server",
            )
        if not advertised and mode != "replace":
            raise PushRefused(
                f"server does not advertise push modes; mode={mode} may silently run a replace",
                code="mode-unknown",
                fix="drop --merge/--prune/--only and push a full build, or upgrade the server",
            )
        splices = SPLICE_FEATURE in (caps.get("features") or [])

        exists, remote = _remote_manifest(http, slug)
        if only and exists:
            # Rebuild with the server's manifest inside profile.jsonld, so a
            # local index that has fallen behind cannot shrink the server's.
            archive = build_profile_archive(
                src,
                include_fulltext=include_fulltext,
                only=only,
                only_manifest_from_remote=remote,
            )
        plan = _plan_push(
            slug,
            src,
            archive,
            mode=mode,
            exists=exists,
            remote=remote,
            only=only,
            splices=splices,
        )
        if dry_run:
            return PushResult(plan=plan, mode=mode)
        if (plan.removed or plan.shrinks) and not force:
            raise PushWouldRemove(plan)
        resp = http.put(
            f"/api/v1/profiles/{slug}?mode={mode}",
            content=archive.data,
            headers={"Content-Type": "application/gzip"},
        )
        if resp.status_code == 401:
            raise PermissionError(resp.text)
        denied = _insufficient_access(resp)
        if denied is not None:
            raise PushInsufficientAccess(denied, only=bool(only))
        if resp.status_code >= 400:
            raise RuntimeError(f"push failed ({resp.status_code}): {resp.text[:300]}")
        return PushResult(plan=plan, mode=mode, summary=resp.json())
    finally:
        if client is None:
            http.close()
