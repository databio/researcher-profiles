"""``SqlProfileStore``: a :class:`~researcher_profiles.store.ProfileStore` over an engine."""

import json
import logging
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from pydantic import ValidationError
from sqlmodel import Session, select

from ...build_state import BuildState
from ...errors import ProfileError, ProfileWriteError
from ...profile import ResearcherProfile
from ...profile.storage import ArtifactStorage
from ...schema import ArtifactRef, ProfileDocument
from ...schema.jsonld import canonical_dumps
from ...utils.paths import cache_dir
from .._analytics import _AnalyticsAccessors
from ..db import (
    ArtifactRow,
    BuildStateRow,
    ChunkVectorRow,
    ExpertiseTopicRow,
    GrantRow,
    PaperRow,
    ProfileRow,
    ProfileVectorRow,
    create_all,
)
from ..hooks import _HookedStore
from ..protocol import IngestResult, ProfileNotFoundError, UploadError
from . import _vectors
from ._shared import RENDERED_URLS, _is_memory_sqlite, _is_text, artifact_rows, render_collection
from ._storage import SqlArtifactStorage

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

    from ...embeddings.protocol import VectorIndex

logger = logging.getLogger(__name__)


class SqlProfileStore(_HookedStore, _AnalyticsAccessors):
    """A set of profiles living in SQL tables.

    ``engine_or_url`` may be a SQLAlchemy ``Engine`` or a connection URL. The
    store owns no per-profile state: every operation opens its own session, and
    the :class:`SqlArtifactStorage` behind a profile handed out by :meth:`get` opens
    its own for each read and each write unit.

    An in-memory SQLite URL (``sqlite://`` / ``sqlite:///:memory:``) gets
    ``StaticPool``: an in-memory database is private per connection, and the
    default per-thread pool would let this store and the threadpool serving
    HTTP requests over it silently see two different empty databases.
    ``StaticPool`` keeps the one connection that holds the data.
    """

    def __init__(self, engine_or_url: Any):
        if isinstance(engine_or_url, str):
            from sqlmodel import create_engine

            kwargs: dict[str, Any] = {}
            if engine_or_url.startswith("sqlite"):
                kwargs["connect_args"] = {"check_same_thread": False}
                if _is_memory_sqlite(engine_or_url):
                    # An in-memory SQLite database is private per connection,
                    # and the default pool hands out a connection per thread,
                    # so a store and the threadpool serving HTTP requests over
                    # it would silently see two different empty databases.
                    # StaticPool keeps the one connection that holds the data.
                    from sqlalchemy.pool import StaticPool

                    kwargs["poolclass"] = StaticPool
            self.engine = create_engine(engine_or_url, **kwargs)
            self.url = engine_or_url
        else:
            self.engine = engine_or_url
            self.url = str(getattr(engine_or_url, "url", engine_or_url))
        # Pre/post-commit hooks live on the store, not on the app; see
        # ``_HookedStore``. ``_live`` holds the profiles already handed out,
        # weakly, so a hook registered after a ``get()`` still reaches them
        # without the store keeping anything alive: this store hands out a
        # fresh object per call and caches nothing.
        self._live: "weakref.WeakSet[ResearcherProfile]" = weakref.WeakSet()
        self._init_hooks()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SqlProfileStore(url={self.url!r})"

    # --- identity of the store ---------------------------------------------

    @property
    def location(self) -> str:
        """The database URL. Display only."""
        return self.url

    @property
    def root(self) -> None:
        """Always ``None``: this store is not a directory.

        One feature still branches on this, and only one: the profile graph,
        whose ``graph.sqlite`` is a regenerable handle ``ArtifactStorage`` does
        not cover and that has no interface of its own. It degrades with a 503
        rather than reaching for a synthetic path.

        Vectors used to be on that list. They are not any more: they live in
        ``rp_chunk_vectors`` / ``rp_profile_vectors`` and this store serves
        them itself as a :class:`~researcher_profiles.store.VectorStore`, so
        ranking a database-backed deployment no longer exports anything to
        disk first. See :mod:`researcher_profiles.store.sql._vectors`.
        """
        return None

    # --- hooks --------------------------------------------------------------

    def _live_profiles(self):
        return list(self._live)

    def _admit(self, prof: ResearcherProfile) -> ResearcherProfile:
        self._thread_hooks(prof)
        self._live.add(prof)
        return prof

    def evict(self, ref: str) -> None:  # noqa: ARG002 - protocol signature
        """Bump the write generation. There are no cached rows to drop.

        This store caches nothing of its own, but ``evict`` is the protocol's
        "drop any view that could still be pre-write", and the analytics on
        this store hold exactly such a view. The API layer calls it after every
        write, so bumping here is what keeps ``/match`` current without that
        caller importing anything vector-shaped.
        """
        self._bump_generation()

    # --- lifecycle ----------------------------------------------------------

    def create_all(self) -> None:
        """Create the ``rp_*`` tables if absent. Fresh-instance convenience."""
        create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with Session(self.engine) as s:
            yield s

    # --- lookup -------------------------------------------------------------

    def rid_for(self, ref: str) -> str:
        """The rid of the profile ``ref`` names. ``ref`` may be a rid or a slug.

        Rid wins, mirroring ``rp where`` accepting a slug or an ORCID. A bare
        ORCID needs no separate lookup: an ORCID rid *is* its ORCID, so it is
        already the primary key being queried. Raises
        :class:`ProfileNotFoundError` naming both lookups that were tried, so
        "not found" never leaves a caller guessing which namespace was searched.
        """
        with self.session() as s:
            if s.get(ProfileRow, ref) is not None:
                return ref
            row = s.exec(select(ProfileRow).where(ProfileRow.slug == ref)).first()
            if row is not None:
                return row.rid
        raise ProfileNotFoundError(
            f"nothing in {self.url} matches {ref!r}: no rp_profiles row with "
            f"rid={ref!r}, and none with slug={ref!r}"
        )

    def exists(self, ref: str) -> bool:
        try:
            self.rid_for(ref)
        except ProfileNotFoundError:
            return False
        return True

    def resolve_slug(self, ref: str) -> str:
        """Map a slug or a rid to the slug.

        The mirror of :meth:`rid_for`, which answers with the rid. Both exist
        because the two are used for different things: ``rid`` is the join key
        every row is written under; ``slug`` is what a URL and a human say.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            row = s.get(ProfileRow, rid)
        return row.slug if row is not None else rid

    def write_lookup_index(self) -> None:
        """``None``: there is no directory to write a lookup file into.

        Not a degradation. The file exists so a *shell* caller can resolve a
        rid without importing the package; a database has :meth:`rid_for`,
        which is one indexed query and always current.
        """
        return None

    def list_slugs(self) -> list[str]:
        """Every profile's display handle, sorted. One column, not one row each.

        A projection query: a management host calls this to render
        a list page, and loading every ``document`` blob to read one string off
        each is how a store gets a reputation for being slow.
        """
        with self.session() as s:
            return list(s.exec(select(ProfileRow.slug).order_by(ProfileRow.slug)).all())

    def list_profiles(self) -> list[ProfileRow]:
        """Every profile row in the store, ordered by slug."""
        with self.session() as s:
            return list(s.exec(select(ProfileRow).order_by(ProfileRow.slug)).all())

    def get(self, ref: str, *, eager: bool = False) -> ResearcherProfile:
        """Load one profile, backed by a :class:`SqlArtifactStorage` over its rows."""
        rid = self.rid_for(ref)
        with self.session() as s:
            row = s.get(ProfileRow, rid)
            slug = row.slug if row is not None else rid
        return self._admit(ResearcherProfile(SqlArtifactStorage(self, rid, slug=slug), eager=eager))

    # --- ingest -------------------------------------------------------------

    def put(
        self,
        profile: ResearcherProfile,
        *,
        slug: Optional[str] = None,
        include_binary: bool = False,
    ) -> str:
        """Write ``profile`` into the store, in one transaction. Returns its rid.

        Takes **any** ``ResearcherProfile``: a directory, a static or remote
        published one, or one belonging to another store. Upserts on ``rid``
        and replaces every child row (delete, then insert) rather than diffing:
        a profile is small, a diff has more ways to be wrong than to be right,
        and "the rows equal the source" is the only invariant worth having.

        Artifacts come from the profile's recorded manifest
        (:meth:`ResearcherProfile.manifest`) when it has one, falling back to
        walking the directory. The recorded manifest is preferred because it is
        what the published document actually claims; regenerating it here
        would silently rewrite a published record during an ingest.

        ``include_binary`` opts into storing binary bodies (a published
        ``.bin`` or an image); without it a binary artifact keeps its manifest
        row and loses only its bytes. It does not gate vectors: those are
        shredded into ``rp_chunk_vectors`` / ``rp_profile_vectors`` on every
        put, because a queryable vector is not a file body.
        """
        meta = profile.metadata
        rid = meta.rid
        handle = slug or profile.slug
        document = profile.persisted_document() or json.loads(
            canonical_dumps(meta.model_dump(mode="json"))
        )
        soul = profile.soul or ""
        parts = self._manifest_of(profile)

        with self.session() as s:
            existing = s.get(ProfileRow, rid)
            if existing is not None:
                self._delete_children(s, rid)
                s.delete(existing)
                s.flush()

            s.add(ProfileRow.from_document(meta, slug=handle, document=document, soul=soul))
            # Flush the parent BEFORE any child. SQLAlchemy orders a flush by
            # mapper dependencies, which come from `relationship()` declarations,
            # and these tables carry FK *columns* with no relationships, so
            # the unit of work is free to emit `INSERT INTO rp_artifacts` first
            # and have the FK rejected. Postgres FKs are not deferrable, so this
            # is a real failure there (and anywhere SQLite's foreign_keys pragma
            # is on, which a management host turns on), not a test artifact.
            s.flush()

            for ordinal, paper in enumerate(profile.papers):
                s.add(PaperRow.from_record(rid, paper, ordinal=ordinal))
            for ordinal, grant in enumerate(profile.grants):
                s.add(GrantRow.from_record(rid, grant, ordinal=ordinal))
            for ordinal, topic in enumerate(meta.expertise):
                s.add(ExpertiseTopicRow(profile_rid=rid, ordinal=ordinal, topic=topic))

            seen: set[str] = set()
            for slot, part, ordinal in parts:
                if part.content_url in seen:
                    continue
                seen.add(part.content_url)
                s.add(
                    self._artifact_row(
                        rid, part, slot, ordinal, profile.storage, include_binary=include_binary
                    )
                )

            state = profile.build_state
            if state is not None and (state.papers or state.build.completed_phases):
                s.add(
                    BuildStateRow(
                        profile_rid=rid,
                        schema_version=int(state.schema_version),
                        state=state.model_dump(mode="json", exclude_none=True),
                    )
                )

            # Vectors: shredded into queryable rows, never stored as a blob.
            # This is what makes the store a ``VectorStore``; see ``_vectors``.
            for row in _vectors.shred_profile(rid, profile):
                s.add(row)

            s.commit()
        self._bump_generation()
        return rid

    def import_directory(self, path: str | Path, *, include_binary: bool = False) -> str:
        """Sugar for ``put(ResearcherProfile.from_files(path))``. Returns the rid."""
        return self.put(ResearcherProfile.from_files(path), include_binary=include_binary)

    def commit_directory(
        self,
        slug: str,
        staging: Path,
        *,
        build_missing_index: bool = False,
    ) -> IngestResult:
        """Make a fully-staged profile directory live in this store.

        One transaction, which is what replaces the filesystem backend's
        rename-aside-and-swap: there is no window in which the profile half
        exists, so a host's pre-commit hook granting ownership commits with it
        or not at all.

        When the staged profile carries a built embedding index
        (``.cache/embeddings.sqlite``), its public chunks are shredded into
        ``rp_chunk_vectors`` and its centroid into ``rp_profile_vectors``, and
        ``indexed=True`` is reported; ``/match`` then queries those rows
        directly, with no directory and no runtime embedding backend needed to
        serve them.

        ``build_missing_index`` builds one in the staging directory first,
        exactly as the filesystem backend does and for the same reason: ingest
        is now the only moment a vector can enter this store, so a profile
        staged without an index would otherwise be hosted and permanently
        unrankable. Best-effort: a core-only install or a build failure leaves
        the profile committed but unindexed, never unhosted.
        """
        try:
            staged = ResearcherProfile.from_files(staging)
            name = staged.metadata.name
            level = str(staged.level)
        except (OSError, ValueError, ProfileError, ValidationError) as e:
            raise UploadError(f"staged profile failed to load: {e}") from e
        indexed = (cache_dir(staging) / "embeddings.sqlite").is_file()
        if not indexed and build_missing_index:
            indexed = self._try_build_index(Path(staging))
            if indexed:
                staged = ResearcherProfile.from_files(staging)
        rid = self.put(staged, slug=slug, include_binary=True)
        return IngestResult(slug=slug, rid=rid, name=name, level=level, indexed=indexed)

    @staticmethod
    def _try_build_index(staging: Path) -> bool:
        """Build the staged directory's index. The filesystem backend's twin."""
        try:
            from ...embeddings import build_index

            build_index(staging)
        # Boundary: the whole embedding stack, including whatever a third-party
        # encoder backend raises. A missing extra, an absent backend, or a build
        # error all leave the profile committed but unindexed, never unhosted.
        except Exception:
            logger.warning("post-ingest index build failed for %s", staging, exc_info=True)
        return (cache_dir(staging) / "embeddings.sqlite").is_file()

    def refresh_vectors(self, ref: str) -> bool:
        """Rebuild one profile's vector rows from its current papers.

        Exports the stored profile to a temp directory, builds the embedding
        index there, shreds the result back into ``rp_chunk_vectors`` /
        ``rp_profile_vectors``, and cleans up. Best-effort: returns ``True``
        on success, ``False`` when the embedding stack is unavailable or the
        build fails for any reason — a caller should log the outcome, never
        raise on it.

        This is the SQL-backed counterpart of the filesystem's
        ``prof.index.build()`` + ``recompute_centroid()`` path: both update a
        profile's vectors after its papers change, but only one needs to go
        through an export round-trip because vectors enter this store through
        rows, not a file.
        """
        import shutil
        import tempfile

        rid = self.rid_for(ref)
        tmp = None
        try:
            tmp = Path(tempfile.mkdtemp(prefix="rp_refresh_"))
            staging = self.export_directory(rid, tmp / "profile")
            if not self._try_build_index(staging):
                return False
            staged = ResearcherProfile.from_files(staging)
            new_rows = _vectors.shred_profile(rid, staged)
            if not new_rows:
                return False
            with self.session() as s:
                for old in s.exec(
                    select(ChunkVectorRow).where(ChunkVectorRow.profile_rid == rid)
                ).all():
                    s.delete(old)
                for old in s.exec(
                    select(ProfileVectorRow).where(ProfileVectorRow.profile_rid == rid)
                ).all():
                    s.delete(old)
                s.flush()
                for row in new_rows:
                    s.add(row)
                s.commit()
            self._bump_generation()
            return True
        except Exception:
            logger.warning("refresh_vectors failed for %s", ref, exc_info=True)
            return False
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    def create(self, document: ProfileDocument, *, slug: str) -> ResearcherProfile:
        """Create a new profile from a validated document, in one write unit.

        The row is inserted bare and then written through
        :meth:`ResearcherProfile.save_profile`, so the create takes exactly the
        same validate -> stamp -> canonicalize -> persist path every other write
        takes. A management host's pre-commit hook therefore sees
        ``kind="create"`` with a live session and can write its ownership row in
        the same transaction, closing the window where a profile exists that
        nobody owns.
        """
        rid = document.rid
        if self.exists(slug) or self.exists(rid):
            raise ProfileWriteError(
                self.url,
                f"cannot create {slug!r}: a profile with that slug or with rid "
                f"{rid!r} already exists in this store",
            )
        prof = self._admit(ResearcherProfile(SqlArtifactStorage(self, rid, slug=slug)))
        with prof.write_unit("create") as ctx:
            ctx.session.add(
                ProfileRow(
                    rid=rid,
                    slug=slug,
                    document={},
                    name=document.name,
                    provenance=str(document.provenance),
                )
            )
            ctx.session.flush()
            prof.save_profile(document)
        return prof

    def put_document(self, slug: str, document: ProfileDocument) -> ResearcherProfile:
        """Create-or-replace a profile's canonical document only.

        If the profile exists, runs an ``"edit"`` write unit via
        :meth:`save_profile`; if new, runs a ``"create"`` unit. The write uses
        the profile's :meth:`save_profile` so ``dateModified`` is stamped
        correctly and hooks fire.
        """
        is_create = not self.exists(slug)

        if is_create:
            # Check that the rid is not already taken by another slug
            if self.exists(document.rid):
                raise ProfileWriteError(
                    self.url,
                    f"cannot create {slug!r}: a profile with rid "
                    f"{document.rid!r} already exists in this store",
                )
            return self.create(document, slug=slug)

        # Profile exists: edit path
        prof = self.get(slug)
        existing_rid = prof.rid
        if document.rid != existing_rid:
            raise ProfileWriteError(
                self.url,
                f"cannot replace {slug!r}: document rid {document.rid!r} does not "
                f"match existing rid {existing_rid!r}",
            )
        prof.save_profile(document)
        return prof

    @staticmethod
    def _manifest_of(profile: ResearcherProfile) -> list[tuple[str, ArtifactRef, int]]:
        """``(manifest_slot, part, ordinal)`` for every artifact of ``profile``."""
        meta = profile.metadata
        recorded = [*meta.has_part, *meta.subject_of]
        if recorded:
            return [
                *[("hasPart", p, i) for i, p in enumerate(meta.has_part)],
                *[("subjectOf", p, i) for i, p in enumerate(meta.subject_of)],
            ]
        parts, subjects = profile.storage.build_manifest()
        return [
            *[("hasPart", p, i) for i, p in enumerate(parts)],
            *[("subjectOf", p, i) for i, p in enumerate(subjects)],
        ]

    @staticmethod
    def _artifact_row(
        rid: str,
        part: ArtifactRef,
        slot: str,
        ordinal: int,
        bodies: "ArtifactStorage",
        *,
        include_binary: bool,
    ) -> ArtifactRow:
        """One ``rp_artifacts`` row, with its body read off the source backend.

        ``bodies`` is the source profile's own storage, so each backend answers
        for its own bytes: a directory reads the file verbatim, another SQL
        store reads the row, a static host fetches the URL, and a live API
        returns ``None`` for anything v1 does not serve. An absent body keeps
        the manifest row and loses only its bytes.
        """
        if part.content_url in RENDERED_URLS:
            return ArtifactRow.from_part(
                rid,
                part,
                manifest_slot=slot,
                ordinal=ordinal,
                rendered=True,
                envelope=bodies.collection_envelope(part.content_url),
            )
        if _is_text(part.encoding_format, part.content_url):
            return ArtifactRow.from_part(
                rid,
                part,
                manifest_slot=slot,
                ordinal=ordinal,
                text=bodies.artifact_text(part.content_url),
            )
        data = bodies.artifact_bytes(part.content_url) if include_binary else None
        return ArtifactRow.from_part(rid, part, manifest_slot=slot, ordinal=ordinal, data=data)

    # --- identity ------------------------------------------------------------

    def rename(self, rid: str, new_slug: str) -> None:
        """Change a profile's display handle. one update; no child row moves.

        This is the whole payoff of keying on ``rid``: a rename is not a data
        migration. The unique constraint on ``slug`` still applies: a store
        cannot hold two profiles under one handle.
        """
        with self.session() as s:
            row = s.get(ProfileRow, rid)
            if row is None:
                raise ProfileNotFoundError(f"no rp_profiles row with rid={rid!r} in {self.url}")
            row.slug = new_slug
            s.add(row)
            s.commit()
        self._bump_generation()

    def delete(self, ref: str) -> str:
        """Remove a profile and every child row, including its build state."""
        rid = self.rid_for(ref)
        with self.session() as s:
            self._delete_children(s, rid)
            row = s.get(ProfileRow, rid)
            if row is not None:
                s.delete(row)
            s.commit()
        self._bump_generation()
        return rid

    @staticmethod
    def _delete_children(s: Session, rid: str) -> None:
        for model in (
            PaperRow,
            GrantRow,
            ExpertiseTopicRow,
            ArtifactRow,
            ChunkVectorRow,
            ProfileVectorRow,
            BuildStateRow,
        ):
            for row in s.exec(select(model).where(model.profile_rid == rid)).all():
                s.delete(row)
        s.flush()

    # --- the manifest ---------------------------------------------------------

    def manifest_from_rows(self, ref: str) -> tuple[list[ArtifactRef], list[ArtifactRef]]:
        """``(hasPart, subjectOf)`` from ``rp_artifacts``.

        The DB twin of :func:`researcher_profiles.manifest.build_manifest`.
        That one walks a directory and this one reads the rows; both answer
        the same question, what a profile contains.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            rows = artifact_rows(s, rid)
        parts = [r.to_part() for r in rows if r.manifest_slot != "subjectOf"]
        subjects = [r.to_part() for r in rows if r.manifest_slot == "subjectOf"]
        return parts, subjects

    # --- vectors (the ``VectorStore`` capability) ----------------------------
    #
    # Four queries over two tables. No blob is unpacked, no directory is
    # exported, and no embedding backend runs here: the store serves the
    # vectors it stored, and the caller embeds its own query. See
    # :mod:`._vectors` for how the rows got there.

    @property
    def backend_spec(self) -> str | None:
        """The embedding space this store's vectors live in, or ``None``.

        One row off ``rp_chunk_vectors``. Asked once at registry construction,
        so it must not open anything.
        """
        with self.session() as s:
            return _vectors.backend_spec(s)

    def has_vector_index(self, ref: str) -> bool:
        """Whether ``ref`` has chunk vectors. One row, by contract cheap.

        The ``/match`` dependency scans every profile in the store with it to
        decide whether ranking is possible at all, so it is a ``LIMIT 1`` and
        never a fetch.
        """
        try:
            rid = self.rid_for(ref)
        except ProfileNotFoundError:
            return False
        with self.session() as s:
            return _vectors.has_vectors(s, rid)

    def vector_index(self, ref: str) -> "VectorIndex":
        """The profile's chunk-level index, built from its rows.

        The rows are serialized into the three published byte shapes and read
        back by :class:`~researcher_profiles.embeddings.flat.FlatEmbeddingIndex`,
        which is the single read implementation behind all three backends. The
        alternative, a fourth index class that happens to hold SQL rows, would
        be a fourth place for cosine to be subtly different.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            payload = _vectors.flat_bytes(s, rid)
        if payload is None:
            from ...embeddings._sqlite import IndexNotBuiltError

            raise IndexNotBuiltError(
                f"profile {ref!r} in {self.url} has no rows in rp_chunk_vectors: "
                "it was ingested without a built index, or every chunk was restricted"
            )

        from ...embeddings.flat import FlatEmbeddingIndex

        return FlatEmbeddingIndex.from_bytes(*payload)

    def centroid(self, ref: str) -> "np.ndarray":
        """The profile's stored centroid: one row, already normalized.

        Not derived from :meth:`vector_index`. The centroid is computed at
        ingest over the *whole* index, restricted chunks included (one averaged
        vector is not invertible, spec section 6), exactly as the published
        ``collection/embeddings/`` blob is, while the chunk rows are the public
        subset. Recomputing it from the chunk rows would quietly answer a
        different question.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            vec = _vectors.centroid(s, rid)
        if vec is None:
            return self.vector_index(rid).centroid()
        return vec

    def centroids_matrix(self) -> "tuple[list[str], np.ndarray] | None":
        """The whole roster's centroids in one ``SELECT``, or ``None``.

        This is the payoff of storing profile vectors in their own table:
        ranking N profiles costs one query rather than N index reads, the same
        way a published site's single stacked blob costs one fetch.
        """
        with self.session() as s:
            return _vectors.centroids_matrix(s)

    # --- export --------------------------------------------------------------

    def export_directory(self, ref: str, dest: str | Path, *, with_build: bool = False) -> Path:
        """Materialize a stored profile as a directory. Returns the directory.

        ``profile.jsonld`` is ``canonical_dumps(document)``; every artifact body
        is written to its own ``contentUrl``; the works and grants collections
        are re-rendered from ``rp_papers`` / ``rp_grants`` inside their stored
        ``envelope``, so the collection metadata (``about``, ``dateModified``,
        ``@context``) survives the round trip.

        Vectors are written as the published flat form
        (``embeddings/index.json`` + ``<backend>.bin`` + ``<backend>.chunks.json``)
        regenerated from ``rp_chunk_vectors``, after the artifact bodies and
        overwriting whatever they wrote, so the declared ``count`` and
        ``sha256`` always describe the blob beside them. No
        ``.cache/embeddings.sqlite`` is reconstituted: nothing needs one, since
        the filesystem backend reads the flat form when there is no sqlite, and
        this store answers ``/match`` from its rows without exporting at all.

        Build state is written only when ``with_build=True``, and then into the
        build root beside the directory, never inside it. See
        :class:`researcher_profiles.store.db.BuildStateRow`.
        """
        rid = self.rid_for(ref)
        out = Path(dest).expanduser()
        out.mkdir(parents=True, exist_ok=True)

        with self.session() as s:
            row = s.get(ProfileRow, rid)
            if row is None:  # pragma: no cover - resolve() already proved it
                raise ProfileNotFoundError(rid)
            # ``newline=""`` throughout: writing in text mode with the default
            # translates ``\n`` to ``os.linesep``, which would make an export
            # platform-dependent and would undo the verbatim storage above.
            (out / "profile.jsonld").write_text(
                canonical_dumps(row.document or {}), encoding="utf-8", newline=""
            )

            for art in artifact_rows(s, rid):
                target = out / art.content_url
                target.parent.mkdir(parents=True, exist_ok=True)
                if art.rendered:
                    body = render_collection(s, rid, art)
                    if body is not None:
                        target.write_text(body, encoding="utf-8", newline="")
                elif art.text is not None:
                    target.write_text(art.text, encoding="utf-8", newline="")
                elif art.data is not None:
                    target.write_bytes(art.data)

            self._write_flat_export(s, rid, out)

            if with_build:
                state_row = s.get(BuildStateRow, rid)
                if state_row is not None:
                    BuildState.model_validate(state_row.state or {}).save(out)
        return out

    @staticmethod
    def _write_flat_export(s: Session, rid: str, out: Path) -> None:
        """Write ``embeddings/`` from the vector rows. No-op without any.

        The rows are the authority for what the exported flat form says, not
        the ``embeddings/index.json`` artifact body: that body was written by
        whichever machine built the profile and its ``sha256`` describes a blob
        this export does not necessarily have. Regenerating both together is
        the only way the pair cannot disagree.
        """
        payload = _vectors.flat_bytes(s, rid)
        if payload is None:
            return
        index_json, blob, chunks_json = payload
        name = str(json.loads(index_json)["file"])
        d = out / "embeddings"
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.json").write_bytes(index_json)
        (d / name).write_bytes(blob)
        (d / f"{Path(name).stem}.chunks.json").write_bytes(chunks_json)

    # --- bytes ----------------------------------------------------------------

    def document_bytes(self, ref: str) -> bytes:
        """The canonical ``profile.jsonld`` bytes for ``ref``.

        Serialized on demand from the stored ``document`` rather than kept in a
        second ``document_bytes`` column.
        :func:`researcher_profiles.schema.jsonld.canonical_dumps` is the package's only
        writer of ``.jsonld`` bytes and is a pure function of the mapping (one
        fixed key order, one fixed formatting), so these bytes are *the* bytes
        ``save_profile`` persisted and ``export_directory`` writes. A byte
        column would be a second representation of the same fact, and two
        representations of one fact drift.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            row = s.get(ProfileRow, rid)
            if row is None:  # pragma: no cover - resolve() already proved it
                raise ProfileNotFoundError(rid)
            return canonical_dumps(row.document or {}).encode("utf-8")

    def artifact_bytes(self, ref: str, content_url: str) -> bytes:
        """One manifest artifact's bytes.

        There is no traversal check because there is no path: ``content_url`` is
        a key in ``rp_artifacts``, and a key that is not there is a 404 rather
        than a walk up the filesystem. A ``rendered`` collection is regenerated
        from ``rp_papers`` / ``rp_grants`` on the way out, exactly as it is for
        :meth:`export_directory`.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            row = s.exec(
                select(ArtifactRow)
                .where(ArtifactRow.profile_rid == rid)
                .where(ArtifactRow.content_url == content_url)
            ).first()
            if row is None:
                raise ProfileNotFoundError(
                    f"artifact {content_url!r} is not an artifact of {ref!r} in {self.url}"
                )
            if row.rendered:
                body = render_collection(s, rid, row)
                if body is None:  # pragma: no cover - only two collections render
                    raise ProfileNotFoundError(
                        f"artifact {content_url!r} of {ref!r} has no renderer"
                    )
                return body.encode("utf-8")
            if row.text is not None:
                return row.text.encode("utf-8")
            if row.data is not None:
                return bytes(row.data)
        raise ProfileNotFoundError(
            f"artifact {content_url!r} of {ref!r} has a manifest row but no stored "
            "body (binary bodies are opt-in: re-ingest with include_binary=True)"
        )

    def content_hash(self, ref: str) -> str:
        """The stored ``content_hash`` column: derived state, not a recompute.

        Refreshed inside the write unit before the pre-commit hooks run, so a
        hook reading it observes post-write content. Spans the canonical
        document and the SOUL text, NUL-separated, identically to every other
        backend.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            row = s.get(ProfileRow, rid)
            if row is None:  # pragma: no cover - resolve() already proved it
                raise ProfileNotFoundError(rid)
            if row.content_hash:
                return row.content_hash
        return self.get(rid).content_hash()
