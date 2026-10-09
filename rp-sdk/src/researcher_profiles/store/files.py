"""``FilesystemProfileStore``: profiles as directories under one root.

Core-only: nothing here imports FastAPI or SQLModel. ``api.upload`` is imported
inside :meth:`FilesystemProfileStore.commit_directory` because importing it at
module scope would pull FastAPI onto the core import path.
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

#: The ``"rid"`` member of profile.jsonld. A text scan, not a JSON parse: it runs
#: over every profile in the root, and a document may inline the works list.
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
    final profile path. Covers the directory swap only; it is not a transaction.
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


def _strip_staged_document(staging: Path) -> None:
    """Drop registry-issued proofs from a staged ``profile.jsonld`` before it goes live.

    A staged directory never passes ``save_document``, so it needs its own
    strip. Untouched when there is nothing to drop, so pushed bytes stay
    verbatim.
    """
    from ..profile.storage import persistable_document
    from ..schema.jsonld import canonical_dumps

    path = staging / "profile.jsonld"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # the load below reports an unreadable document
    stripped = persistable_document(data, where=str(path))
    if stripped is not data:
        path.write_text(canonical_dumps(stripped), encoding="utf-8")


class FilesystemProfileStore(_HookedStore, _AnalyticsAccessors):
    """Profiles as directories under one root, with a small LRU over the
    loaded :class:`~researcher_profiles.profile.ResearcherProfile` objects.

    Eviction only drops the reference; sqlite handles are GC'd with the index.
    """

    def __init__(self, root: str | os.PathLike, capacity: int = 32):
        self._root = Path(root).expanduser().resolve()
        self.capacity = capacity
        self._cache: OrderedDict[str, ResearcherProfile] = OrderedDict()
        self._rid_map_cache: dict[str, str] | None = None
        self._rid_map_stamp: tuple | None = None
        #: slug -> the profile's open vector index. Separate from the profile
        #: LRU because an index is expensive to reopen.
        self._vector_indexes: dict[str, "VectorIndex"] = {}
        self._init_hooks()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FilesystemProfileStore(root={str(self._root)!r})"

    # --- identity of the store --------------------------------------------

    @property
    def location(self) -> str:
        return str(self._root)

    @property
    def root(self) -> Path:
        """The profiles root. Never ``None``."""
        return self._root

    # --- hooks --------------------------------------------------------------

    def _live_profiles(self):
        return self._cache.values()

    def evict(self, ref: str) -> None:
        """Drop a cached profile so the next ``get`` reloads from disk.

        Bumps the write generation too: the API layer calls it after writes
        this store never saw (an archive swapped in over its directory).
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

        Directory name first; a directory name can never be mistaken for a rid.
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
        """See :meth:`~.protocol.ProfileStore.rid_for`."""
        slug = self.resolve_slug(ref)
        for rid, mapped in self._rid_map().items():
            if mapped == slug:
                return rid
        raise ProfileNotFoundError(
            f"profile {slug!r} in {self._root} has no readable rid in its profile.jsonld"
        )

    def path_for(self, ref: str) -> Path:
        """The absolute profile directory for a slug or a rid. Numpy-free."""
        return self._root / self.resolve_slug(ref)

    def exists(self, ref: str) -> bool:
        try:
            self.resolve_slug(ref)
        except ProfileNotFoundError:
            return False
        return True

    def rids_with_email(self, email: str) -> list[str]:
        """rids whose document ``email`` equals ``email`` (case and outer space ignored).

        Loads every profile, which is fine for a directory corpus.
        """
        needle = (email or "").strip().lower()
        if not needle:
            return []
        out = []
        for slug in self.list_slugs():
            meta = self.get(slug).metadata
            if (meta.email or "").strip().lower() == needle:
                out.append(meta.rid)
        return sorted(out)

    def successor_of(self, ref: str) -> Optional[str]:  # noqa: ARG002
        """Always ``None``: a directory store keeps no aliases (it cannot merge)."""
        return None

    def alias_slugs(self) -> set[str]:
        """Always empty: a directory store keeps no aliases."""
        return set()

    def merge_into(self, retired_ref: str, staging: Path, **kwargs) -> IngestResult:  # noqa: ARG002
        raise NotImplementedError("merge needs the SQL store")

    def write_lookup_index(self) -> Optional[Path]:
        """Persist ``rid <-> slug`` into ``<root>/.cache/index.json``."""
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

        Prefers ``<root>/.cache/index.json``, but only when it names exactly
        the profiles on disk (a stale index would hide a new profile's rid);
        else a text scan of each ``profile.jsonld``.

        Raises :class:`DuplicateIdentityError` when two directories claim one
        rid, rather than letting directory order pick the winner.
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

        The write unit is not atomic here (``ctx.atomic`` is ``False``), so a
        raising hook's directory is removed explicitly.
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
            # Seed the in-memory document so the frozen ``WriteContext.rid`` is
            # populated for create hooks. ``save_profile`` still persists.
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

    def create_bundle(
        self,
        document: ProfileDocument,
        *,
        slug: str,
        expertise: str | None = None,
        soul: str | None = None,
        artifacts: dict[str, str] | None = None,
    ) -> ResearcherProfile:
        """Create ``<root>/<slug>/`` with its document and every artifact.

        Not atomic here; a failure part-way removes the whole directory.
        """
        target = self._root / slug
        if target.exists() or self.exists(document.rid):
            raise ProfileWriteError(
                str(target),
                f"cannot create {slug!r}: a profile with that slug or with rid "
                f"{document.rid!r} already exists in {self._root}",
            )
        declared = {p.content_url: p for p in (*document.has_part, *document.subject_of)}
        for content_url in artifacts or {}:
            if content_url not in declared:
                raise ProfileWriteError(
                    str(target), f"artifact {content_url!r} is not in the document manifest"
                )
        target.mkdir(parents=True)
        try:
            prof = ResearcherProfile.from_files(target)
            # See ``create``.
            prof._metadata = document
            self._thread_hooks(prof)
            with prof.write_unit("create"):
                prof.save_profile(document)
                if expertise is not None:
                    prof.save_expertise(expertise)
                if soul is not None:
                    prof.save_soul(soul)
                for content_url, text in (artifacts or {}).items():
                    part = declared[content_url]
                    prof.storage.write_artifact(
                        content_url,
                        text,
                        role=part.role or "artifact",
                        name=part.name or content_url,
                        encoding_format=part.encoding_format or "text/plain",
                        manifest_slot=("subjectOf" if part in document.subject_of else "hasPart"),
                    )
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        self._rid_map_stamp = None
        self._bump_generation()
        return self._admit(slug, prof)

    def put_document(self, slug: str, document: ProfileDocument) -> ResearcherProfile:
        """See :meth:`~.protocol.ProfileStore.put_document`."""
        target = self._root / slug
        is_create = not target.exists()

        if is_create:
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

        prof = self.get(slug)
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

        With ``build_missing_index`` and no ``.cache/embeddings.sqlite``, a
        best-effort build runs afterwards; failure leaves the profile committed
        but unindexed rather than raising.
        """
        _strip_staged_document(Path(staging))
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
        # Boundary: anything the embedding stack raises leaves the profile
        # committed but unindexed.
        except Exception:
            logger.warning("post-ingest index build failed for %s", profile_dir, exc_info=True)
        return (cache_dir(profile_dir) / "embeddings.sqlite").is_file()

    def export_directory(self, ref: str, dest: Path) -> Path:
        """Copy a profile directory to ``dest``. Build state is not copied."""
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

    def held_artifacts(self, ref: str) -> dict[str, int | None]:
        """Manifest entries whose file exists under the profile directory, with its size."""
        slug = self.resolve_slug(ref)
        profile_root = (self._root / slug).resolve()
        out: dict[str, int | None] = {}
        for part in self.get(slug).manifest():
            path = profile_root / part.content_url
            try:
                out[part.content_url] = path.stat().st_size if path.is_file() else None
            except OSError:
                continue
            if out[part.content_url] is None:
                del out[part.content_url]
        return out

    def artifact_bytes(self, ref: str, content_url: str) -> bytes:
        """Read one artifact, refusing anything that escapes the profile dir.

        The traversal check lives in the store so every caller inherits it.
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
        """Recomputed per call: this backend stores no digest column."""
        return self.get(ref).content_hash()

    # --- vectors (the ``VectorStore`` capability) ----------------------------

    @property
    def backend_spec(self) -> str | None:
        """The first readable backend name across the root, sqlite or flat.

        First, not a consensus: a root whose profiles disagree is already
        broken (cosine across models is meaningless).
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

        sqlite first because it carries every chunk (including private ones)
        and text. Imports are deferred because both readers pull numpy.
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

        ``<root>/.cache/centroids.npz`` is ``store.centroids``'s own memo, not
        an authoritative matrix.
        """
        return None
