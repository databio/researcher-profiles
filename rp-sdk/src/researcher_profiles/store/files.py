"""``FilesystemProfileStore``: profiles as directories under one root.

Core-only: nothing here imports FastAPI, SQLModel, or anything outside the base
install. The one deferred import is the tar/staging machinery in
:mod:`researcher_profiles.api.upload`, pulled in inside
:meth:`FilesystemProfileStore.commit_directory` because reaching it at module
scope would execute ``researcher_profiles.api.__init__`` and drag FastAPI onto
the core import path.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from pydantic import ValidationError

from ..errors import ProfileError, ProfileWriteError
from ..profile import ResearcherProfile
from ..schema import ProfileDocument
from ..utils.paths import STORE_CACHE_DIRNAME, cache_dir
from ._analytics import _AnalyticsAccessors
from .hooks import _HookedStore
from .protocol import DuplicateIdentityError, IngestResult, ProfileNotFoundError, UploadError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

    from ..embeddings.protocol import VectorIndex

logger = logging.getLogger(__name__)

__all__ = ["FilesystemProfileStore", "swap_profile_dir"]

#: The ``"rid"`` member of profile.jsonld. A text scan, not a JSON parse: this
#: runs over every profile in the root to answer "which directory is this
#: rid?", and a published profile.jsonld may inline the whole works list, so
#: parsing 30 of them to read one scalar is not worth it.
_RID_LINE_RE = re.compile(r'"rid"\s*:\s*"([^"]+)"')


def _rid_from_document_text(path: Path) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _RID_LINE_RE.search(text)
    return m.group(1) if m else None


def swap_profile_dir(root: Path, slug: str, staging_dir: Path) -> Path:
    """Atomically replace ``root/slug`` with ``staging_dir``.

    The old directory (if any) is renamed aside first and removed only after the
    new one is in place; on failure the old directory is restored. Returns the
    final profile path.

    This is the filesystem backend's whole answer to atomicity, and it is a
    rename, not a transaction: it covers the directory swap and nothing else.
    A SQL store replaces it with a real transaction.
    """
    target = root / slug
    backup = root / f".old-{slug}-{uuid.uuid4().hex}"
    had_old = target.exists()
    if had_old:
        os.rename(target, backup)
    try:
        os.rename(staging_dir, target)
    except OSError:
        if had_old:
            os.rename(backup, target)
        raise
    if had_old:
        shutil.rmtree(backup, ignore_errors=True)
    return target


class FilesystemProfileStore(_HookedStore, _AnalyticsAccessors):
    """Profiles as directories under one root, with a small LRU over the
    loaded :class:`~researcher_profiles.profile.ResearcherProfile` objects.

    Eviction is a no-op beyond dropping the reference. The sqlite handles are
    GC'd along with the index objects on the profile.
    """

    def __init__(self, root: str | os.PathLike, capacity: int = 32):
        self._root = Path(root).expanduser().resolve()
        self.capacity = capacity
        self._cache: OrderedDict[str, ResearcherProfile] = OrderedDict()
        self._rid_map_cache: dict[str, str] | None = None
        self._rid_map_stamp: tuple | None = None
        #: slug -> the profile's open vector index. Separate from the profile
        #: LRU: an index is expensive to reopen (a sqlite handle, or a whole
        #: blob parsed into a matrix) and ranking touches every survivor of
        #: every query.
        self._vector_indexes: dict[str, "VectorIndex"] = {}
        # Pre/post-commit hooks live on the store, not on the app; see
        # ``_HookedStore``. Already-cached profiles are reached via the LRU.
        self._init_hooks()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FilesystemProfileStore(root={str(self._root)!r})"

    # --- identity of the store --------------------------------------------

    @property
    def location(self) -> str:
        return str(self._root)

    @property
    def root(self) -> Path:
        """The profiles root. This backend is a directory, so never ``None``."""
        return self._root

    # --- hooks --------------------------------------------------------------

    def _live_profiles(self):
        return self._cache.values()

    def evict(self, ref: str) -> None:
        """Drop a cached profile so the next ``get`` reloads from disk.

        Bumps the write generation too. ``evict`` is what the API layer calls
        after every write, including one that arrived by a path this store
        never saw (an extracted archive swapped in over its directory), so
        treating it as "something changed" is what keeps the analytics honest
        without making that caller import them.
        """
        self._bump_generation()
        self._cache.pop(ref, None)
        self._vector_indexes.pop(ref, None)
        try:
            slug = self.resolve_slug(ref)
        except ProfileNotFoundError:
            return
        self._cache.pop(slug, None)
        self._vector_indexes.pop(slug, None)

    # --- enumeration and lookup ---------------------------------------------

    def list_slugs(self) -> list[str]:
        if not self._root.is_dir():
            return []
        out: list[str] = []
        for child in sorted(self._root.iterdir()):
            if child.name.startswith("."):
                # Hidden dirs are never profiles (the store's ``.cache/`` memo dir,
                # upload staging/backup dirs).
                continue
            if child.is_dir() and (child / "profile.jsonld").is_file():
                out.append(child.name)
        return out

    def resolve_slug(self, ref: str) -> str:
        """Map a profile reference to a directory name.

        ``ref`` is either the directory name itself or a ``rid`` (a canonical
        ORCID or a ``local:`` id). Directory-first, because that is the common
        case and a directory name can never be mistaken for a rid.
        """
        if (self._root / ref / "profile.jsonld").is_file():
            return ref
        mapped = self._rid_map().get(ref)
        if mapped is None:
            raise ProfileNotFoundError(
                f"nothing in {self._root} matches {ref!r}: no directory by that "
                f"name, and no profile whose rid is {ref!r}"
            )
        return mapped

    def rid_for(self, ref: str) -> str:
        """Map a slug or a rid to the rid. Raises :class:`ProfileNotFoundError`.

        A bare ORCID resolves here without any extra indexing: an ORCID rid
        *is* its ORCID (``scholarcore.identity.orcid_of`` derives one from the
        other and invents nothing), so it is already a key of the rid map.
        """
        slug = self.resolve_slug(ref)
        for rid, mapped in self._rid_map().items():
            if mapped == slug:
                return rid
        raise ProfileNotFoundError(
            f"profile {slug!r} in {self._root} has no readable rid in its profile.jsonld"
        )

    def path_for(self, ref: str) -> Path:
        """The absolute profile directory for a slug or a rid.

        Numpy-free, which is the point: ``rp where`` is a path lookup and used
        to import the whole vector stack to do it.
        """
        return self._root / self.resolve_slug(ref)

    def exists(self, ref: str) -> bool:
        try:
            self.resolve_slug(ref)
        except ProfileNotFoundError:
            return False
        return True

    def write_lookup_index(self) -> Optional[Path]:
        """Persist ``rid <-> slug`` into ``<root>/.cache/index.json``.

        A shell caller resolving a rid to a directory reads this, which is what
        lets a rid work anywhere a slug does, regardless of directory name.
        Nothing writes it implicitly: a caller that owns a writable root calls
        this when it wants the file fresh.
        """
        from ..utils.clock import now_iso
        from ..utils.paths import store_cache_dir

        d = store_cache_dir(self._root)
        if d is None:  # unreachable: this backend always has a root
            return None
        path = d / "index.json"
        by_rid = dict(self._rid_map())
        payload = {
            "version": 1,
            "computed_at": now_iso(),
            "root": str(self._root),
            "by_rid": by_rid,
            "by_slug": {slug: rid for rid, slug in by_rid.items()},
        }
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if current.get("by_rid") == by_rid and current.get("root") == str(self._root):
                    return path  # unchanged; don't churn mtime on every load
            except (OSError, json.JSONDecodeError):
                pass
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError:  # a read-only root must not break loading
            logger.warning("could not write registry index at %s", path)
        return path

    def _rid_map(self) -> dict[str, str]:
        """``rid -> directory name``, rebuilt when the root changes.

        Prefers ``<root>/.cache/index.json`` (written by
        :meth:`write_lookup_index`). Falls back to a text scan of each
        ``profile.jsonld``: that file is a cache, and a rid lookup must not
        depend on whether some other code path happened to refresh it first.

        This is also where a duplicate identity is caught, because it is the
        only place that sees every profile's rid at once. Two directories
        claiming one rid is a corrupt root, not a near miss: silently keeping
        the first would make which profile a rid resolves to depend on
        directory ordering.

        The index is used only when it names exactly the profiles on disk. A
        stale-but-non-empty index is worse than no index: it would silently
        hide a newly built profile's rid while still looking usable.
        """
        stamp = self._root_stamp()
        if self._rid_map_cache is not None and self._rid_map_stamp == stamp:
            return self._rid_map_cache
        mapping: dict[str, str] = {}
        index = self._root / STORE_CACHE_DIRNAME / "index.json"
        if index.is_file():
            try:
                data = json.loads(index.read_text(encoding="utf-8"))
                by_rid = data.get("by_rid")
                if isinstance(by_rid, dict):
                    mapping = {
                        str(k): str(v)
                        for k, v in by_rid.items()
                        if (self._root / str(v) / "profile.jsonld").is_file()
                    }
                    if set(mapping.values()) != set(self.list_slugs()):
                        mapping = {}  # stale: a profile appeared or vanished
            except (OSError, json.JSONDecodeError, TypeError):
                mapping = {}
        if not mapping:
            for slug in self.list_slugs():
                rid = _rid_from_document_text(self._root / slug / "profile.jsonld")
                if not rid:
                    continue
                prior = mapping.get(rid)
                if prior is not None:
                    raise DuplicateIdentityError(
                        f"rid {rid!r} is claimed by two profiles in {self._root}: "
                        f"{prior!r} and {slug!r}"
                    )
                mapping[rid] = slug
        self._rid_map_cache = mapping
        self._rid_map_stamp = stamp
        return mapping

    def _root_stamp(self) -> tuple:
        try:
            return (self._root.stat().st_mtime_ns, len(self.list_slugs()))
        except OSError:
            return (0, 0)

    def get(self, ref: str) -> ResearcherProfile:
        slug = self.resolve_slug(ref)
        if slug in self._cache:
            self._cache.move_to_end(slug)
            return self._cache[slug]
        return self._admit(slug, ResearcherProfile.from_files(self._root / slug))

    def _admit(self, slug: str, prof: ResearcherProfile) -> ResearcherProfile:
        """Attach the store's hooks and put ``prof`` in the LRU."""
        self._thread_hooks(prof)
        self._cache[slug] = prof
        while len(self._cache) > self.capacity:
            self._cache.popitem(last=False)
        return prof

    # --- mutation ------------------------------------------------------------

    def create(self, document: ProfileDocument, *, slug: str) -> ResearcherProfile:
        """Create ``<root>/<slug>/`` and persist ``document`` into it.

        One write unit of kind ``"create"``, so a host's pre-commit hook lands
        its own state in the same logical write. On the filesystem that unit is
        not atomic (``ctx.atomic`` is ``False``). A raising hook leaves the
        directory behind, which is why this backend removes it explicitly rather
        than pretending a rename undid anything.
        """
        target = self._root / slug
        if target.exists() or self.exists(document.rid):
            raise ProfileWriteError(
                str(target),
                f"cannot create {slug!r}: a profile with that slug or with rid "
                f"{document.rid!r} already exists in {self._root}",
            )
        target.mkdir(parents=True)
        try:
            prof = ResearcherProfile.from_files(target)
            # Seed the in-memory document before opening the unit so
            # ``WriteContext.rid`` is populated: the context is frozen, and a
            # create hook whose whole job is to write an ownership row keyed on
            # the rid cannot be handed an empty one. ``save_profile`` below is
            # still what persists, and reassigns this to the reparsed document.
            prof._metadata = document
            self._thread_hooks(prof)
            with prof.write_unit("create"):
                prof.save_profile(document)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        self._rid_map_stamp = None
        self._bump_generation()
        return self._admit(slug, prof)

    def put_document(self, slug: str, document: ProfileDocument) -> ResearcherProfile:
        """Create-or-replace a profile's canonical document only.

        If the profile exists, runs an ``"edit"`` write unit; if new, runs a
        ``"create"`` unit so a host's pre-commit hook can write ownership. The
        write uses the profile's :meth:`save_profile` so ``dateModified`` is
        stamped correctly and hooks fire.
        """
        target = self._root / slug
        is_create = not target.exists()

        if is_create:
            # Check that the rid is not already taken by another slug
            if self.exists(document.rid):
                raise ProfileWriteError(
                    str(target),
                    f"cannot create {slug!r}: a profile with rid "
                    f"{document.rid!r} already exists in {self._root}",
                )
            target.mkdir(parents=True)
            try:
                prof = ResearcherProfile.from_files(target)
                prof._metadata = document
                self._thread_hooks(prof)
                with prof.write_unit("create"):
                    prof.save_profile(document)
            except BaseException:
                shutil.rmtree(target, ignore_errors=True)
                raise
            self._rid_map_stamp = None
            self._bump_generation()
            return self._admit(slug, prof)

        # Profile exists: edit path
        prof = self.get(slug)
        # Validate rid consistency: the document's rid must match the existing
        existing_rid = prof.rid
        if document.rid != existing_rid:
            raise ProfileWriteError(
                str(target),
                f"cannot replace {slug!r}: document rid {document.rid!r} does not "
                f"match existing rid {existing_rid!r}",
            )
        prof.save_profile(document)
        return prof

    def delete(self, ref: str) -> str:
        slug = self.resolve_slug(ref)
        rid = self.get(slug).rid
        self.evict(slug)
        shutil.rmtree(self._root / slug, ignore_errors=True)
        self._rid_map_stamp = None
        self._bump_generation()
        return rid

    def commit_directory(
        self, slug: str, staging: Path, *, build_missing_index: bool = False
    ) -> IngestResult:
        """Validate a staged directory loads, then atomically swap it live.

        ``build_missing_index`` is off by default because the operator push
        (``PUT /api/v1/profiles/{slug}``) ships its own index and a rebuild
        there is wasted minutes; a host ingesting on a person's behalf, who
        wants their profile rankable immediately, turns it on. When it is on and
        ``.cache/embeddings.sqlite`` is absent, a best-effort build runs
        afterwards: a core-only install or a build failure leaves the profile
        committed but unindexed rather than raising. The profile is hosted,
        but not yet in ``/match``.
        """
        try:
            staged = ResearcherProfile.from_files(staging)
            name = staged.metadata.name
            level = str(staged.level)
            rid = getattr(staged, "rid", None)
        except (OSError, ValueError, ProfileError, ValidationError) as e:
            raise UploadError(f"staged profile failed to load: {e}") from e

        target = swap_profile_dir(self._root, slug, Path(staging))
        self.evict(slug)
        self._rid_map_stamp = None
        self._bump_generation()
        indexed = (cache_dir(target) / "embeddings.sqlite").is_file()
        if not indexed and build_missing_index:
            indexed = self._try_build_index(target)
        return IngestResult(slug=slug, rid=rid, name=name, level=level, indexed=indexed)

    @staticmethod
    def _try_build_index(profile_dir: Path) -> bool:
        try:
            from ..embeddings import build_index

            build_index(profile_dir)
        # Boundary: the whole embedding stack, including whatever a third-party
        # encoder backend raises. A missing extra, an absent backend, or a build
        # error all leave the profile committed but unindexed, never unhosted.
        except Exception:
            logger.warning("post-ingest index build failed for %s", profile_dir, exc_info=True)
        return (cache_dir(profile_dir) / "embeddings.sqlite").is_file()

    def export_directory(self, ref: str, dest: Path) -> Path:
        """Copy a profile directory to ``dest``. Build state is not copied.

        The build root is a sibling tree outside the content root, so a copy of
        the content directory is the published record and nothing else. That
        is the same thing ``SqlProfileStore.export_directory`` produces
        without ``with_build``.
        """
        slug = self.resolve_slug(ref)
        out = Path(dest).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        src = self._root / slug
        for item in src.rglob("*"):
            if not item.is_file():
                continue
            target = out / item.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
        return out

    # --- bytes ---------------------------------------------------------------

    def document_bytes(self, ref: str) -> bytes:
        slug = self.resolve_slug(ref)
        path = self._root / slug / "profile.jsonld"
        try:
            return path.read_bytes()
        except OSError as e:
            raise ProfileNotFoundError(f"could not read {path}: {e}") from e

    def artifact_bytes(self, ref: str, content_url: str) -> bytes:
        """Read one artifact, refusing anything that escapes the profile dir.

        The traversal check is here rather than at the call site, so every
        caller inherits it. A store that can be talked into reading
        ``../../etc/passwd`` has a bug in the store, not in one route.
        """
        slug = self.resolve_slug(ref)
        profile_root = (self._root / slug).resolve()
        path = (profile_root / content_url).resolve()
        if profile_root not in path.parents:
            raise ProfileNotFoundError(f"artifact {content_url!r} is not inside profile {slug!r}")
        try:
            return path.read_bytes()
        except OSError as e:
            raise ProfileNotFoundError(
                f"artifact {content_url!r} of profile {slug!r} could not be read: {e}"
            ) from e

    def content_hash(self, ref: str) -> str:
        """Recomputed per call: this backend stores no digest column.

        Same two-artifact, NUL-separated surface every backend reports; see
        :meth:`researcher_profiles.profile.ResearcherProfile.content_hash`.
        """
        return self.get(ref).content_hash()

    # --- vectors (the ``VectorStore`` capability) ----------------------------

    @property
    def backend_spec(self) -> str | None:
        """The first readable backend name across the root, sqlite or flat.

        First, not a consensus: a root whose profiles disagree is already
        broken (cosine across models is meaningless), and this is the same
        answer the registry has always taken. The published flat form is
        checked too, so a directory holding only downloaded profiles still
        reports a space.
        """
        from ..embeddings.cache import index_backend_name

        for slug in self.list_slugs():
            d = self._root / slug
            if (cache_dir(d) / "embeddings.sqlite").is_file():
                name = index_backend_name(d)
                if name:
                    return name
            flat = d / "embeddings" / "index.json"
            if flat.is_file():
                try:
                    spec = json.loads(flat.read_text(encoding="utf-8")).get("backend_spec")
                except (OSError, json.JSONDecodeError, AttributeError):
                    continue
                if spec:
                    return str(spec)
        return None

    def has_vector_index(self, ref: str) -> bool:
        """Whether the profile has actual vectors, not just an empty DB."""
        try:
            slug = self.resolve_slug(ref)
        except ProfileNotFoundError:
            return False
        d = self._root / slug
        db_path = cache_dir(d) / "embeddings.sqlite"
        if db_path.is_file():
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    row = conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone()
                    return row is not None
                finally:
                    conn.close()
            except (sqlite3.Error, OSError):
                return False
        return (d / "embeddings" / "index.json").is_file()

    def vector_index(self, ref: str) -> "VectorIndex":
        """The profile's vectors: the build-local sqlite, else the served flat form.

        sqlite first because it is the richer index: it carries every chunk,
        including the restricted ones the public export drops, and its hits
        carry text. A directory that only holds a published profile (no
        ``.cache/``) still ranks, at the public subset, through the flat form.

        The imports are inside the method. This module is on the core import
        path and both readers pull numpy.
        """
        slug = self.resolve_slug(ref)
        cached = self._vector_indexes.get(slug)
        if cached is not None:
            return cached

        from ..embeddings._sqlite import IndexNotBuiltError
        from ..embeddings.cache import SqliteEmbeddingIndex
        from ..embeddings.flat import FlatEmbeddingIndex

        d = self._root / slug
        index: "VectorIndex"
        if (cache_dir(d) / "embeddings.sqlite").is_file():
            index = SqliteEmbeddingIndex(d, profile_document=self.get(slug).metadata)
        elif (d / "embeddings" / "index.json").is_file():
            index = FlatEmbeddingIndex.load(d / "embeddings")
        else:
            raise IndexNotBuiltError(
                f"profile {slug!r} in {self._root} has no vectors: neither "
                f".cache/embeddings.sqlite nor embeddings/index.json. Run "
                f"`rp index {slug}`."
            )
        self._vector_indexes[slug] = index
        return index

    def centroid(self, ref: str) -> "np.ndarray":
        """The profile's centroid, from whichever index :meth:`vector_index` picked."""
        return self.vector_index(ref).centroid()

    def centroids_matrix(self) -> "tuple[list[str], np.ndarray] | None":
        """``None``: a directory holds no stacked matrix, only per-profile vectors.

        ``<root>/.cache/centroids.npz`` is not it. That file is the memo
        ``store.centroids`` keeps of its own computed matrix; returning it here
        would make the manager read its own cache back through the store and
        call the result authoritative.
        """
        return None
