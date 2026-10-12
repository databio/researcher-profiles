"""Profile archive helpers for push (``PUT``) and install (``GET .../archive``).

A profile travels as a gzipped tarball with ``profile.jsonld`` at the tar
root. Committing the staged directory belongs to the store
(:meth:`researcher_profiles.store.ProfileStore.commit_directory`). No FastAPI
here, so the install client reuses these helpers.
"""

import io
import logging
import os
import shutil
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

import pydantic

from ..errors import ProfileError
from ..privacy import ViewerTier

# ``SLUG_RE`` is the directory-name grammar, not an identity check. Never
# match a ``rid`` against it; it forbids the uppercase ``X`` ORCID check digit.
from ..store import IngestResult, ProfileStore, UploadError  # noqa: F401  (re-exported)
from ..utils.paths import CACHE_DIRNAME, LEGACY_CACHE_DIRNAME
from ..utils.slug import SLUG_RE  # noqa: F401  (re-exported)

if TYPE_CHECKING:  # pragma: no cover
    from ..schema import ArtifactRef

logger = logging.getLogger(__name__)

#: Server-side size cap for profile pushes (``PUT``). A text-only profile runs
#: a few MB even with full paper text, so hitting 50 MB signals accidental
#: non-text content (scraped HTML, binaries, images).
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: Profile-relative directory holding downloaded paper fulltext. Excluded from
#: served archives unless the server opts in: the extracted paper text is a
#: derived copy of third-party copyrighted works, so a public registry must not
#: redistribute it.
FULLTEXT_SUBDIR = ("sources", "papers")

#: Whitelist of documented top-level profile members (see
#: ``docs/rp-sdk/profile-format.md``). Anything else on disk never ships.
PROFILE_TOP_LEVEL = frozenset(
    {"profile.jsonld", "SKILL.md", "index.html", "personality", "sources", CACHE_DIRNAME}
)

#: Members under ``.cache/`` that ship. The other caches are regenerable and
#: stay local.
CACHE_MEMBERS = frozenset({"embeddings.sqlite"})

#: Whitelist of documented ``sources/`` members.
SOURCES_MEMBERS = frozenset(
    {
        "papers.jsonld",
        "grants.jsonld",
        "trials.jsonld",
        "citations.json",
        "papers",
        "summaries",
        "cv.md",
        "web",
    }
)


#: The profile document. Always ships: the server loads the staged directory
#: through it, so an archive without it is not a profile.
PROFILE_DOCUMENT = "profile.jsonld"

#: Why a file on disk did not enter the archive, in report order.
#: ``fulltext`` is policy (``include_fulltext=False``), ``legacy_cache`` is a
#: ``cache/`` directory, and ``not_in_spec`` is everything the whitelists reject.
DROP_REASONS = ("fulltext", "legacy_cache", "not_in_spec")


@dataclass(frozen=True)
class ProfileArchive:
    """A built archive plus the account of what stayed behind.

    ``members`` is every regular file inside the tarball, profile-relative, so
    a caller can diff the push against a server without reopening the tar.
    ``dropped`` maps a reason from :data:`DROP_REASONS` to the paths on disk
    that did not ship, so the caller can tell the user.
    """

    data: bytes
    members: frozenset[str]
    dropped: dict[str, list[str]]


def _drop_reason(rel: str, *, include_fulltext: bool) -> str | None:
    """Why the profile-relative path ``rel`` does not ship, or None if it does.

    Prefix-based, so it answers for a directory as well as a file.
    """
    parts = Path(rel).parts
    if parts[0] == LEGACY_CACHE_DIRNAME:
        return "legacy_cache"
    # Dotfiles never ship, except the top-level ``.cache/`` itself.
    if any(p.startswith(".") for p in parts[1:]) or (
        parts[0].startswith(".") and parts[0] != CACHE_DIRNAME
    ):
        return "not_in_spec"
    if parts[0] not in PROFILE_TOP_LEVEL:
        return "not_in_spec"
    if parts[0] == "sources" and len(parts) >= 2:
        if parts[1] not in SOURCES_MEMBERS:
            return "not_in_spec"
        if not include_fulltext and parts[1] == FULLTEXT_SUBDIR[1]:
            return "fulltext"
    if parts[0] == CACHE_DIRNAME and len(parts) >= 2 and parts[1] not in CACHE_MEMBERS:
        return "not_in_spec"
    return None


def _walk_members(src: Path, *, include_fulltext: bool) -> tuple[list[str], dict[str, list[str]]]:
    """Split a profile directory into ``(members, dropped)``.

    A top-level directory that is not a profile member is reported as itself
    and not descended into, so a ``.git/`` does not flood the report.
    """
    members: list[str] = []
    dropped: dict[str, list[str]] = {}
    for child in sorted(src.iterdir()):
        if child.is_dir():
            if child.name not in PROFILE_TOP_LEVEL and child.name != LEGACY_CACHE_DIRNAME:
                dropped.setdefault("not_in_spec", []).append(f"{child.name}/")
                continue
            files = [f for f in sorted(child.rglob("*")) if f.is_file()]
        elif child.is_file():
            files = [child]
        else:
            continue
        for f in files:
            rel = f.relative_to(src).as_posix()
            reason = _drop_reason(rel, include_fulltext=include_fulltext)
            if reason is None:
                members.append(rel)
            else:
                dropped.setdefault(reason, []).append(rel)
    return members, dropped


def _only_members(src: Path, only: Sequence[str], *, include_fulltext: bool) -> list[str]:
    """The members of an ``only=`` archive: the document plus the named files.

    A named path that does not exist or is not a pushable member raises rather
    than being silently dropped.
    """
    members = [PROFILE_DOCUMENT]
    for name in only:
        rel = Path(name).as_posix()
        if rel == PROFILE_DOCUMENT:
            continue
        if not (src / rel).is_file():
            raise ValueError(f"not a file in the profile: {name!r}")
        reason = _drop_reason(rel, include_fulltext=include_fulltext)
        if reason == "fulltext":
            raise ValueError(f"{name!r} is extracted fulltext: pass include_fulltext to send it")
        if reason is not None:
            raise ValueError(f"{name!r} is not a profile member ({reason})")
        members.append(rel)
    return list(dict.fromkeys(members))


def _normalize(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    """Zero the ownership so archives are reproducible across machines."""
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def _remote_entry(raw: dict) -> "ArtifactRef":
    """One served manifest entry back into an :class:`ArtifactRef`.

    Drops the served ``effective_visibility`` key so a derived field is not
    written back into a published document.
    """
    from ..schema import ArtifactRef

    return ArtifactRef.model_validate({k: v for k, v in raw.items() if k != "effective_visibility"})


def _project_only_manifest(
    src: Path, only: Sequence[str], remote_manifest: dict[str, dict]
) -> tuple[list["ArtifactRef"], list["ArtifactRef"]]:
    """``(hasPart, subjectOf)`` for an ``only=`` push: the server's index, refreshed.

    ``only=`` means "send these files and change nothing else". The document's
    manifest is the index the server commits, so it comes from the server,
    with only the named files' size and digest restamped from disk.

    A named file the server does not know about is added from the local
    manifest, or from a freshly built entry when the local manifest has not
    caught up either.
    """
    import hashlib

    from ..schema import ProfileDocument
    from ..schema.manifest import SUBJECT_ROLES, build_manifest

    doc = ProfileDocument.model_validate_json((src / PROFILE_DOCUMENT).read_bytes())
    local = {p.content_url: p for p in (*doc.has_part, *doc.subject_of)}
    entries = {url: _remote_entry(raw) for url, raw in remote_manifest.items()}

    named = sorted({Path(n).as_posix() for n in only} - {PROFILE_DOCUMENT})
    generated: dict[str, ArtifactRef] | None = None
    for rel in named:
        entry = entries.get(rel) or local.get(rel)
        if entry is None:
            if generated is None:
                parts, subjects = build_manifest(src)
                generated = {p.content_url: p for p in (*parts, *subjects)}
            entry = generated.get(rel)
        if entry is None:
            # Nothing anywhere describes this file. It still ships (the caller
            # named it), it simply gets no manifest entry from here.
            continue
        data = (src / rel).read_bytes()
        refreshed = entry.model_copy(deep=True)
        refreshed.bytes = len(data)
        refreshed.sha256 = hashlib.sha256(data).hexdigest()
        entries[rel] = refreshed

    has_part: list[ArtifactRef] = []
    subject_of: list[ArtifactRef] = []
    for url in sorted(entries):
        entry = entries[url]
        (subject_of if entry.role in SUBJECT_ROLES else has_part).append(entry)
    return has_part, subject_of


def build_profile_archive(
    profile_dir: str | os.PathLike,
    *,
    include_fulltext: bool = False,
    only: Sequence[str] | None = None,
    only_manifest_from_remote: dict[str, dict] | None = None,
) -> ProfileArchive:
    """Tar+gzip a profile directory's contents (``profile.jsonld`` at the root).

    Only the documented profile members ship (:data:`PROFILE_TOP_LEVEL`,
    :data:`SOURCES_MEMBERS`); everything else is named in
    :attr:`ProfileArchive.dropped`.

    ``include_fulltext`` is a copyright gate (see :data:`FULLTEXT_SUBDIR`).
    It defaults to exclude because every archive that leaves this machine
    goes through here, so saying nothing must be the safe choice.

    ``only`` narrows the archive to :data:`PROFILE_DOCUMENT` plus the named
    profile-relative paths. The document is not optional: the server loads the
    staged directory through it. Nothing is dropped in this mode -- a named
    path either ships or raises ``ValueError``.

    ``only_manifest_from_remote`` is the server's current manifest
    (``{contentUrl: entry}``). With it, the ``only=`` archive's
    ``profile.jsonld`` is built in memory from that manifest rather than read
    off disk, so a stale local manifest cannot shrink the server's index.
    Inline sections still come from the local document.
    """
    src = Path(profile_dir).expanduser().resolve()
    if not (src / PROFILE_DOCUMENT).is_file():
        raise FileNotFoundError(f"not a profile directory (no {PROFILE_DOCUMENT}): {src}")

    if only is None:
        members, dropped = _walk_members(src, include_fulltext=include_fulltext)
    else:
        members, dropped = _only_members(src, only, include_fulltext=include_fulltext), {}

    document: bytes | None = None
    if only is not None and only_manifest_from_remote is not None:
        from ..schema import ProfileDocument
        from ..schema.jsonld import canonical_dumps

        doc = ProfileDocument.model_validate_json((src / PROFILE_DOCUMENT).read_bytes())
        doc.has_part, doc.subject_of = _project_only_manifest(src, only, only_manifest_from_remote)
        document = canonical_dumps(doc.model_dump(mode="json")).encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel in members:
            if rel == PROFILE_DOCUMENT and document is not None:
                info = tarfile.TarInfo(PROFILE_DOCUMENT)
                info.size = len(document)
                tf.addfile(_normalize(info), io.BytesIO(document))
                continue
            tf.add(src / rel, arcname=rel, filter=_normalize)
    return ProfileArchive(data=buf.getvalue(), members=frozenset(members), dropped=dropped)


def build_viewer_archive(profile_dir: str | os.PathLike, *, viewer: ViewerTier) -> bytes:
    """Tar+gzip only what a viewer entitled to ``viewer`` may see.

    The egress archive an HTTP caller can reach. What ships is decided by
    :func:`~researcher_profiles.publish._export.plan_profile_export`, the
    same projection ``rp publish`` writes, so the two cannot drift. The
    whole-profile gate is the caller's job.
    """
    from ..publish._export import PROFILE_DOCUMENT, plan_profile_export

    src = Path(profile_dir).expanduser().resolve()
    plan = plan_profile_export(src, viewer)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(PROFILE_DOCUMENT)
        info.size = len(plan.document)
        tf.addfile(info, io.BytesIO(plan.document))
        for rel in plan.files:
            tf.add(src / rel, arcname=rel, filter=_normalize)
    return buf.getvalue()


def _member_is_unsafe(member: tarfile.TarInfo) -> str | None:
    """Return a rejection reason for a tar member, or None if it is safe.

    Only regular files and directories are allowed; symlinks, hardlinks,
    devices, and any path escaping the extraction root are rejected.
    """
    name = member.name
    if member.issym() or member.islnk():
        return f"link member not allowed: {name!r}"
    if not (member.isfile() or member.isdir()):
        return f"special member not allowed: {name!r}"
    if name.startswith("/") or name.startswith("\\"):
        return f"absolute path not allowed: {name!r}"
    parts = Path(name).parts
    if ".." in parts:
        return f"path traversal not allowed: {name!r}"
    if os.path.isabs(name):
        return f"absolute path not allowed: {name!r}"
    return None


def _is_fulltext_member(name: str) -> bool:
    """True if a tar member path lands inside ``sources/papers/``."""
    parts = tuple(p for p in Path(name).parts if p not in (".", ""))
    return parts[: len(FULLTEXT_SUBDIR)] == FULLTEXT_SUBDIR


#: Path predicates for artifact classes a push may legitimately omit. When the
#: archive carries none of a class and the live profile has some, ingest keeps
#: the live copies. Deleting them takes an explicit prune.
WITHHELD_CLASSES: dict[str, Callable[[str], bool]] = {
    "fulltext": _is_fulltext_member,
    "index": lambda name: Path(name).parts[:2] == (CACHE_DIRNAME, "embeddings.sqlite"),
}

#: What an ingest does with a live file the archive does not carry:
#:
#: * ``replace`` (the default): keep it only when it belongs to a
#:   :data:`WITHHELD_CLASSES` class the archive carried no member of.
#: * ``merge``: keep it, always (``rp push --only``).
#: * ``prune``: delete it, always. The archive is the whole profile.
#:
#: Kept files get their manifest entries spliced back in
#: (:func:`_splice_kept_into_manifest`).
PushMode = Literal["replace", "merge", "prune"]

#: The :data:`PushMode` values, for a route validating a query parameter.
PUSH_MODES = get_args(PushMode)

#: Class name for a kept file no :data:`WITHHELD_CLASSES` predicate matches
#: (only ``merge`` produces these).
OTHER_CLASS = "other"


def extract_profile_archive(
    data: bytes, staging_dir: Path, *, include_fulltext: bool = True
) -> set[str]:
    """Validate ``data`` as a profile tarball and extract it into ``staging_dir``.

    Raises :class:`UploadError` on a malformed archive, unsafe members, or a
    missing root ``profile.jsonld``. Returns the profile-relative names of
    every regular file the archive carried, before the fulltext filter below:
    what the sender chose to send, not what landed.

    ``include_fulltext`` controls whether ``sources/papers/`` members are
    written out. It defaults to ``True`` for faithful extraction (the install
    client); the ingest path always passes the server's ``accept_fulltext``,
    so a client cannot land fulltext on a registry by sending it anyway.
    """
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except (tarfile.TarError, EOFError) as e:
        raise UploadError(f"not a readable tar archive: {e}") from e
    with tf:
        members = tf.getmembers()
        if not members:
            raise UploadError("empty archive")
        for m in members:
            reason = _member_is_unsafe(m)
            if reason:
                raise UploadError(reason)
        # ``Path`` collapses a leading ``./``; a character-wise strip would also
        # eat the dot of ``.cache/``.
        root_files = {Path(m.name).as_posix() for m in members if m.isfile()}
        if "profile.jsonld" not in root_files:
            raise UploadError(
                "archive must contain profile.jsonld at its root "
                "(tar the profile directory's contents, not the directory itself)"
            )
        if not include_fulltext:
            members = [m for m in members if not _is_fulltext_member(m.name)]
        staging_dir.mkdir(parents=True, exist_ok=True)
        try:
            tf.extractall(path=staging_dir, members=members, filter="data")
        except tarfile.TarError as e:
            raise UploadError(f"archive extraction failed: {e}") from e
        return root_files


def ingest_archive(
    store: "ProfileStore",
    slug: str,
    data: bytes,
    *,
    include_fulltext: bool = False,
    build_missing_index: bool = False,
    gate: "Callable[[str], None] | None" = None,
    mode: PushMode = "replace",
) -> IngestResult:
    """Validate, stage, and commit an uploaded profile tarball into ``store``.

    ``build_missing_index`` builds the vector index when the archive shipped
    none.

    ``gate`` is called with the staged profile's rid after extraction and
    before commit; whatever it raises aborts the ingest with nothing written.
    The store upserts on rid, not slug, so any "may you write this profile"
    check has to see the rid. See ``app.state.push_gate``.

    Absence from the tarball is not automatically a deletion: :data:`PushMode`
    says what it means. Kept files are merged into staging before commit, so
    the store still sees one complete directory.
    """
    root = store.root
    with _staging_dir(root, slug) as staging:
        archive_names = extract_profile_archive(data, staging, include_fulltext=include_fulltext)
        if gate is not None:
            gate(_staged_rid(staging))
        kept_paths = _keep_omitted(store, slug, staging, archive_names, mode=mode)
        kept_flat = {rel for paths in kept_paths.values() for rel in paths}
        spliced = (
            _splice_kept_into_manifest(staging, _live_manifest(store, slug), kept_flat)
            if kept_flat
            else 0
        )
        result = store.commit_directory(slug, staging, build_missing_index=build_missing_index)
        result.kept = {cls: len(paths) for cls, paths in kept_paths.items()}
        result.spliced = spliced
        result.mode = mode
        result.manifest_counts = _committed_manifest_counts(store, slug)
        return result


def _live_manifest(store: "ProfileStore", slug: str) -> dict[str, tuple[str, "ArtifactRef"]]:
    """``{contentUrl: (manifest_slot, entry)}`` for the profile the store holds.

    Empty when the store does not hold it yet.
    """
    if not store.exists(slug):
        return {}
    meta = store.get(slug).metadata
    return {
        **{p.content_url: ("subjectOf", p) for p in meta.subject_of},
        # ``hasPart`` wins a duplicate contentUrl, as in the SQL store.
        **{p.content_url: ("hasPart", p) for p in meta.has_part},
    }


def _splice_kept_into_manifest(
    staging: Path,
    live_manifest: dict[str, tuple[str, "ArtifactRef"]],
    kept_paths: set[str],
) -> int:
    """Add the live manifest entries for every kept file the staged manifest omits.

    A kept file the committed manifest does not list is a file no reader can
    fetch, and the SQL store (which writes rows by the manifest) never
    persists it. Keeping bytes without the entry would be a silent delete.
    Returns how many entries were added; spliced entries follow the staged
    ones, sorted by ``contentUrl``.
    """
    from ..schema import ProfileDocument
    from ..schema.jsonld import canonical_dumps

    doc_path = staging / PROFILE_DOCUMENT
    try:
        doc = ProfileDocument.model_validate_json(doc_path.read_bytes())
    except (OSError, ValueError, pydantic.ValidationError) as e:
        raise UploadError(f"staged profile failed to load: {e}") from e

    staged_urls = {p.content_url for p in (*doc.has_part, *doc.subject_of)}
    missing = sorted(rel for rel in kept_paths if rel not in staged_urls and rel in live_manifest)
    if not missing:
        return 0

    has_part = list(doc.has_part)
    subject_of = list(doc.subject_of)
    for rel in missing:
        slot, entry = live_manifest[rel]
        (has_part if slot == "hasPart" else subject_of).append(entry)
    doc.has_part = has_part
    doc.subject_of = subject_of
    # ``dateModified`` is left alone: the push stamps it through the normal
    # write path.
    doc_path.write_text(canonical_dumps(doc.model_dump(mode="json")), encoding="utf-8")
    logger.info("spliced %d kept manifest entry(ies) back into %s", len(missing), doc_path)
    return len(missing)


def _committed_manifest_counts(store: "ProfileStore", slug: str) -> dict[str, int]:
    """``{role: count}`` over the manifest the store now holds, post-commit.

    Read back from the store, so it describes what was committed, not what
    was offered.
    """
    try:
        entries = store.get(slug).manifest()
    # Boundary: reporting must never fail a push that already committed.
    except Exception:  # noqa: BLE001
        logger.warning("could not read back the committed manifest for %s", slug, exc_info=True)
        return {}
    counts: dict[str, int] = {}
    for entry in entries:
        role = entry.role or OTHER_CLASS
        counts[role] = counts.get(role, 0) + 1
    return counts


def _keep_omitted(
    store: "ProfileStore",
    slug: str,
    staging: Path,
    archive_names: set[str],
    *,
    mode: PushMode,
) -> dict[str, list[str]]:
    """Copy the live files the archive omitted into ``staging``, per ``mode``.

    Under ``replace`` a :data:`WITHHELD_CLASSES` class is kept only when the
    archive carried none of it: one member of a class makes the archive
    authoritative for the whole class. Under ``merge`` every live file the
    archive did not name is kept, counted under its class or
    :data:`OTHER_CLASS`. Under ``prune`` nothing is. Returns
    ``{class: [profile-relative paths kept]}``.

    Fulltext kept this way is the server's own copy, already admitted under
    its ``accept_fulltext`` policy, so that gate does not apply here.
    """
    if mode == "prune" or not store.exists(slug):
        return {}

    if mode == "merge":

        def _class_of(rel: str) -> str | None:
            if rel in archive_names:
                return None
            cls = next((c for c, match in WITHHELD_CLASSES.items() if match(rel)), None)
            return cls or OTHER_CLASS
    else:
        absent = {
            cls: match
            for cls, match in WITHHELD_CLASSES.items()
            if not any(match(name) for name in archive_names)
        }
        if not absent:
            return {}

        def _class_of(rel: str) -> str | None:
            return next((c for c, match in absent.items() if match(rel)), None)

    live_slug = store.resolve_slug(slug)
    root = store.root
    with tempfile.TemporaryDirectory(prefix=f"rp-keep-{slug}-") as tmp:
        live = (
            root / live_slug if root is not None else store.export_directory(live_slug, Path(tmp))
        )
        kept: dict[str, list[str]] = {}
        for item in sorted(live.rglob("*")):
            if not item.is_file():
                continue
            rel = item.relative_to(live).as_posix()
            cls = _class_of(rel)
            if cls is None:
                continue
            target = staging / rel
            if target.exists():  # never overwrite what the archive carried
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            kept.setdefault(cls, []).append(rel)
    return kept


def _staged_rid(staging: Path) -> str:
    """The rid a staged directory's ``profile.jsonld`` declares.

    Read the way ``commit_directory`` reads it, so the gate judges the rid
    that gets written.
    """
    from ..profile import ResearcherProfile

    try:
        return ResearcherProfile.from_files(staging).rid
    except (OSError, ValueError, ProfileError, pydantic.ValidationError) as e:
        raise UploadError(f"staged profile failed to load: {e}") from e


@contextmanager
def _staging_dir(root: "Path | None", slug: str) -> "Iterator[Path]":
    """A transient directory to extract into, removed on the way out.

    Beside the profiles root when there is one, since ``os.rename`` cannot
    cross a filesystem.
    """
    if root is not None:
        staging = Path(root) / f".upload-{slug}-{uuid.uuid4().hex}"
        try:
            yield staging
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return
    with tempfile.TemporaryDirectory(prefix=f"rp-upload-{slug}-") as tmp:
        yield Path(tmp) / slug
