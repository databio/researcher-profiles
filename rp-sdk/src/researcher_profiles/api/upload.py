"""Profile archive helpers for push (``PUT``) and install (``GET .../archive``).

A profile travels as a (gzipped) tarball whose root is the profile
directory's contents: ``profile.jsonld`` sits at the tar root. The
helpers here build an archive from a directory and validate an inbound one.
Committing the staged directory belongs to the store
(:meth:`researcher_profiles.store.ProfileStore.commit_directory`), because what
"live" means differs per backend: an atomic rename for a directory, a
transaction for SQL. They are pure filesystem/tar code (no FastAPI), so the
install client reuses them.

``IngestResult`` and ``UploadError`` are re-exported here from
:mod:`researcher_profiles.store`, where they are defined: they describe a store
operation, and neither should require importing the HTTP layer to name.
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
from ..privacy import (
    ALWAYS_RESTRICTED_PREFIXES,
    ViewerTier,
    explain_tiers,
    project_document,
    tier_allows,
)

# ``SLUG_RE`` is the directory-name grammar, re-exported from the single
# canonical definition in ``schema/``. This is not an identity check. Never
# match a ``rid`` against this; it forbids the uppercase ``X`` check digit some
# ORCIDs carry.
from ..schema import ALWAYS_RESTRICTED_ROLES  # noqa: F401  (re-exported)
from ..store import IngestResult, ProfileStore, UploadError  # noqa: F401  (re-exported)
from ..utils.paths import CACHE_DIRNAME, LEGACY_CACHE_DIRNAME
from ..utils.slug import SLUG_RE  # noqa: F401  (re-exported)

if TYPE_CHECKING:  # pragma: no cover
    from ..schema import ArtifactRef

logger = logging.getLogger(__name__)

#: Server-side size cap for profile pushes (``PUT``). A spec-clean profile is
#: text-only (metadata, summaries, and extracted paper markdown) and runs a
#: few MB even carrying full paper text, so 50 MB is comfortable headroom. A
#: profile that hits this cap is signalling accidental non-text content (raw
#: scraped HTML, binaries, embedded images), not normal growth; the cap is a
#: guard that catches exactly that class of bloat regression.
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: Profile-relative directory holding downloaded paper fulltext. Excluded from
#: served archives unless the server opts in: the extracted paper text is a
#: derived copy of third-party copyrighted works, so a public registry must not
#: redistribute it.
FULLTEXT_SUBDIR = ("sources", "papers")

#: Whitelist of documented top-level profile members (see
#: ``docs/rp-sdk/profile-format.md``). Anything else on disk, notably raw
#: ``sources/html/`` scrape output, is a build-time intermediate that never
#: ships, so the archive builder drops it regardless of what is present.
#: ``index.html`` is the rendered profile page, a manifest part (role
#: ``html``) like any other.
PROFILE_TOP_LEVEL = frozenset(
    {"profile.jsonld", "SKILL.md", "index.html", "personality", "sources", CACHE_DIRNAME}
)

#: Members under ``.cache/`` that ship. Only the derived ``embeddings.sqlite``
#: index does; the other caches (``topics.json``, ``calibration.json``, …) are
#: regenerable and stay local. Build-session bookkeeping (``build_state.json``,
#: download attempts, rejection reasons, verification state) is
#: unreachable from here: it lives in the build root outside the content tree,
#: and pushing a profile publishes the record, not the build.
CACHE_MEMBERS = frozenset({"embeddings.sqlite"})

#: Whitelist of documented ``sources/`` members. Raw scraped HTML
#: (``sources/html/``) is absent: it is not a valid profile member.
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

#: Why a file on disk did not enter the archive, in the order a report reads
#: them. ``fulltext`` is policy (``include_fulltext=False``), ``legacy_cache``
#: is a profile built before the ``cache/`` -> ``.cache/`` rename, and
#: ``not_in_spec`` is everything the member whitelists reject.
DROP_REASONS = ("fulltext", "legacy_cache", "not_in_spec")


@dataclass(frozen=True)
class ProfileArchive:
    """A built archive plus the account of what stayed behind.

    ``members`` is every regular file inside the tarball, profile-relative, so
    a caller can diff the push against a server without reopening the tar.
    ``dropped`` maps a reason from :data:`DROP_REASONS` to the paths on disk
    that did not ship. The builder's exclusions are deliberate, but silent
    exclusions are how a user ends up pushing a profile they did not build:
    the account is returned so the caller can say so.
    """

    data: bytes
    members: frozenset[str]
    dropped: dict[str, list[str]]


def _drop_reason(rel: str, *, include_fulltext: bool) -> str | None:
    """Why the profile-relative path ``rel`` does not ship, or None if it does.

    The one place the member whitelists are applied. Prefix-based, so it
    answers for a directory as readily as for a file.
    """
    parts = Path(rel).parts
    if parts[0] == LEGACY_CACHE_DIRNAME:
        return "legacy_cache"
    # Dotfiles never ship. ``.cache/`` is the one dotted member on the
    # whitelist, and only for the ``CACHE_MEMBERS`` below it; its own name
    # is therefore exempt from this sweep, nothing nested under it is.
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

    One walk, both answers, from the single :func:`_drop_reason` predicate, so
    the tarball and the report it comes with can never disagree.

    A top-level directory that is not a profile member is reported as itself
    and not descended into: a profile directory that happens to be a git
    checkout would otherwise drown the report in ``.git/`` objects.
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
    than being dropped. Naming a file and having it silently vanish from the
    upload is precisely the behaviour ``only=`` exists to replace.
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

    ``GET /profiles/{slug}`` stamps ``effective_visibility`` onto every entry
    for the benefit of a reader. The model allows extra keys, so leaving it in
    would write a derived field back into a published document; drop it.
    """
    from ..schema import ArtifactRef

    return ArtifactRef.model_validate({k: v for k, v in raw.items() if k != "effective_visibility"})


def _project_only_manifest(
    src: Path, only: Sequence[str], remote_manifest: dict[str, dict]
) -> tuple[list["ArtifactRef"], list["ArtifactRef"]]:
    """``(hasPart, subjectOf)`` for an ``only=`` push: the server's index, refreshed.

    ``only=`` means "send these files and change nothing else". The document
    has to travel regardless (the server loads staging through it), and the
    manifest inside it is the index the server commits -- so taking that
    manifest from the local directory is what turns "send one file" into
    "replace the whole index". Here the manifest comes from the server, with
    only the named files' size and digest restamped from disk.

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

    Only the documented profile members ship: the top-level entries in
    :data:`PROFILE_TOP_LEVEL` and, within ``sources/``, the entries in
    :data:`SOURCES_MEMBERS`. Anything else on disk, such as dotfiles and stale
    build intermediates, is dropped -- and named in
    :attr:`ProfileArchive.dropped`, so a caller can tell the user what did not
    travel instead of letting them find out from the server.

    ``include_fulltext`` is a copyright gate, not a size gate: with
    ``include_fulltext=False`` (the default) the ``sources/papers/``
    extracted-text tree is omitted, yielding the metadata+summaries+index
    bundle that is safe to redistribute over the public GET serve.

    The default is exclude. This is the shared entry point for every
    archive that leaves this machine. The GET serve and the push client both
    route through here, so the safe behaviour must be what you get when you
    say nothing. Redistributing derived text of third-party copyrighted works
    is an act a caller opts into explicitly (a local backup, an
    operator-enabled serve), never something a caller falls into by forgetting
    a keyword.

    ``only`` narrows the archive to :data:`PROFILE_DOCUMENT` plus the named
    profile-relative paths. The document is not optional: the server loads the
    staged directory through it. Nothing is dropped in this mode -- a named
    path either ships or raises ``ValueError``.

    ``only_manifest_from_remote`` is the server's current manifest
    (``{contentUrl: entry}``). With it, the ``only=`` archive's
    ``profile.jsonld`` is built in memory from that manifest rather than read
    off disk, so a local copy whose manifest has fallen behind cannot shrink
    the server's index. See :func:`_project_only_manifest`. Inline sections
    (name, expertise, affiliations) still come from the local document.
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

    This is the egress archive: the one an HTTP caller can reach. Where
    :func:`build_profile_archive` hands another machine the whole record (a
    push, a migration), this answers "what may this reader have", using the same
    ``privacy.explain_tiers`` projection every other read route uses.

    Rules, in order:

    * a manifest artifact ships when ``tier_allows(viewer, effective_tier)``;
    * ``ALWAYS_RESTRICTED_ROLES`` (copyrighted full text) never ships, to
      anyone, at any tier: a legal floor, not a preference;
    * ``ALWAYS_RESTRICTED_PREFIXES`` (``.cache/``, ``.keys/``) never ships;
    * ``profile.jsonld`` always ships, but **projected**: the manifest inside
      it is tier-invariant (spec section 6), while its inline sections are not,
      and an exclude list cannot redact a field out of a document that is
      already in the tarball. It is otherwise gated at the profile level by the
      caller's route.
    """
    src = Path(profile_dir).expanduser().resolve()
    doc = src / "profile.jsonld"
    if not doc.is_file():
        raise FileNotFoundError(f"not a profile directory (no profile.jsonld): {src}")

    from ..schema import ProfileDocument

    profile = ProfileDocument.model_validate_json(doc.read_bytes())
    explain = explain_tiers(profile)

    def _ships(rel: str) -> bool:
        if rel == "profile.jsonld":
            # Never from disk: the projected copy is added separately below.
            return False
        if any(rel.startswith(prefix) for prefix in ALWAYS_RESTRICTED_PREFIXES):
            return False
        entry = explain.get(rel)
        if entry is None:
            # Not a manifest artifact: it is not part of the published record,
            # so it does not travel. (This is what retires the old whitelist:
            # the manifest already says what a profile contains.)
            return False
        if entry.role in ALWAYS_RESTRICTED_ROLES:
            return False
        return tier_allows(viewer, entry.effective)

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        rel = ti.name
        if any(part.startswith(".") for part in Path(rel).parts):
            return None
        if ti.isdir():
            # Keep a directory only when something under it actually ships. An
            # empty ``sources/papers/`` would otherwise still announce that the
            # profile holds full text it would not hand over.
            prefix = rel.rstrip("/") + "/"
            if not any(url.startswith(prefix) and _ships(url) for url in explain):
                return None
        elif not _ships(rel):
            return None
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    from ..schema.jsonld import canonical_dumps

    document = canonical_dumps(project_document(profile, viewer).model_dump(mode="json")).encode(
        "utf-8"
    )

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("profile.jsonld")
        info.size = len(document)
        tf.addfile(info, io.BytesIO(document))
        for child in sorted(src.iterdir()):
            if child.name.startswith("."):
                continue
            tf.add(child, arcname=child.name, filter=_filter)
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
#:   :data:`WITHHELD_CLASSES` class the archive carried no member of. A full
#:   build push is a reproducible replacement of the record.
#: * ``merge``: keep it, always. This is what ``rp push --only`` needs: send a
#:   file or two and leave the rest of the server's copy alone.
#: * ``prune``: delete it, always. The archive is the whole profile.
#:
#: Keeping a file is not enough on its own. The manifest inside
#: ``profile.jsonld`` is the index every reader (and the SQL store) works
#: from, so a kept file the incoming manifest does not list is a file nobody
#: can fetch and the SQL backend never even persists. Under ``merge`` and
#: ``replace`` the committed manifest is therefore the incoming manifest
#: **plus an entry for every kept file** (see
#: :func:`_splice_kept_into_manifest`). Under ``prune`` nothing is kept, so
#: nothing is spliced and the incoming manifest stands alone.
PushMode = Literal["replace", "merge", "prune"]

#: The :data:`PushMode` values, for a route validating a query parameter.
PUSH_MODES = get_args(PushMode)

#: Class name for a kept file no :data:`WITHHELD_CLASSES` predicate matches.
#: Only ``merge`` produces these: it keeps every live file the archive omitted,
#: classed or not.
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
    written out. It defaults to ``True`` because this function's job is
    faithful extraction of an archive already in hand: the install client keeps
    whatever a server chose to serve it. The copyright policy lives at the
    ingest boundary: ``PUT /profiles/{slug}`` always passes this explicitly
    from the server's ``accept_fulltext`` flag, so a misbehaving or outdated
    client cannot land copyrighted fulltext on a registry by sending it anyway.
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

    The single extract->stage->commit code path shared by the operator push
    (``PUT /api/v1/profiles/{slug}``) and the session-gated ingest, now over a
    store rather than a root: the tarball is the interchange format and the
    staging directory is transient, so what the profile ends up living in is
    the store's business, not this function's.

    The two callers differ only in ``include_fulltext`` (the copyright policy)
    and ``build_missing_index`` (a user hosting their own profile wants it
    rankable even when the archive shipped no index).

    ``gate`` is called with the staged profile's rid after extraction and
    before commit; whatever it raises aborts the ingest with nothing written.
    It exists because the store upserts on rid, not slug: the URL names a
    handle, but the profile that gets replaced is whichever one carries the
    archive's rid, so any "may you write this profile" check has to see the
    rid. See ``app.state.push_gate``.

    A push is a policy-filtered view of the sender's directory, not the whole
    of it: ``build_profile_archive`` withholds fulltext by default, and
    ``only=`` narrows it to a file or two. Absence from the tarball is
    therefore not automatically a deletion: :data:`PushMode` says what it
    means. Whatever survives is merged into staging before commit, so the
    commit itself (and both store backends) still sees one complete directory.

    Staging goes beside a directory store (an atomic ``os.rename`` needs the
    same filesystem) and into the system scratch space otherwise.
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

    Empty when the store does not hold it yet: a create has no live manifest
    to splice anything out of.
    """
    if not store.exists(slug):
        return {}
    meta = store.get(slug).metadata
    return {
        **{p.content_url: ("subjectOf", p) for p in meta.subject_of},
        # ``hasPart`` wins a duplicate contentUrl, matching the slot order the
        # SQL store writes rows in.
        **{p.content_url: ("hasPart", p) for p in meta.has_part},
    }


def _splice_kept_into_manifest(
    staging: Path,
    live_manifest: dict[str, tuple[str, "ArtifactRef"]],
    kept_paths: set[str],
) -> int:
    """Add the live manifest entries for every kept file the staged manifest omits.

    A kept file that the committed manifest does not list is a file no reader
    can fetch, and the SQL store will not even persist it: ``put`` writes rows
    *by the recorded manifest*, not by what is in staging. So keeping bytes
    without keeping the index entry is not a partial save, it is a silent
    delete. Returns how many entries were added.

    Ordering is deterministic: the staged entries keep their order and the
    spliced ones follow, sorted by ``contentUrl``.
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
    # ``dateModified`` is deliberately untouched: the push itself stamps it
    # through the normal write path, and this step restores entries that were
    # already part of the record rather than changing the record.
    doc_path.write_text(canonical_dumps(doc.model_dump(mode="json")), encoding="utf-8")
    logger.info("spliced %d kept manifest entry(ies) back into %s", len(missing), doc_path)
    return len(missing)


def _committed_manifest_counts(store: "ProfileStore", slug: str) -> dict[str, int]:
    """``{role: count}`` over the manifest the store now holds, post-commit.

    Read back from the store rather than from staging: this is the number the
    client prints as "the server now holds N artifacts", and it must describe
    what was committed, not what was offered.
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
    ``{class: [profile-relative paths kept]}`` for whatever contributed; a new
    profile has nothing to keep either way. The paths, not just their count,
    because the caller has to splice their manifest entries back in
    (:func:`_splice_kept_into_manifest`) and cannot do that from a number.

    The live copies come from the store's own directory when it has one and
    from a scratch export otherwise (push is rare; an export is affordable).
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

    Read the same way ``commit_directory`` will read it, so the rid the gate
    judges is the rid that gets written.
    """
    from ..profile import ResearcherProfile

    try:
        return ResearcherProfile.from_files(staging).rid
    except (OSError, ValueError, ProfileError, pydantic.ValidationError) as e:
        raise UploadError(f"staged profile failed to load: {e}") from e


@contextmanager
def _staging_dir(root: "Path | None", slug: str) -> "Iterator[Path]":
    """A transient directory to extract into, removed on the way out.

    Beside the profiles root when there is one, since ``swap_profile_dir``
    renames the staging directory into place and ``os.rename`` cannot cross a
    filesystem. Otherwise, anywhere the OS offers.
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
