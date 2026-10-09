"""``SqlProfileStore``: a :class:`~researcher_profiles.store.ProfileStore` over an engine."""

import json
import logging
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from pydantic import ValidationError
from sqlalchemy import func, or_
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
    RidAliasRow,
    create_all,
)
from ..hooks import _HookedStore
from ..protocol import IngestResult, ProfileNotFoundError, RetiredRidError, UploadError
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
    """

    def __init__(self, engine_or_url: Any):
        if isinstance(engine_or_url, str):
            from sqlmodel import create_engine

            kwargs: dict[str, Any] = {}
            if engine_or_url.startswith("sqlite"):
                kwargs["connect_args"] = {"check_same_thread": False}
                if _is_memory_sqlite(engine_or_url):
                    # An in-memory database is private per connection, and the
                    # default pool gives each thread its own, so threads would
                    # see different empty databases. StaticPool shares one.
                    from sqlalchemy.pool import StaticPool

                    kwargs["poolclass"] = StaticPool
            self.engine = create_engine(engine_or_url, **kwargs)
            self.url = engine_or_url
        else:
            self.engine = engine_or_url
            self.url = str(getattr(engine_or_url, "url", engine_or_url))
        # Profiles already handed out, held weakly so a later hook reaches
        # them without the store keeping anything alive.
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
        """Always ``None``: this store is not a directory."""
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

        The analytics hold a view that could be pre-write; the bump drops it.
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

        Rid wins, then slug, then an alias. Raises
        :class:`ProfileNotFoundError` naming both lookups that were tried.
        """
        with self.session() as s:
            if s.get(ProfileRow, ref) is not None:
                return ref
            row = s.exec(select(ProfileRow).where(ProfileRow.slug == ref)).first()
            if row is not None:
                return row.rid
            successor = self._alias_successor(s, ref)
            if successor is not None and s.get(ProfileRow, successor) is not None:
                return successor
        raise ProfileNotFoundError(
            f"nothing in {self.url} matches {ref!r}: no rp_profiles row with "
            f"rid={ref!r}, and none with slug={ref!r}"
        )

    @staticmethod
    def _alias_successor(s: Session, ref: str) -> Optional[str]:
        """The successor rid of a retired rid or slug ``ref``, or ``None``.

        Only consulted after the live lookups miss, so a live profile always
        beats an alias. Chains are one hop, so this is one lookup.
        """
        alias = s.get(RidAliasRow, ref)
        if alias is None:
            alias = s.exec(select(RidAliasRow).where(RidAliasRow.old_slug == ref)).first()
        return None if alias is None else alias.successor_rid

    @staticmethod
    def _refuse_retired(s: Session, rid: str, slug: Optional[str]) -> None:
        """Raise :class:`RetiredRidError` if a write would reuse a retired rid or slug.

        Rid and slug are checked in their own namespaces (not via
        :meth:`_alias_successor`, which falls through from one to the other).
        """
        alias = s.get(RidAliasRow, rid)
        if alias is not None and s.get(ProfileRow, rid) is None:
            raise RetiredRidError(rid, alias.successor_rid, rid=rid, slug=slug)
        if slug:
            alias = s.exec(select(RidAliasRow).where(RidAliasRow.old_slug == slug)).first()
            if (
                alias is not None
                and s.exec(select(ProfileRow).where(ProfileRow.slug == slug)).first() is None
            ):
                raise RetiredRidError(slug, alias.successor_rid, rid=rid, slug=slug)

    def successor_of(self, ref: str) -> Optional[str]:
        """See :meth:`~researcher_profiles.store.ProfileStore.successor_of`."""
        with self.session() as s:
            if s.get(ProfileRow, ref) is not None:
                return None
            if s.exec(select(ProfileRow).where(ProfileRow.slug == ref)).first() is not None:
                return None
            return self._alias_successor(s, ref)

    def alias_slugs(self) -> set[str]:
        """Every retired slug."""
        with self.session() as s:
            rows = s.exec(select(RidAliasRow.old_slug).where(RidAliasRow.old_slug.is_not(None)))
            return {r for r in rows.all() if r}

    def rids_with_email(self, email: str) -> list[str]:
        """Reads the ``rp_profiles.email`` projection."""
        needle = (email or "").strip().lower()
        if not needle:
            return []
        with self.session() as s:
            return list(
                s.exec(
                    select(ProfileRow.rid)
                    .where(func.lower(func.trim(ProfileRow.email)) == needle)
                    .order_by(ProfileRow.rid)
                ).all()
            )

    def exists(self, ref: str) -> bool:
        try:
            self.rid_for(ref)
        except ProfileNotFoundError:
            return False
        return True

    def resolve_slug(self, ref: str) -> str:
        """Map a slug or a rid to the slug."""
        rid = self.rid_for(ref)
        with self.session() as s:
            row = s.get(ProfileRow, rid)
        return row.slug if row is not None else rid

    def write_lookup_index(self) -> None:
        """``None``: no directory. :meth:`rid_for` answers the same question."""
        return None

    def list_slugs(self) -> list[str]:
        """Every profile's display handle, sorted. Selects one column only."""
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

        Takes any ``ResearcherProfile``. Upserts on ``rid`` and replaces every
        child row rather than diffing, so the rows always equal the source.

        Artifacts come from the profile's recorded manifest when it has one
        (see :meth:`_manifest_of`), else from walking the directory.

        ``include_binary`` opts into storing binary bodies; without it a binary
        artifact keeps its manifest row and loses only its bytes. Vectors are
        shredded on every put regardless.

        Raises :class:`RetiredRidError` when the rid or slug was retired by a
        merge; that covers :meth:`commit_directory` and :meth:`import_directory`.
        """
        handle = slug or profile.slug
        with self.session() as s:
            self._refuse_retired(s, profile.metadata.rid, handle)
            rid = self._write_profile_rows(s, profile, slug=handle, include_binary=include_binary)
            s.commit()
        self._bump_generation()
        return rid

    def _write_profile_rows(
        self,
        s: Session,
        profile: ResearcherProfile,
        *,
        slug: str,
        include_binary: bool,
    ) -> str:
        """Replace ``profile``'s rows on ``s``. Flushes, never commits. Returns the rid.

        Shared by :meth:`put` and :meth:`merge_into`.
        """
        meta = profile.metadata
        rid = meta.rid
        handle = slug
        document = profile.persisted_document() or json.loads(
            canonical_dumps(meta.model_dump(mode="json"))
        )
        soul = profile.soul or ""
        parts = self._manifest_of(profile)

        existing = s.get(ProfileRow, rid)
        if existing is not None:
            self._delete_children(s, rid)
            s.delete(existing)
            s.flush()

        s.add(ProfileRow.from_document(meta, slug=handle, document=document, soul=soul))
        # Flush the parent BEFORE any child. These tables have FK columns but no
        # `relationship()`, so SQLAlchemy may otherwise insert children first,
        # and Postgres FKs are not deferrable.
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

        # Vectors: shredded into queryable rows; see ``_vectors``.
        for row in _vectors.shred_profile(rid, profile):
            s.add(row)
        s.flush()
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

        One transaction, so a pre-commit hook's ownership row commits with the
        profile or not at all.

        A staged ``.cache/embeddings.sqlite`` is shredded into vector rows and
        reported as ``indexed=True``. Ingest is the only moment a vector can
        enter this store, so ``build_missing_index`` builds one first
        (best-effort; failure leaves the profile committed but unindexed).
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

    def merge_into(
        self,
        retired_ref: str,
        staging: Path,
        *,
        survivor_rid: str,
        survivor_slug: str,
        build_missing_index: bool = True,
    ) -> IngestResult:
        """Retire one profile into another, in ONE write unit.

        See :meth:`~researcher_profiles.store.ProfileStore.merge_into`. Hooks
        run on the unit's own session, so a host can re-key its rows in the
        same transaction.

        When the survivor keeps the retired profile's slug (a rid conversion),
        the alias covers the old rid only: the slug still names a live profile.

        Raises :class:`ProfileWriteError` when the retired and survivor rids are
        the same or the staged rid is not ``survivor_rid``, :class:`RetiredRidError`
        when ``survivor_rid`` was itself retired by an earlier merge,
        :class:`ProfileNotFoundError` when ``retired_ref`` names no live profile,
        :class:`UploadError` when the staged directory does not load.
        """
        with self.session() as s:
            # A merge must never make a retired rid live again.
            self._refuse_retired(s, survivor_rid, None)
            retired_row = (
                s.get(ProfileRow, retired_ref)
                or s.exec(select(ProfileRow).where(ProfileRow.slug == retired_ref)).first()
            )
            if retired_row is None:
                raise ProfileNotFoundError(f"no live profile {retired_ref!r} in {self.url}")
            retired_rid, retired_slug = retired_row.rid, retired_row.slug
        if retired_rid == survivor_rid:
            raise ProfileWriteError(self.url, "cannot merge a profile into itself")
        try:
            staged = ResearcherProfile.from_files(staging)
            name = staged.metadata.name
            level = str(staged.level)
        except (OSError, ValueError, ProfileError, ValidationError) as e:
            raise UploadError(f"staged profile failed to load: {e}") from e
        if staged.metadata.rid != survivor_rid:
            raise ProfileWriteError(
                self.url,
                f"staged rid {staged.metadata.rid!r} is not the survivor rid {survivor_rid!r}",
            )
        indexed = (cache_dir(staging) / "embeddings.sqlite").is_file()
        if not indexed and build_missing_index:
            indexed = self._try_build_index(Path(staging))
            if indexed:
                staged = ResearcherProfile.from_files(staging)

        storage = SqlArtifactStorage(self, survivor_rid, slug=survivor_slug)
        storage.context_extra = {"retired_rid": retired_rid, "retired_slug": retired_slug}
        prof = self._admit(ResearcherProfile(storage))
        try:
            self._merge_unit(
                prof, storage, staged, retired_rid, retired_slug, survivor_rid, survivor_slug
            )
        finally:
            storage.context_extra = {}
        self._bump_generation()
        return IngestResult(
            slug=survivor_slug, rid=survivor_rid, name=name, level=level, indexed=indexed
        )

    def _merge_unit(
        self,
        prof: ResearcherProfile,
        storage: SqlArtifactStorage,
        staged: ResearcherProfile,
        retired_rid: str,
        retired_slug: str,
        survivor_rid: str,
        survivor_slug: str,
    ) -> None:
        """The one write unit behind :meth:`merge_into`."""
        with prof.write_unit("merge") as ctx:
            s = ctx.session
            self._delete_children(s, retired_rid)
            gone = s.get(ProfileRow, retired_rid)
            if gone is not None:
                s.delete(gone)
            s.flush()
            self._write_profile_rows(s, staged, slug=survivor_slug, include_binary=True)
            # A live profile beats an alias: drop any alias that names the
            # survivor's own rid or slug, then collapse chains onto the survivor.
            for stale in s.exec(
                select(RidAliasRow).where(
                    (RidAliasRow.old_rid == survivor_rid)
                    | (RidAliasRow.old_slug == survivor_slug)
                    | (RidAliasRow.old_slug == retired_slug)
                )
            ).all():
                s.delete(stale)
            for chained in s.exec(
                select(RidAliasRow).where(RidAliasRow.successor_rid == retired_rid)
            ).all():
                chained.successor_rid = survivor_rid
                s.add(chained)
            s.flush()
            s.add(
                RidAliasRow(
                    old_rid=retired_rid,
                    old_slug=None if retired_slug == survivor_slug else retired_slug,
                    successor_rid=survivor_rid,
                )
            )
            s.flush()
            storage.refresh_derived(ctx)
            prof._run_pre_commit_hooks(ctx)

    @staticmethod
    def _try_build_index(staging: Path) -> bool:
        """Build the staged directory's index, best-effort."""
        try:
            from ...embeddings import build_index

            build_index(staging)
        # Boundary: anything the embedding stack raises leaves the profile
        # committed but unindexed.
        except Exception:
            logger.warning("post-ingest index build failed for %s", staging, exc_info=True)
        return (cache_dir(staging) / "embeddings.sqlite").is_file()

    def refresh_vectors(self, ref: str) -> bool:
        """Rebuild one profile's vector rows from its current papers.

        Exports the stored profile to a temp directory, builds the embedding
        index there, shreds the result back into ``rp_chunk_vectors`` /
        ``rp_profile_vectors``, and cleans up. Best-effort: returns ``True``
        on success, ``False`` when the embedding stack is unavailable or the
        build fails for any reason. Never raises.
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
        :meth:`ResearcherProfile.save_profile`, so it takes the same path as
        every other write.
        """
        rid = document.rid
        with self.session() as s:
            self._refuse_retired(s, rid, slug)
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

    def create_bundle(
        self,
        document: ProfileDocument,
        *,
        slug: str,
        expertise: str | None = None,
        soul: str | None = None,
        artifacts: dict[str, str] | None = None,
    ) -> ResearcherProfile:
        """Create a document and its authored artifacts in one transaction."""
        rid = document.rid
        with self.session() as s:
            self._refuse_retired(s, rid, slug)
        if self.exists(slug) or self.exists(rid):
            raise ProfileWriteError(self.url, f"cannot create {slug!r}: slug or rid already exists")
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
            if expertise is not None:
                prof.save_expertise(expertise)
            if soul is not None:
                prof.save_soul(soul)
            for content_url, text in (artifacts or {}).items():
                part = next(
                    (
                        p
                        for p in (*document.has_part, *document.subject_of)
                        if p.content_url == content_url
                    ),
                    None,
                )
                if part is None:
                    raise ProfileWriteError(
                        self.url, f"artifact {content_url!r} is not in manifest"
                    )
                prof.storage.write_artifact(
                    content_url,
                    text,
                    role=part.role or "artifact",
                    name=part.name or content_url,
                    manifest_slot="hasPart" if part in document.has_part else "subjectOf",
                    encoding_format=part.encoding_format or "text/plain",
                )
        return prof

    def put_document(self, slug: str, document: ProfileDocument) -> ResearcherProfile:
        """See :meth:`~researcher_profiles.store.ProfileStore.put_document`."""
        with self.session() as s:
            self._refuse_retired(s, document.rid, slug)
        is_create = not self.exists(slug)

        if is_create:
            if self.exists(document.rid):
                raise ProfileWriteError(
                    self.url,
                    f"cannot create {slug!r}: a profile with rid "
                    f"{document.rid!r} already exists in this store",
                )
            return self.create(document, slug=slug)

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
        """``(manifest_slot, part, ordinal)`` for every artifact of ``profile``.

        The recorded manifest wins when there is one: it is what the published
        document claims, and regenerating it would rewrite a published record.
        A file it does not list is not persisted, so for a directory source the
        omissions are logged.
        """
        meta = profile.metadata
        recorded = [*meta.has_part, *meta.subject_of]
        if recorded:
            SqlProfileStore._warn_unlisted(profile, {p.content_url for p in recorded})
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
    def _warn_unlisted(profile: ResearcherProfile, recorded_urls: set[str]) -> None:
        """Log the files the source directory holds that the manifest omits."""
        root = getattr(profile.storage, "_root", None)
        if root is None:
            return
        try:
            parts, subjects = profile.storage.build_manifest()
        # Boundary: a diagnostic must never fail an ingest.
        except Exception:  # noqa: BLE001
            return
        unlisted = sorted({p.content_url for p in (*parts, *subjects)} - recorded_urls)
        if unlisted:
            logger.warning(
                "%s: %d file(s) in %s are not in the recorded manifest and will "
                "not be persisted: %s",
                profile.slug,
                len(unlisted),
                root,
                unlisted[:5],
            )

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

        ``bodies`` is the source profile's own storage. An absent body keeps
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
        """Change a profile's display handle. One update; no child row moves.

        The unique constraint on ``slug`` still applies.
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

        The row-based twin of ``build_manifest``.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            rows = artifact_rows(s, rid)
        parts = [r.to_part() for r in rows if r.manifest_slot != "subjectOf"]
        subjects = [r.to_part() for r in rows if r.manifest_slot == "subjectOf"]
        return parts, subjects

    # --- vectors (the ``VectorStore`` capability) ----------------------------

    @property
    def backend_spec(self) -> str | None:
        """The embedding space this store's vectors live in, or ``None``.

        One row off ``rp_chunk_vectors``.
        """
        with self.session() as s:
            return _vectors.backend_spec(s)

    def has_vector_index(self, ref: str) -> bool:
        """Whether ``ref`` has chunk vectors. A ``LIMIT 1``, never a fetch."""
        try:
            rid = self.rid_for(ref)
        except ProfileNotFoundError:
            return False
        with self.session() as s:
            return _vectors.has_vectors(s, rid)

    def vector_index(self, ref: str) -> "VectorIndex":
        """The profile's chunk-level index, built from its rows.

        The rows are serialized into the published byte shapes and read back by
        :class:`~researcher_profiles.embeddings.flat.FlatEmbeddingIndex`, the
        single read implementation behind all backends.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            payload = _vectors.flat_bytes(s, rid)
        if payload is None:
            from ...embeddings._sqlite import IndexNotBuiltError

            raise IndexNotBuiltError(
                f"profile {ref!r} in {self.url} has no rows in rp_chunk_vectors: "
                "it was ingested without a built index, or every chunk was private"
            )

        from ...embeddings.flat import FlatEmbeddingIndex

        return FlatEmbeddingIndex.from_bytes(*payload)

    def centroid(self, ref: str) -> "np.ndarray":
        """The profile's stored centroid: one row, already normalized.

        Not derived from :meth:`vector_index`: the centroid covers the whole
        index, private chunks included (one averaged vector is not invertible,
        spec section 6), while the chunk rows are the public subset.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            vec = _vectors.centroid(s, rid)
        if vec is None:
            return self.vector_index(rid).centroid()
        return vec

    def centroids_matrix(self) -> "tuple[list[str], np.ndarray] | None":
        """The whole roster's centroids in one ``SELECT``, or ``None``."""
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

        Vectors are written as the published flat form, regenerated from the
        rows (see :meth:`_write_flat_export`). No ``.cache/embeddings.sqlite``
        is reconstituted.

        Build state is written only when ``with_build=True``, into the build
        root beside the directory, never inside it.
        """
        rid = self.rid_for(ref)
        out = Path(dest).expanduser()
        out.mkdir(parents=True, exist_ok=True)

        with self.session() as s:
            row = s.get(ProfileRow, rid)
            if row is None:  # pragma: no cover - resolve() already proved it
                raise ProfileNotFoundError(rid)
            # ``newline=""`` throughout, so ``\n`` is never translated and the
            # export stays verbatim on every platform.
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

        The rows are the authority, not the ``embeddings/index.json`` artifact
        body, whose ``sha256`` may describe a different blob. Regenerating both
        together keeps the pair consistent.
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

        Serialized on demand from the stored ``document``.
        :func:`researcher_profiles.schema.jsonld.canonical_dumps` is a pure
        function of the mapping, so these are the bytes ``save_profile``
        persisted.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            row = s.get(ProfileRow, rid)
            if row is None:  # pragma: no cover - resolve() already proved it
                raise ProfileNotFoundError(rid)
            return canonical_dumps(row.document or {}).encode("utf-8")

    def held_artifacts(self, ref: str) -> dict[str, int | None]:
        """One ``SELECT`` over the ``rp_artifacts`` rows that hold a body.

        A rendered collection always has one (it is regenerated on the way
        out); any other row needs a ``text`` or ``data`` body. The size is the
        row's ``size_bytes``, else the stored body's length.
        """
        rid = self.rid_for(ref)
        with self.session() as s:
            rows = s.exec(
                select(
                    ArtifactRow.content_url,
                    ArtifactRow.size_bytes,
                    func.length(ArtifactRow.text),
                    func.length(ArtifactRow.data),
                )
                .where(ArtifactRow.profile_rid == rid)
                .where(
                    or_(
                        ArtifactRow.rendered == True,  # noqa: E712 - SQL expression
                        ArtifactRow.text.is_not(None),
                        ArtifactRow.data.is_not(None),
                    )
                )
            ).all()
        return {
            url: size if size is not None else (n_text or n_data)
            for url, size, n_text, n_data in rows
        }

    def artifact_bytes(self, ref: str, content_url: str) -> bytes:
        """One manifest artifact's bytes.

        No traversal check is needed: ``content_url`` is a row key, not a path.
        A ``rendered`` collection is regenerated from the tables.
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
        """The stored ``content_hash`` column, not a recompute."""
        rid = self.rid_for(ref)
        with self.session() as s:
            row = s.get(ProfileRow, rid)
            if row is None:  # pragma: no cover - resolve() already proved it
                raise ProfileNotFoundError(rid)
            if row.content_hash:
                return row.content_hash
        return self.get(rid).content_hash()
