"""Per-profile sqlite-vec embedding store.

The DB lives at ``<profile>/.cache/embeddings.sqlite`` so it travels with
the profile. There is no migration code: incompatible existing DBs are
dropped and recreated when ``build_index(force=True)`` is called.
"""

import hashlib
import json
import logging
import os
import sqlite3
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..utils.clock import now_iso
from ..utils.const import DEFAULT_BACKEND_SPEC, EMBEDDING_PROBE_TEXT, INDEX_SCHEMA_VERSION
from ..utils.paths import cache_dir
from ._sqlite import (
    IndexNotBuiltError,
    connect_vec,
    deserialize_vec,
    ensure_schema,
    read_index_meta,
    serialize_vec,
    write_index_meta,
)
from .backends import EmbeddingBackend, get_backend
from .chunking import (
    PAPER_CHUNK_TYPES,
    Chunk,
    chunk_abstract,
    chunk_cv,
    chunk_expertise,
    chunk_grant,
    chunk_soul,
    chunk_summary,
    chunk_web,
)

logger = logging.getLogger(__name__)

_BATCH = 64

#: One chunk awaiting embedding: ``(chunk, text_hash, existing_id_to_replace)``.
#: A ``None`` id means the chunk is new rather than changed.
_PendingChunk = tuple[Chunk, str, int | None]


class IndexBackendMismatchError(RuntimeError):
    """Raised when the existing index was built with a different backend.

    Pass ``force=True`` to drop and recreate.
    """


@dataclass
class IndexReport:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    removed: int = 0
    backend_name: str = ""
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IndexStats:
    """One profile's embedding-index facts, gathered in a single open.

    The typed form of what a coverage report needs to know about an index
    without opening it itself.
    """

    exists: bool = False
    n_chunks: int = 0
    n_papers: int = 0
    backend_name: str = ""
    last_built_at: str | None = None
    mtime: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchHit:
    text: str
    source_type: str
    source_id: str
    chunk_index: int
    section: str | None
    cosine: float
    score: float
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine distance in the same convention sqlite-vec uses (1 - cos_sim, range 0..2)."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = (na**0.5) * (nb**0.5)
    if denom == 0.0:
        return 1.0
    return 1.0 - (dot / denom)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_frontmatter(text: str) -> str:
    """Drop a leading ``---`` YAML frontmatter block, if present."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + len("\n---") :].lstrip("\n")


# Shared vec0 nearest-neighbour SELECT used by both search paths. Params: (blob, k).
_SEARCH_SELECT = (
    "SELECT c.id, c.source_type, c.source_id, c.chunk_index, c.section, "
    "c.text, c.meta, v.distance, v.embedding "
    "FROM chunk_vec v JOIN chunks c ON c.id = v.id "
    "WHERE v.embedding MATCH ? AND k = ? "
    "ORDER BY v.distance"
)


def _rows_to_hits(rows, query_vec, k, *, skip=None) -> list["SearchHit"]:
    """Map ``_SEARCH_SELECT`` rows to ``SearchHit``s, recomputing NULL distances.

    ``skip(source_type, source_id, chunk_index)`` drops matching rows before
    they count toward ``k``. ``query_vec`` is used only when vec0 returns a
    NULL distance and the score must be recomputed in Python.
    """
    hits: list[SearchHit] = []
    for row in rows:
        _id, stype, sid, cidx, section, text, meta_str, distance, emb_blob = row
        if skip is not None and skip(stype, sid, cidx):
            continue
        if distance is None:
            # vec0 occasionally returns NULL distance for valid rows.
            # Recompute in Python so users still get k results back.
            logger.debug(
                "vec0 returned NULL distance for chunk_id=%s; recomputing in Python.",
                _id,
            )
            try:
                distance = _cosine_distance(query_vec, deserialize_vec(emb_blob))
            except (ValueError, TypeError, struct.error):
                logger.warning(
                    "could not recompute a distance for chunk_id=%s; dropping the row",
                    _id,
                    exc_info=True,
                )
                continue
        cosine = 1.0 - float(distance)
        score = max(0.0, 0.5 * (1.0 + cosine))
        meta = {}
        if meta_str:
            try:
                meta = json.loads(meta_str)
            except json.JSONDecodeError:
                meta = {}
        hits.append(
            SearchHit(
                text=text,
                source_type=stype,
                source_id=sid,
                chunk_index=cidx,
                section=section,
                cosine=round(cosine, 4),
                score=round(score, 4),
                meta=meta,
            )
        )
        if len(hits) >= k:
            break
    return hits


class SqliteEmbeddingIndex:
    """sqlite-vec index for a single profile."""

    SCHEMA_VERSION = INDEX_SCHEMA_VERSION

    def __init__(self, profile_dir: str | os.PathLike, *, profile_document: Any = None):
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        if not self.profile_dir.is_dir():
            raise FileNotFoundError(f"profile dir not found: {self.profile_dir}")
        # Serve-time derived index: profile-adjacent cache, not build state.
        self.index_dir = cache_dir(self.profile_dir)
        self.db_path = self.index_dir / "embeddings.sqlite"
        self._profile_document = profile_document
        self._cached_backend: EmbeddingBackend | None = None

    def _level(self) -> str:
        """The profile's depth tier.

        Prefers the already-loaded document; falls back to a direct read of
        ``profile.jsonld`` so ``SqliteEmbeddingIndex(path)`` (the CLI's entry point)
        indexes a lite profile correctly without the caller doing the load.
        """
        level = getattr(self._profile_document, "level", None)
        if level:
            return str(level)
        path = self.profile_dir / "profile.jsonld"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return "full"
            if isinstance(data, dict) and data.get("level"):
                return str(data["level"])
        return "full"

    # ------------------------------------------------------------------
    # Backend resolution
    # ------------------------------------------------------------------

    def _resolve_backend_spec(
        self, override: EmbeddingBackend | str | None
    ) -> tuple[str | None, EmbeddingBackend | None]:
        env_override = os.environ.get("RESEARCHER_PROFILES_EMBEDDING_BACKEND")
        if isinstance(override, str):
            return None, get_backend(override)
        if override is not None:
            return None, override
        if env_override:
            return None, get_backend(env_override)
        # From profile config
        if self._profile_document is not None:
            spec = getattr(self._profile_document, "embedding_backend", None)
            if spec is None and hasattr(self._profile_document, "model_extra"):
                extra = self._profile_document.model_extra or {}
                spec = extra.get("embedding_backend")
            if spec:
                return None, get_backend(spec)
        return DEFAULT_BACKEND_SPEC, None

    # ------------------------------------------------------------------
    # Source enumeration
    # ------------------------------------------------------------------

    def _enumerate_chunks(self) -> list[Chunk]:
        # A "lite" profile has no personality/ or summaries/ on disk; its index
        # is built over paper abstracts instead.
        level = self._level()
        if level == "lite":
            return self._enumerate_abstract_chunks()

        out: list[Chunk] = []
        expertise_path = self.profile_dir / "personality" / "expertise.md"
        if expertise_path.is_file():
            out.extend(chunk_expertise(expertise_path.read_text(encoding="utf-8")))
        soul_path = self.profile_dir / "personality" / "SOUL.md"
        if soul_path.is_file():
            out.extend(chunk_soul(soul_path.read_text(encoding="utf-8")))
        summaries_dir = self.profile_dir / "sources" / "summaries"
        if summaries_dir.is_dir():
            for f in sorted(summaries_dir.iterdir()):
                if not (f.is_file() and f.name.endswith(".summary.md")):
                    continue
                paper_id = f.name[: -len(".summary.md")]
                text = f.read_text(encoding="utf-8")
                out.extend(chunk_summary(text, paper_id))
        if level == "deep":
            out.extend(self._enumerate_deep_source_chunks())
        return out

    def _enumerate_deep_source_chunks(self) -> list[Chunk]:
        """Enumerate grant/cv/web chunks for a ``deep`` profile.

        Each source class is optional on disk (presence-conditional): grants
        from ``sources/grants.jsonld``, the CV from ``sources/cv.md``, web pages
        from ``sources/web/*.md``. Provenance frontmatter on cv/web files is
        stripped before chunking.
        """
        out: list[Chunk] = []
        grants_path = self.profile_dir / "sources" / "grants.jsonld"
        if grants_path.is_file():
            data = json.loads(grants_path.read_text(encoding="utf-8"))
            entries = data.get("hasPart", []) or [] if isinstance(data, dict) else []
            for i, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                grant_id = str(entry.get("id") or f"grant-{i}")
                out.extend(
                    chunk_grant(
                        str(entry.get("name") or ""),
                        entry.get("abstract"),
                        grant_id,
                    )
                )
        cv_path = self.profile_dir / "sources" / "cv.md"
        if cv_path.is_file():
            out.extend(chunk_cv(_strip_frontmatter(cv_path.read_text(encoding="utf-8"))))
        web_dir = self.profile_dir / "sources" / "web"
        if web_dir.is_dir():
            for f in sorted(web_dir.iterdir()):
                if not (f.is_file() and f.suffix == ".md"):
                    continue
                out.extend(chunk_web(_strip_frontmatter(f.read_text(encoding="utf-8")), f.stem))
        return out

    def _enumerate_abstract_chunks(self) -> list[Chunk]:
        """Enumerate ``paper_abstract`` chunks from ``sources/papers.jsonld``.

        Used for ``lite`` profiles, which never build summaries or personality
        docs. Skips papers with no abstract and papers the build state flags
        contaminated (contamination is build bookkeeping, not published record).
        """
        from ..build_state import BuildState

        papers_path = self.profile_dir / "sources" / "papers.jsonld"
        if not papers_path.is_file():
            return []
        data = json.loads(papers_path.read_text(encoding="utf-8"))
        entries = data.get("hasPart", []) or [] if isinstance(data, dict) else []
        state = BuildState.load(self.profile_dir)
        out: list[Chunk] = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            paper_id = entry.get("paper_id") or entry.get("openalex_id") or f"paper-{i}"
            if state.is_contaminated(entry.get("paper_id")):
                continue
            abstract = (entry.get("abstract") or "").strip()
            if not abstract:
                continue
            out.extend(chunk_abstract(abstract, str(paper_id)))
        return out

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _prepare_db(self, be: EmbeddingBackend, *, force: bool) -> dict[str, str]:
        """Validate any existing index against ``be`` and return its meta.

        Must run before tables are created, so a mismatched dim never reaches
        ``ensure_schema``. Raises :class:`IndexBackendMismatchError` on a
        backend or dim mismatch unless ``force``, which unlinks the DB and
        returns an empty meta instead.
        """
        existing_meta: dict[str, str] = {}
        if self.db_path.exists():
            conn = connect_vec(self.db_path, create_parents=True)
            try:
                existing_meta = read_index_meta(conn)
            finally:
                conn.close()
        if not existing_meta:
            return {}

        existing_backend = existing_meta.get("backend_name")
        existing_dim = existing_meta.get("embedding_dim")
        mismatch = (existing_backend and existing_backend != be.name) or (
            existing_dim and int(existing_dim) != int(be.dim)
        )
        if not mismatch:
            return existing_meta
        if not force:
            raise IndexBackendMismatchError(
                f"Index was built with backend={existing_backend!r} "
                f"(dim={existing_dim}); requested backend={be.name!r} "
                f"(dim={be.dim}). Pass force=True to rebuild."
            )
        # Force: drop and recreate from scratch.
        self.db_path.unlink()
        return {}

    def _ensure_probe_vector(self, conn: sqlite3.Connection, be: EmbeddingBackend) -> None:
        """Cache the probe vector in ``index_meta`` while the model is resident."""
        if read_index_meta(conn).get("probe_vector"):
            return
        probe_vec = be.embed([EMBEDDING_PROBE_TEXT])[0]
        write_index_meta(
            conn,
            {
                "probe_text": EMBEDDING_PROBE_TEXT,
                "probe_vector": json.dumps([float(v) for v in probe_vec]),
            },
        )

    def _diff_chunks(
        self, conn: sqlite3.Connection, chunks: list[Chunk]
    ) -> tuple[list[_PendingChunk], int]:
        """Split ``chunks`` into work to do and work already done.

        Returns the pending chunks and the count of unchanged ones, so the
        caller owns the report rather than this method mutating it.
        """
        existing_rows: dict[tuple[str, str, int], tuple[int, str]] = {}
        for row in conn.execute(
            "SELECT id, source_type, source_id, chunk_index, text_hash FROM chunks"
        ):
            existing_rows[(row[1], row[2], row[3])] = (row[0], row[4])

        to_embed: list[_PendingChunk] = []
        skipped = 0
        for ch in chunks:
            key = (ch.source_type, ch.source_id, ch.chunk_index)
            h = _sha256(ch.embed_text)
            existing = existing_rows.get(key)
            if existing is None:
                to_embed.append((ch, h, None))
            elif existing[1] == h:
                skipped += 1
            else:
                to_embed.append((ch, h, existing[0]))
        return to_embed, skipped

    def _embed_and_write(
        self,
        conn: sqlite3.Connection,
        be: EmbeddingBackend,
        to_embed: list[_PendingChunk],
        report: IndexReport,
    ) -> None:
        """Embed the pending chunks in batches and write them, committing per batch."""
        now = now_iso()
        for i in range(0, len(to_embed), _BATCH):
            batch = to_embed[i : i + _BATCH]
            vecs = be.embed([c.embed_text for c, _, _ in batch])
            if len(vecs) != len(batch):
                raise RuntimeError(f"backend returned {len(vecs)} vectors for {len(batch)} inputs")
            for (ch, h, existing_id), vec in zip(batch, vecs):
                if existing_id is not None:
                    conn.execute("DELETE FROM chunks WHERE id = ?", (existing_id,))
                    conn.execute("DELETE FROM chunk_vec WHERE id = ?", (existing_id,))
                    report.updated += 1
                else:
                    report.added += 1
                cur = conn.execute(
                    """
                    INSERT INTO chunks (
                        source_type, source_id, chunk_index, text, text_hash,
                        section, char_count, indexed_at, meta
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ch.source_type,
                        ch.source_id,
                        ch.chunk_index,
                        ch.text,
                        h,
                        ch.section,
                        len(ch.text),
                        now,
                        json.dumps(ch.meta) if ch.meta else "",
                    ),
                )
                conn.execute(
                    "INSERT INTO chunk_vec (id, embedding) VALUES (?, ?)",
                    (cur.lastrowid, serialize_vec(vec)),
                )
            conn.commit()

    def _prune_stale(
        self, conn: sqlite3.Connection, chunks: list[Chunk], report: IndexReport
    ) -> None:
        """Drop rows whose source artifact is gone, and stale indices within kept sources."""
        present_sources = {(c.source_type, c.source_id) for c in chunks}
        db_sources = set()
        for row in conn.execute("SELECT DISTINCT source_type, source_id FROM chunks"):
            db_sources.add((row[0], row[1]))
        for st, sid in db_sources - present_sources:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM chunks WHERE source_type = ? AND source_id = ?",
                    (st, sid),
                )
            ]
            for cid in ids:
                conn.execute("DELETE FROM chunks WHERE id = ?", (cid,))
                conn.execute("DELETE FROM chunk_vec WHERE id = ?", (cid,))
            report.removed += 1
        for st, sid in present_sources:
            kept_indices = {
                c.chunk_index for c in chunks if (c.source_type, c.source_id) == (st, sid)
            }
            for row in conn.execute(
                "SELECT id, chunk_index FROM chunks WHERE source_type = ? AND source_id = ?",
                (st, sid),
            ).fetchall():
                if row[1] not in kept_indices:
                    conn.execute("DELETE FROM chunks WHERE id = ?", (row[0],))
                    conn.execute("DELETE FROM chunk_vec WHERE id = ?", (row[0],))
                    report.removed += 1
        conn.commit()

    def build_index(
        self,
        *,
        force: bool = False,
        backend: EmbeddingBackend | str | None = None,
    ) -> IndexReport:
        if os.environ.get("RESEARCHER_PROFILES_DISABLE_INDEX") == "1":
            logger.info("RESEARCHER_PROFILES_DISABLE_INDEX=1; build_index no-op")
            return IndexReport(backend_name="disabled")

        start = time.monotonic()

        spec, instance = self._resolve_backend_spec(backend)
        be = instance if instance is not None else get_backend(spec)
        self._cached_backend = be

        chunks = self._enumerate_chunks()
        if not chunks and not self.db_path.exists():
            logger.info(
                "No chunks to index for %s and no existing DB; skipping",
                self.profile_dir.name,
            )
            return IndexReport(backend_name=be.name)

        existing_meta = self._prepare_db(be, force=force)
        conn = connect_vec(self.db_path, create_parents=True)
        report = IndexReport(backend_name=be.name)
        try:
            ensure_schema(conn, dim=be.dim)
            if force and existing_meta:
                # Same backend, but caller wants a clean rebuild.
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM chunk_vec")
                conn.commit()

            write_index_meta(
                conn,
                {
                    "schema_version": self.SCHEMA_VERSION,
                    "backend_name": be.name,
                    "embedding_dim": str(be.dim),
                    "created_at": existing_meta.get("created_at", now_iso()),
                },
            )
            self._ensure_probe_vector(conn, be)

            to_embed, skipped = self._diff_chunks(conn, chunks)
            report.skipped = skipped
            self._embed_and_write(conn, be, to_embed, report)
            self._prune_stale(conn, chunks, report)

            write_index_meta(conn, {"last_built_at": now_iso()})
        finally:
            conn.close()

        report.duration_s = round(time.monotonic() - start, 3)
        logger.info(
            "Indexed profile %s: added=%d updated=%d skipped=%d removed=%d backend=%s duration=%.2fs",
            self.profile_dir.name,
            report.added,
            report.updated,
            report.skipped,
            report.removed,
            report.backend_name,
            report.duration_s,
        )
        return report

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Facts about the index file (no sqlite-vec required)
    # ------------------------------------------------------------------

    def exists(self) -> bool:
        """True when ``embeddings.sqlite`` is present."""
        return self.db_path.is_file()

    def mtime(self) -> float | None:
        """The index file's mtime, or ``None`` when it is absent."""
        try:
            return self.db_path.stat().st_mtime
        except OSError:
            return None

    def index_meta(self) -> dict[str, str]:
        """``index_meta`` as a dict; ``{}`` when there is no readable index."""
        return _read_meta(self.db_path)

    @property
    def backend_spec(self) -> str:
        """The embedding model this index was built with; ``""`` when unbuilt.

        Half of the :class:`~researcher_profiles.embeddings.protocol.VectorIndex`
        contract, and the same field ``embeddings/index.json`` publishes under
        that name.
        """
        return self.index_meta().get("backend_name", "")

    def centroid(self):
        """The L2-normalized mean of every chunk vector, npz-cached.

        The other half of ``VectorIndex``. Delegates to
        :func:`~researcher_profiles.embeddings.profile_vec.index_centroid`,
        which is also what ``prof.index.embedding("centroid")`` reaches, so a
        centroid read through a store and one read through a profile are the
        same number out of the same cache. Imported inside the method: numpy is
        the ``vectors`` extra and the facts-about-the-file half of this class
        stays usable without it.
        """
        from .profile_vec import index_centroid

        return index_centroid(self.db_path, self.index_dir / "profile_vec.npz")

    def counts(self) -> tuple[int, int]:
        """``(n_chunks, n_papers)``.

        Papers counts DISTINCT ``source_id`` over :data:`PAPER_CHUNK_TYPES`,
        so a lite profile's abstracts count as papers.
        """
        if not self.db_path.is_file():
            return (0, 0)
        try:
            conn = sqlite3.connect(str(self.db_path))
        except sqlite3.Error:
            return (0, 0)
        try:
            try:
                row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
                n_chunks = int(row[0]) if row else 0
            except sqlite3.Error:
                n_chunks = 0
            placeholders = ", ".join("?" for _ in PAPER_CHUNK_TYPES)
            try:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT source_id) FROM chunks "
                    f"WHERE source_type IN ({placeholders})",
                    PAPER_CHUNK_TYPES,
                ).fetchone()
                n_papers = int(row[0]) if row else 0
            except sqlite3.Error:
                n_papers = 0
            return (n_chunks, n_papers)
        finally:
            conn.close()

    def stats(self) -> IndexStats:
        """``exists`` + :meth:`counts` + :meth:`index_meta` + :meth:`mtime`."""
        if not self.db_path.is_file():
            return IndexStats()
        n_chunks, n_papers = self.counts()
        meta = self.index_meta()
        return IndexStats(
            exists=True,
            n_chunks=n_chunks,
            n_papers=n_papers,
            backend_name=meta.get("backend_name", ""),
            last_built_at=meta.get("last_built_at"),
            mtime=self.mtime(),
        )

    def _open_for_search(self) -> tuple[sqlite3.Connection, EmbeddingBackend]:
        if not self.db_path.exists():
            raise IndexNotBuiltError(f"No index at {self.db_path}. Call build_index() first.")
        conn = connect_vec(self.db_path, create_parents=True)
        meta = read_index_meta(conn)
        if not meta.get("backend_name"):
            conn.close()
            raise IndexNotBuiltError(
                f"Index at {self.db_path} is missing backend metadata; rebuild it."
            )
        # Reuse the cached backend if it matches the stored name (avoids
        # re-instantiating heavy models, and lets tests use FakeBackend).
        if self._cached_backend is not None and self._cached_backend.name == meta.get(
            "backend_name"
        ):
            be = self._cached_backend
        else:
            try:
                be = get_backend(meta["backend_name"])
            # Resource boundary, not a swallow: any backend construction failure
            # is re-raised, this only stops the open connection leaking on the
            # way out.
            except Exception:
                logger.debug(
                    "backend %r could not be constructed for %s",
                    meta.get("backend_name"),
                    self.db_path,
                    exc_info=True,
                )
                conn.close()
                raise
            self._cached_backend = be
        # Override dim from stored value (don't trust backend table)
        try:
            be.dim = int(meta["embedding_dim"])
        except (KeyError, ValueError, TypeError):
            pass
        return conn, be

    def search(
        self,
        query: str,
        k: int = 5,
        filter: dict | None = None,
    ) -> list[SearchHit]:
        if not query or not query.strip():
            return []
        conn, be = self._open_for_search()
        try:
            vec = be.embed([query])[0]
            blob = serialize_vec(vec)

            type_filter: list[str] | None = None
            if filter:
                raw = filter.get("source_type")
                if raw is not None:
                    if isinstance(raw, str):
                        type_filter = [raw]
                    else:
                        type_filter = list(raw)

            # If filtering, fetch more than k from vec0 and post-filter.
            fetch_k = k * 4 if type_filter else k
            rows = conn.execute(_SEARCH_SELECT, (blob, fetch_k)).fetchall()
            skip = None
            if type_filter:

                def skip(st, si, ci):  # noqa: ARG001 - fixed callback signature
                    return st not in type_filter

            return _rows_to_hits(rows, vec, k, skip=skip)
        finally:
            conn.close()

    def search_similar(
        self,
        source_type: str,
        source_id: str,
        chunk_index: int = 0,
        k: int = 5,
    ) -> list[SearchHit]:
        conn, _be = self._open_for_search()
        try:
            row = conn.execute(
                "SELECT v.embedding FROM chunk_vec v JOIN chunks c ON c.id = v.id "
                "WHERE c.source_type = ? AND c.source_id = ? AND c.chunk_index = ?",
                (source_type, source_id, chunk_index),
            ).fetchone()
            if not row:
                return []
            blob = row[0]
            query_vec = deserialize_vec(blob)
            fetch_k = k + 1
            rows = conn.execute(_SEARCH_SELECT, (blob, fetch_k)).fetchall()
            return _rows_to_hits(
                rows,
                query_vec,
                k,
                skip=lambda st, si, ci: (st, si, ci) == (source_type, source_id, chunk_index),
            )
        finally:
            conn.close()


def index_backend_name(profile_dir: str | os.PathLike) -> str:
    """``index_meta.backend_name`` for a profile dir, or ``""``.

    Module-level so a caller that only has a path (notably
    ``FilesystemProfileStore.backend_spec``, scanning a root) can ask without
    constructing a :class:`SqliteEmbeddingIndex`.
    """
    db = cache_dir(Path(profile_dir)) / "embeddings.sqlite"
    return _read_meta(db).get("backend_name", "")


def _read_meta(db_path: Path) -> dict[str, str]:
    """``index_meta`` as a dict; ``{}`` when there is no readable index.

    The tolerant sibling of :func:`._sqlite.read_index_meta_path`, which
    raises. A plain connection: reading meta must never require sqlite-vec.
    """
    if not db_path.is_file():
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return {}
    try:
        return read_index_meta(conn)
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
