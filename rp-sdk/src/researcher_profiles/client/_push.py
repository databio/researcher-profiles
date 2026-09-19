"""``push_profile``: upload a locally built profile directory to a server.

A push is a preflight and a PUT, in that order. The preflight builds the
archive, asks the server what it already holds, and answers the three
questions a user has before bytes move: what changes, what disappears, and
what on disk did not ship at all. Nothing is uploaded until they are answered,
and a push that would delete server-side files refuses unless told otherwise.

The diff costs one ``GET /api/v1/profiles/{slug}``: the response carries the
whole manifest (``contentUrl``, ``role``, ``sha256``) at every tier, so no new
server surface is needed to compare a directory against a registry.
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
PLAN_GROUPS = ("added", "changed", "unchanged", "removed", "kept")

#: Role for an artifact neither manifest names. A file can be on disk and in
#: the archive without being in the manifest yet (``rp manifest --write`` has
#: not run); it still has to land in some group.
UNKNOWN_ROLE = "other"


class PushRefused(Exception):
    """A preflight check said no. Nothing was sent."""


class PushWouldRemove(PushRefused):
    """This push deletes files the server holds. Carries the plan that says which."""

    def __init__(self, plan: "PushPlan"):
        self.plan = plan
        n = sum(len(paths) for paths in plan.removed.values())
        super().__init__(f"{n} file(s) the server holds are not in this push")


@dataclass(frozen=True)
class PushPlan:
    """What a push would do to the server's copy, computed before it runs.

    Every group maps a manifest ``role`` to the profile-relative paths in it,
    because "3 paper_summary, 1 works" is a sentence a reader can act on and
    four bare filenames are not. ``removed`` is what this push deletes;
    ``kept`` is what the server holds on to anyway, which depends on the push
    mode (a withheld class under ``replace``, everything under ``merge``).
    ``dropped`` is the archive builder's account of files on disk that never
    entered the tarball.

    ``profile.jsonld`` is in no group: it is the manifest rather than an entry
    in it, and it always travels.
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
    dropped: dict[str, list[str]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        """``{group: file count}`` for every group in :data:`PLAN_GROUPS`."""
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

    A 404 is the create case, not a failure. Any other error is: pushing
    blind, having failed to learn what the server holds, is how a push
    silently deletes a profile.
    """
    resp = http.get(f"/api/v1/profiles/{slug}")
    if resp.status_code == 404:
        return False, {}
    if resp.status_code == 401:
        raise PermissionError(resp.text)
    if resp.status_code >= 400:
        raise RuntimeError(f"push preflight failed ({resp.status_code}): {resp.text[:300]}")
    entries = resp.json().get("manifest") or []
    return True, {
        e["contentUrl"]: e for e in entries if isinstance(e, dict) and e.get("contentUrl")
    }


def _local_roles(src: Path) -> dict[str, str]:
    """``contentUrl -> role`` from the profile's own manifest.

    Roles only. The recorded digests are deliberately not read: a manifest
    written before the last edit would report a changed file as unchanged,
    which is the one answer a preflight must never give. A role that is a
    little stale only mislabels a group heading.
    """
    from ..api.upload import PROFILE_DOCUMENT

    try:
        doc = json.loads((src / PROFILE_DOCUMENT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = [*(doc.get("hasPart") or []), *(doc.get("subjectOf") or [])]
    return {
        e["contentUrl"]: e.get("role") or UNKNOWN_ROLE
        for e in entries
        if isinstance(e, dict) and e.get("contentUrl")
    }


def _by_role(paths: Sequence[str], roles: dict[str, str]) -> dict[str, list[str]]:
    """Group profile-relative paths by manifest role, each list sorted."""
    out: dict[str, list[str]] = {}
    for p in sorted(paths):
        out.setdefault(roles.get(p, UNKNOWN_ROLE), []).append(p)
    return out


def _plan_push(http: Any, slug: str, src: Path, archive: Any, *, mode: str) -> PushPlan:
    """Diff the archive against the server's manifest.

    Local digests are computed from the files going into the tarball rather
    than read out of ``profile.jsonld``: the recorded manifest can be stale,
    and a stale digest turns a real change into "unchanged". A remote entry
    with no recorded digest counts as changed for the same reason -- there is
    no evidence it is the same file.

    The keep rule mirrors ``upload._keep_omitted`` exactly, mode for mode:
    ``merge`` keeps everything the archive omits, ``prune`` keeps none of it,
    and ``replace`` keeps a withheld class the archive carries no member of
    (one member makes the archive authoritative for the class).
    """
    from ..api.upload import PROFILE_DOCUMENT, WITHHELD_CLASSES

    exists, remote = _remote_manifest(http, slug)
    roles = {url: (e.get("role") or UNKNOWN_ROLE) for url, e in remote.items()}
    roles.update(_local_roles(src))

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

    authoritative = {
        cls for cls, match in WITHHELD_CLASSES.items() if any(match(n) for n in archive.members)
    }
    removed, kept = [], []
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
        (kept if survives else removed).append(rel)

    return PushPlan(
        slug=slug,
        exists=exists,
        added=_by_role(added, roles),
        changed=_by_role(changed, roles),
        unchanged=_by_role(unchanged, roles),
        removed=_by_role(removed, roles),
        kept=_by_role(kept, roles),
        dropped=archive.dropped,
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

    ``mode`` is the :data:`~researcher_profiles.api.upload.PushMode` sent as
    ``?mode=``: what the server does with the live files this archive does not
    carry. ``replace`` (the default) keeps only a withheld class the archive
    carried none of, so a full build push replaces the record; ``merge`` keeps
    every omitted file; ``prune`` keeps none. Passing ``only`` without a mode
    selects ``merge``, because sending one file and deleting the rest of the
    profile is never what naming a file meant.

    ``only`` narrows the archive to ``profile.jsonld`` plus the named
    profile-relative paths.

    Before any of that travels, the preflight diffs the archive against the
    server's manifest into a :class:`PushPlan`. A push that would remove files
    raises :class:`PushWouldRemove` unless ``force=True``, and a profile still
    carrying the retired ``cache/`` directory raises :class:`PushRefused`;
    both leave the server untouched. ``dry_run=True`` stops after the plan and
    returns it, removals and all, rather than raising: a run that uploads
    nothing has nothing to refuse.

    Returns a :class:`PushResult`: the plan, the mode, and the server's parsed
    summary (``{slug, name, level, indexed, kept, mode}``), or ``None`` for a
    dry run. ``slug`` defaults to the directory name; ``token`` falls back to
    the ``RESEARCHER_PROFILES_TOKEN`` env var.
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
            f"{len(legacy)} file(s) under the retired {LEGACY_CACHE_DIRNAME}/ directory "
            f"would not ship: {legacy[:5]}\n"
            f"move the file(s) from {LEGACY_CACHE_DIRNAME}/ to {CACHE_DIRNAME}/ and run "
            "`rp manifest --write` to regenerate"
        )

    http = (
        client
        if client is not None
        else httpx.Client(base_url=base_url, headers=auth_headers(token), timeout=timeout)
    )
    try:
        plan = _plan_push(http, slug, src, archive, mode=mode)
        # A dry run answers first, refusals included: showing the removals is
        # the whole point of asking, and a run that uploads nothing cannot
        # perform them anyway.
        if dry_run:
            return PushResult(plan=plan, mode=mode)
        if plan.removed and not force:
            raise PushWouldRemove(plan)
        resp = http.put(
            f"/api/v1/profiles/{slug}?mode={mode}",
            content=archive.data,
            headers={"Content-Type": "application/gzip"},
        )
        if resp.status_code == 401:
            raise PermissionError(resp.text)
        if resp.status_code >= 400:
            raise RuntimeError(f"push failed ({resp.status_code}): {resp.text[:300]}")
        return PushResult(plan=plan, mode=mode, summary=resp.json())
    finally:
        if client is None:
            http.close()
