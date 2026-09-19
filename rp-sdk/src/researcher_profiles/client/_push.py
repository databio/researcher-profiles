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
#: ``respliced`` is a view of part of ``kept``, not a group beside it: those
#: entries are kept *and* absent from the local manifest, which is the state
#: that once looked like "removed 0" while 53 artifacts disappeared.
PLAN_GROUPS = ("added", "changed", "unchanged", "removed", "kept", "respliced")

#: The capability name a server advertises when it splices a kept file's
#: manifest entry back into the committed document. Without it, an entry the
#: local manifest dropped is deleted no matter how many files the server keeps.
SPLICE_FEATURE = "manifest_splice"

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
        why = f"{n} file(s) the server holds are not in this push"
        if plan.shrinks:
            why += (
                f"; the manifest would go from {plan.manifest_before} to "
                f"{plan.manifest_after} entries"
            )
        super().__init__(why)


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

    ``respliced`` is the subset of ``kept`` whose entries the **local**
    manifest no longer lists. The server keeps those bytes and adds the
    manifest entry back, so they survive -- but only on a server that
    advertises ``manifest_splice``. Against one that does not, they are
    counted as ``removed`` instead, because a kept file with no manifest entry
    is a file no reader can fetch.

    ``manifest_before`` / ``manifest_after`` are entry counts: what the server
    indexes now, and what it would index after this push. A push whose
    manifest shrinks is a removal even when ``removed`` is empty; that
    disagreement is what let a one-file push delete 53 artifacts under a
    report that read ``removed 0``.

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
    respliced: dict[str, list[str]] = field(default_factory=dict)
    dropped: dict[str, list[str]] = field(default_factory=dict)
    #: Manifest entry counts: the server's now, and the predicted post-commit one.
    manifest_before: int = 0
    manifest_after: int = 0
    #: Local manifest entries with no file on disk (legacy ``cache/`` excluded).
    #: The signature of a partial copy; see ``push_profile``'s refusal.
    local_stale: list[str] = field(default_factory=list)

    @property
    def shrinks(self) -> bool:
        """Whether this push would leave the server indexing fewer artifacts."""
        return self.manifest_after < self.manifest_before

    def counts(self) -> dict[str, int]:
        """``{group: file count}`` for every group in :data:`PLAN_GROUPS`.

        ``respliced`` overlaps ``kept`` by construction; it is reported as its
        own number because it is the one a reader has to see.
        """
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


def _server_capabilities(http: Any) -> dict:
    """What the server says it supports, or ``{}`` when it will not say.

    ``{}`` is the honest answer for a build that predates
    ``GET /api/v1/capabilities`` (a 404) or a transport that failed; the
    caller decides what to do about it, and the decision differs per mode.
    Never raises: this is a pre-check, not the push.
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

    The local manifest is not a description of the directory: it is the index
    that travels inside ``profile.jsonld`` and becomes the server's index on
    commit. A preflight that never reads it cannot tell a push that adds a
    file from a push that drops fifty-three.
    """
    from ..api.upload import PROFILE_DOCUMENT

    try:
        doc = json.loads((src / PROFILE_DOCUMENT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = [*(doc.get("hasPart") or []), *(doc.get("subjectOf") or [])]
    return {e["contentUrl"]: e for e in entries if isinstance(e, dict) and e.get("contentUrl")}


def _local_roles(src: Path) -> dict[str, str]:
    """``contentUrl -> role`` from the profile's own manifest.

    Roles only. The recorded digests are deliberately not read: a manifest
    written before the last edit would report a changed file as unchanged,
    which is the one answer a preflight must never give. A role that is a
    little stale only mislabels a group heading.
    """
    return {url: (e.get("role") or UNKNOWN_ROLE) for url, e in _local_manifest_entries(src).items()}


def _local_stale_entries(src: Path) -> list[str]:
    """Local manifest entries naming a file the directory does not have.

    The signature of a partial copy. Retired ``cache/`` entries are excluded:
    those name a file that IS there under the pre-rename directory name, and
    ``push_profile`` refuses on them separately with its own fix.
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

    Not against the archive's members. The tarball is bytes; the *manifest*
    inside ``profile.jsonld`` is the index the server commits, the only thing
    a reader can fetch through, and the only thing the SQL store writes rows
    for. Diffing members against the remote manifest answered "what bytes
    travel" while the question was "what will the server still have", and the
    two gave opposite answers for the push that deleted 53 artifacts.

    Local digests are computed from the files going into the tarball rather
    than read out of ``profile.jsonld``: the recorded manifest can be stale,
    and a stale digest turns a real change into "unchanged". A remote entry
    with no recorded digest counts as changed for the same reason -- there is
    no evidence it is the same file.

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

    # The manifest that travels inside profile.jsonld. With ``only``, the
    # archive builder replaced it with the server's own (see
    # ``upload._project_only_manifest``), so the server's index is what gets
    # committed; otherwise it is whatever the local document happens to say.
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
    profile-relative paths, and takes the manifest inside that document from
    the **server** rather than from disk: naming a file to send has never
    meant "and re-index the profile from this directory". Inline sections
    (name, expertise, affiliations) still come from the local document; to
    change one field and nothing else, use ``rp work patch``.

    Before any of that travels, the preflight diffs the server's manifest
    against the manifest this push would commit, into a :class:`PushPlan`. A
    push that would remove files, or that would leave the server indexing
    fewer artifacts than it does now, raises :class:`PushWouldRemove` unless
    ``force=True``. A profile still carrying the retired ``cache/`` directory,
    or whose manifest names files this directory does not have (the signature
    of a partial copy), raises :class:`PushRefused`. All of them leave the
    server untouched. ``dry_run=True`` stops after the plan and returns it,
    removals and all, rather than raising: a run that uploads nothing has
    nothing to refuse.

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
            f"{len(legacy)} file(s) under the retired {LEGACY_CACHE_DIRNAME}/ directory "
            f"would not ship: {legacy[:5]}\n"
            f"move the file(s) from {LEGACY_CACHE_DIRNAME}/ to {CACHE_DIRNAME}/ and run "
            "`rp manifest --write` (it will show what changes and refuse to drop entries)"
        )

    # A manifest that names files this directory does not have is not drift to
    # be tidied up, it is the tell that this is a partial copy of the profile.
    # Pushing it re-indexes the server from an incomplete directory. With
    # ``only`` the manifest comes from the server instead, so the stale local
    # entries never travel and the refusal would be noise.
    stale = _local_stale_entries(src)
    if stale and not only:
        raise PushRefused(
            f"{len(stale)} manifest entry(ies) name files this directory does not have: "
            f"{stale[:5]}\n"
            "This copy is not the whole profile. Push from the directory that built it\n"
            "(`rp where <slug>` shows which root is in force), or pass --only to send\n"
            "named files without touching the rest."
        )

    http = (
        client
        if client is not None
        else httpx.Client(base_url=base_url, headers=auth_headers(token), timeout=timeout)
    )
    try:
        caps = _server_capabilities(http)
        advertised = caps.get("push_modes") or []
        # ``replace`` is what a server that has never heard of ``?mode=`` does
        # anyway, so silence there is not a disagreement. Asking such a server
        # for ``merge`` or ``prune`` is: it would answer 200 and do a replace.
        if advertised and mode not in advertised:
            raise PushRefused(
                f"server does not support mode={mode} (it offers: "
                f"{', '.join(advertised) or 'none'}).\n"
                "Upgrade the server, or push a full build with the default mode."
            )
        if not advertised and mode != "replace":
            raise PushRefused(
                f"server does not advertise its push modes, so mode={mode} cannot be "
                "relied on:\nit may predate ?mode= and silently run a replace. "
                "Upgrade the server, or push a\nfull build with the default mode."
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
        # A dry run answers first, refusals included: showing the removals is
        # the whole point of asking, and a run that uploads nothing cannot
        # perform them anyway.
        if dry_run:
            return PushResult(plan=plan, mode=mode)
        # A shrinking manifest is a removal even when no single path landed in
        # ``removed``: fewer entries means fewer artifacts a reader can fetch.
        if (plan.removed or plan.shrinks) and not force:
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
