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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pydantic

from ..errors import ProfileError
from ..privacy import (
    ALWAYS_RESTRICTED_PREFIXES,
    ViewerTier,
    explain_tiers,
    tier_allows,
)

# ``SLUG_RE`` is the directory-name grammar, re-exported from the single
# canonical definition in ``schema/``. This is not an identity check. Never
# match a ``rid`` against this; it forbids the uppercase ``X`` check digit some
# ORCIDs carry.
from ..schema import ALWAYS_RESTRICTED_ROLES  # noqa: F401  (re-exported)
from ..store import IngestResult, ProfileStore, UploadError  # noqa: F401  (re-exported)
from ..utils.paths import CACHE_DIRNAME
from ..utils.slug import SLUG_RE  # noqa: F401  (re-exported)

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
PROFILE_TOP_LEVEL = frozenset(
    {"profile.jsonld", "SKILL.md", "personality", "sources", CACHE_DIRNAME}
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
        "citations.json",
        "papers",
        "summaries",
        "cv.md",
        "web",
    }
)


def build_profile_archive(
    profile_dir: str | os.PathLike, *, include_fulltext: bool = False
) -> bytes:
    """Tar+gzip a profile directory's contents (``profile.jsonld`` at the root).

    Only the documented profile members ship: the top-level entries in
    :data:`PROFILE_TOP_LEVEL` and, within ``sources/``, the entries in
    :data:`SOURCES_MEMBERS`. Anything else on disk, such as dotfiles and
    stale build intermediates, is silently dropped, so a non-spec artifact can
    never bloat an archive.

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
    """
    src = Path(profile_dir).expanduser().resolve()
    if not (src / "profile.jsonld").is_file():
        raise FileNotFoundError(f"not a profile directory (no profile.jsonld): {src}")

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(ti.name).parts
        # Dotfiles never ship. ``.cache/`` is the one dotted member on the
        # whitelist, and only for the ``CACHE_MEMBERS`` below it; its own name
        # is therefore exempt from this sweep, nothing nested under it is.
        if any(p.startswith(".") for p in parts[1:]) or (
            parts[0].startswith(".") and parts[0] != CACHE_DIRNAME
        ):
            return None
        if parts[0] not in PROFILE_TOP_LEVEL:
            return None
        if parts[0] == "sources" and len(parts) >= 2:
            if parts[1] not in SOURCES_MEMBERS:
                return None
            if not include_fulltext and parts[1] == FULLTEXT_SUBDIR[1]:
                return None
        if parts[0] == CACHE_DIRNAME and len(parts) >= 2 and parts[1] not in CACHE_MEMBERS:
            return None
        # Normalize ownership so archives are reproducible across machines.
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        return ti

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for child in sorted(src.iterdir()):
            if child.name not in PROFILE_TOP_LEVEL:
                continue  # dotfiles + non-spec cruft never enter the tarball
            tf.add(child, arcname=child.name, filter=_filter)
    return buf.getvalue()


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
    * ``profile.jsonld`` always ships. It is the document itself, gated at the
      profile level by the caller's route, and the manifest inside it is
      tier-invariant (spec section 6).
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
            return True
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

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
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


def extract_profile_archive(
    data: bytes, staging_dir: Path, *, include_fulltext: bool = True
) -> None:
    """Validate ``data`` as a profile tarball and extract it into ``staging_dir``.

    Raises :class:`UploadError` on a malformed archive, unsafe members, or a
    missing root ``profile.jsonld``.

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
        root_files = {Path(m.name).as_posix().lstrip("./") for m in members if m.isfile()}
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


def ingest_archive(
    store: "ProfileStore",
    slug: str,
    data: bytes,
    *,
    include_fulltext: bool = False,
    build_missing_index: bool = False,
    gate: "Callable[[str], None] | None" = None,
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

    Staging goes beside a directory store (an atomic ``os.rename`` needs the
    same filesystem) and into the system scratch space otherwise.
    """
    root = store.root
    with _staging_dir(root, slug) as staging:
        extract_profile_archive(data, staging, include_fulltext=include_fulltext)
        if gate is not None:
            gate(_staged_rid(staging))
        return store.commit_directory(slug, staging, build_missing_index=build_missing_index)


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
