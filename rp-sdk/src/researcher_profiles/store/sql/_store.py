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
from sqlalchemy.exc import IntegrityError
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
    SlugAliasRow,
    create_all,
)
from ..hooks import _HookedStore
from ..protocol import (
    IngestResult,
    Ownership,
    ProfileNotFoundError,
    ProfileRef,
    RetiredRidError,
    UploadError,
)
from . import _vectors
from ._shared import RENDERED_URLS, _is_memory_sqlite, _is_text, artifact_rows, render_collection
from ._storage import SqlArtifactStorage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    import numpy as np

    from ...embeddings.protocol import VectorIndex

logger = logging.getLogger(__name__)

#: What a ``ref`` may be: a slug (or a retired slug), an integer ``profile_id``,
#: or ``"#<profile_id>"``.
Ref = str | int


def _as_profile_id(ref: Any) -> Optional[int]:
    """The ``profile_id`` a ref spells, or ``None`` when it is a slug."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ref
    if isinstance(ref, str) and ref.startswith("#") and ref[1:].isdigit():
        return int(ref[1:])
    return None


def _owner_columns(owner: Optional[Ownership]) -> dict[str, Any]:
    """The ``rp_profiles`` owner columns for ``owner`` (all empty for ``None``)."""
    if owner is None:
        return {"owner_kind": None, "owner_principal_id": None, "is_self": False}
    return {
        "owner_kind": owner.kind,
        "owner_principal_id": owner.principal_id,
        "is_self": bool(owner.is_self),
    }


class SqlProfileStore(_HookedStore, _AnalyticsAccessors):
    """A set of profiles living in SQL tables.

    ``engine_or_url`` may be a SQLAlchemy ``Engine`` or a connection URL. The
    store owns no per-profile state: every operation opens its own session, and
    the :class:`SqlArtifactStorage` behind a profile handed out by :meth:`get` opens
    its own for each read and each write unit.

    ``owner_resolver`` (settable later) is asked for an :class:`Ownership`
    whenever a method is about to insert a profile row without an explicit
    ``owner``. With no resolver, or one that answers ``None``, the owner
    columns stay NULL: fine in a standalone store, refused by a host database
    that requires an owner.
    """

    def __init__(
        self,
        engine_or_url: Any,
        *,
        owner_resolver: "Optional[Callable[[], Optional[Ownership]]]" = None,
    ):
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
        self.owner_resolver = owner_resolver
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

    def evict(self, ref: Ref) -> None:  # noqa: ARG002 - protocol signature
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

    @staticmethod
    def _row_for(s: Session, ref: Ref) -> Optional[ProfileRow]:
        """The live row ``ref`` names, or ``None``.

        An integer (or ``"#<id>"``) is a ``profile_id``; a string is a slug,
        then a retired slug (``rp_slug_aliases``). A rid is never a ref.
        """
        profile_id = _as_profile_id(ref)
        if profile_id is not None:
            return s.get(ProfileRow, profile_id)
        if not isinstance(ref, str) or not ref:
            return None
        return SqlProfileStore._slug_row(s, ref)

    @staticmethod
    def _slug_row(s: Session, slug: str) -> Optional[ProfileRow]:
        """The live row ``slug`` names: the live slug, then a retired slug."""
        row = s.exec(select(ProfileRow).where(ProfileRow.slug == slug)).first()
        if row is not None:
            return row
        alias = s.get(SlugAliasRow, slug)
        return None if alias is None else s.get(ProfileRow, alias.profile_id)

    @staticmethod
    def _former_slug_error(slug: str, live: ProfileRow, rid: Optional[str]) -> RetiredRidError:
        """The refusal for a write at ``slug``, a former slug of the record ``live``."""
        about = f"; this write is about {rid!r}" if rid else ""
        return RetiredRidError(
            slug,
            live.rid,
            rid=rid,
            slug=slug,
            message=(
                f"{slug!r} is a former slug of {live.slug!r} (renamed, or merged into it), "
                f"which is about {live.rid!r}{about}; a former slug cannot be reused"
            ),
        )

    def _refuse_second_ownerless(
        self, s: Session, rid: str, slug: str, owner: Optional[Ownership]
    ) -> None:
        """Refuse an ownerless create about a rid that already has a record.

        With owners the database allows one record per owner per person. With
        none, NULL owners never collide, so nothing would stop a second record
        about one person: the same rule a directory store applies.
        """
        if owner is not None:
            return
        if s.exec(select(ProfileRow.profile_id).where(ProfileRow.rid == rid)).first() is not None:
            raise ProfileWriteError(
                self.url,
                f"cannot create {slug!r}: a profile with rid {rid!r} already exists, "
                "and a store without owners holds one record per person",
            )

    def _require_row(self, s: Session, ref: Ref) -> ProfileRow:
        """:meth:`_row_for`, raising :class:`ProfileNotFoundError` on a miss."""
        row = self._row_for(s, ref)
        if row is None:
            profile_id = _as_profile_id(ref)
            tried = (
                f"no rp_profiles row with profile_id={profile_id}"
                if profile_id is not None
                else f"no rp_profiles row with slug={ref!r}, and no retired slug {ref!r}"
            )
            raise ProfileNotFoundError(f"nothing in {self.url} matches {ref!r}: {tried}")
        return row

    def profile_id_for(self, ref: Ref) -> int:
        """The ``profile_id`` of the record ``ref`` names."""
        with self.session() as s:
            return int(self._require_row(s, ref).profile_id)

    @staticmethod
    def _rid_successor(s: Session, rid: str) -> Optional[str]:
        """The successor of a retired rid, or ``None`` (a live rid wins)."""
        alias = s.get(RidAliasRow, rid)
        if alias is None:
            return None
        if s.exec(select(ProfileRow.profile_id).where(ProfileRow.rid == rid)).first() is not None:
            return None
        return alias.successor_rid

    @staticmethod
    def _refuse_retired(s: Session, rid: Optional[str], slug: Optional[str]) -> None:
        """Raise :class:`RetiredRidError` if a write would reuse a retired rid or slug.

        Rid and slug are checked in their own namespaces: ``rp_rid_aliases``
        and ``rp_slug_aliases``. A live record always beats an alias.
        """
        if rid:
            successor = SqlProfileStore._rid_successor(s, rid)
            if successor is not None:
                raise RetiredRidError(rid, successor, rid=rid, slug=slug)
        if slug:
            alias = s.get(SlugAliasRow, slug)
            if (
                alias is not None
                and s.exec(select(ProfileRow.profile_id).where(ProfileRow.slug == slug)).first()
                is None
            ):
                target = s.get(ProfileRow, alias.profile_id)
                if target is None:
                    raise RetiredRidError(slug, "?", rid=rid, slug=slug)
                raise SqlProfileStore._former_slug_error(slug, target, rid)

    def successor_rid(self, rid: str) -> Optional[str]:
        """See :meth:`~researcher_profiles.store.ProfileStore.successor_rid`."""
        with self.session() as s:
            return self._rid_successor(s, rid)

    def profiles_about(self, rid: str) -> list[ProfileRef]:
        """See :meth:`~researcher_profiles.store.ProfileStore.profiles_about`.

        Self profile first, then by ``profile_id``. Selects the identity
        columns only, never a document.
        """
        with self.session() as s:
            target = self._rid_successor(s, rid) or rid
            rows = s.exec(
                select(
                    ProfileRow.profile_id,
                    ProfileRow.rid,
                    ProfileRow.slug,
                    ProfileRow.owner_kind,
                    ProfileRow.owner_principal_id,
                    ProfileRow.is_self,
                )
                .where(ProfileRow.rid == target)
                .order_by(ProfileRow.is_self.desc(), ProfileRow.profile_id)
            ).all()
        return [
            ProfileRef(
                profile_id=pid,
                rid=r,
                slug=slug,
                owner=(
                    Ownership(kind, principal, bool(is_self))
                    if kind is not None and principal is not None
                    else None
                ),
            )
            for pid, r, slug, kind, principal, is_self in rows
        ]

    def self_profile_for(self, rid: str) -> Optional[ResearcherProfile]:
        """See :meth:`~researcher_profiles.store.ProfileStore.self_profile_for`."""
        with self.session() as s:
            target = self._rid_successor(s, rid) or rid
            pid = s.exec(
                select(ProfileRow.profile_id).where(
                    ProfileRow.rid == target,
                    ProfileRow.is_self == True,  # noqa: E712 - SQL expression
                )
            ).first()
        return None if pid is None else self.get(pid)

    def profile_owned_by(self, rid: str, owner: Ownership) -> Optional[ResearcherProfile]:
        """See :meth:`~researcher_profiles.store.ProfileStore.profile_owned_by`."""
        with self.session() as s:
            target = self._rid_successor(s, rid) or rid
            pid = s.exec(
                select(ProfileRow.profile_id).where(
                    ProfileRow.rid == target,
                    ProfileRow.owner_kind == owner.kind,
                    ProfileRow.owner_principal_id == owner.principal_id,
                )
            ).first()
        return None if pid is None else self.get(pid)

    def alias_slugs(self) -> set[str]:
        """Every retired slug."""
        with self.session() as s:
            return set(s.exec(select(SlugAliasRow.old_slug)).all())

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

    def exists(self, ref: Ref) -> bool:
        with self.session() as s:
            return self._row_for(s, ref) is not None

    def resolve_slug(self, ref: Ref) -> str:
        """Map a ref (a slug, a retired slug or a ``profile_id``) to the live slug."""
        with self.session() as s:
            return self._require_row(s, ref).slug

    def write_lookup_index(self) -> None:
        """``None``: no directory. :meth:`profiles_about` answers the same question."""
        return None

    def list_slugs(self) -> list[str]:
        """Every profile's display handle, sorted. Selects one column only."""
        with self.session() as s:
            return list(s.exec(select(ProfileRow.slug).order_by(ProfileRow.slug)).all())

    def list_profiles(self) -> list[ProfileRow]:
        """Every profile row in the store, ordered by slug."""
        with self.session() as s:
            return list(s.exec(select(ProfileRow).order_by(ProfileRow.slug)).all())

    def get(self, ref: Ref, *, eager: bool = False) -> ResearcherProfile:
        """Load one profile, backed by a :class:`SqlArtifactStorage` over its rows."""
        with self.session() as s:
            row = self._require_row(s, ref)
            profile_id, slug, rid = int(row.profile_id), row.slug, row.rid
        return self._admit(
            ResearcherProfile(SqlArtifactStorage(self, profile_id, slug=slug, rid=rid), eager=eager)
        )

    def _owner_for_insert(self, owner: Optional[Ownership]) -> Optional[Ownership]:
        """``owner``, else what :attr:`owner_resolver` says, else ``None``."""
        if owner is not None:
            return owner
        return self.owner_resolver() if self.owner_resolver is not None else None

    # --- ingest -------------------------------------------------------------

    def put(
        self,
        profile: ResearcherProfile,
        *,
        slug: Optional[str] = None,
        owner: Optional[Ownership] = None,
        include_binary: bool = False,
    ) -> int:
        """Write ``profile`` into the store, in one transaction. Returns its ``profile_id``.

        Takes any ``ResearcherProfile``. Keyed on the slug: a live slug is a
        replace (same record, same ``profile_id``, same owner; the rid must
        match), and so is a former slug (renamed or merged) whose record is
        about the same rid, which keeps that record's live slug. A new slug
        is a create (owned by ``owner``, else the ``owner_resolver``). Every
        child row is replaced rather than diffed, so the rows always equal
        the source.

        Artifacts come from the profile's recorded manifest when it has one
        (see :meth:`_manifest_of`), else from walking the directory.

        ``include_binary`` opts into storing binary bodies; without it a binary
        artifact keeps its manifest row and loses only its bytes. Vectors are
        shredded on every put regardless.

        Raises :class:`RetiredRidError` when a create would reuse a rid a
        merge retired, or a former slug's record is about another rid;
        ``ProfileWriteError`` when a live slug's rid differs, or when a store
        without owners already holds a record about the rid. That covers
        :meth:`commit_directory` and :meth:`import_directory`.
        """
        handle = slug or profile.slug
        # Resolved before the session opens: a resolver may read the host's
        # own tables, and must not share this write's connection mid-transaction.
        owner = self._owner_for_insert(owner)
        with self.session() as s:
            profile_id = self._write_profile_rows(
                s, profile, slug=handle, owner=owner, include_binary=include_binary
            )
            s.commit()
        self._bump_generation()
        return profile_id

    def _write_profile_rows(
        self,
        s: Session,
        profile: ResearcherProfile,
        *,
        slug: str,
        include_binary: bool,
        owner: Optional[Ownership] = None,
        row: Optional[ProfileRow] = None,
    ) -> int:
        """Write ``profile``'s rows on ``s``. Flushes, never commits. Returns the ``profile_id``.

        ``row`` (else the live row ``slug`` names, through a former slug
        too) is replaced in place: its identity columns stay, its live slug
        included, and its content and children are rewritten. With no live
        row a new one is inserted, owned by ``owner`` (already resolved by
        the caller). The parent is never deleted and re-inserted, which would
        mint a new ``profile_id`` and orphan every host row keyed on it.
        Shared by :meth:`put` and :meth:`merge_into`.
        """
        meta = profile.metadata
        rid = meta.rid
        document = profile.persisted_document() or json.loads(
            canonical_dumps(meta.model_dump(mode="json"))
        )
        soul = profile.soul or ""
        parts = self._manifest_of(profile)
        fresh = ProfileRow.from_document(meta, slug=slug, document=document, soul=soul)

        live = row if row is not None else self._slug_row(s, slug)
        if live is not None:
            if live.rid != rid and live.slug != slug:
                raise self._former_slug_error(slug, live, rid)
            if live.rid != rid:
                raise ProfileWriteError(
                    self.url,
                    f"cannot replace {slug!r}: staged rid {rid!r} does not match "
                    f"existing rid {live.rid!r}",
                )
            live.take_content(fresh)
            s.add(live)
            profile_id = int(live.profile_id)
            self._delete_children(s, profile_id)
        else:
            self._refuse_retired(s, rid, slug)
            self._refuse_second_ownerless(s, rid, slug, owner)
            fresh.rid = rid
            fresh.slug = slug
            for name, value in _owner_columns(owner).items():
                setattr(fresh, name, value)
            s.add(fresh)
            # Flush the parent BEFORE any child. These tables have FK columns but
            # no `relationship()`, so SQLAlchemy may otherwise insert children
            # first, and Postgres FKs are not deferrable. A second self profile
            # for the rid or owner, or a second record for one owner about one
            # person, is the database's refusal: a write error, not a crash.
            try:
                s.flush()
            except IntegrityError as e:
                raise ProfileWriteError(
                    self.url, f"cannot create {slug!r}: refused by the database: {e.orig}"
                ) from e
            profile_id = int(fresh.profile_id)
        s.flush()

        for ordinal, paper in enumerate(profile.papers):
            s.add(PaperRow.from_record(profile_id, paper, ordinal=ordinal))
        for ordinal, grant in enumerate(profile.grants):
            s.add(GrantRow.from_record(profile_id, grant, ordinal=ordinal))
        for ordinal, topic in enumerate(meta.expertise):
            s.add(ExpertiseTopicRow(profile_id=profile_id, ordinal=ordinal, topic=topic))

        seen: set[str] = set()
        for slot, part, ordinal in parts:
            if part.content_url in seen:
                continue
            seen.add(part.content_url)
            s.add(
                self._artifact_row(
                    profile_id, part, slot, ordinal, profile.storage, include_binary=include_binary
                )
            )

        state = profile.build_state
        if state is not None and (state.papers or state.build.completed_phases):
            s.add(
                BuildStateRow(
                    profile_id=profile_id,
                    schema_version=int(state.schema_version),
                    state=state.model_dump(mode="json", exclude_none=True),
                )
            )

        # Vectors: shredded into queryable rows; see ``_vectors``.
        for vector_row in _vectors.shred_profile(profile_id, profile):
            s.add(vector_row)
        s.flush()
        return profile_id

    def import_directory(
        self, path: str | Path, *, owner: Optional[Ownership] = None, include_binary: bool = False
    ) -> int:
        """Sugar for ``put(ResearcherProfile.from_files(path))``. Returns the ``profile_id``."""
        return self.put(
            ResearcherProfile.from_files(path), owner=owner, include_binary=include_binary
        )

    def commit_directory(
        self,
        slug: str,
        staging: Path,
        *,
        owner: Optional[Ownership] = None,
        build_missing_index: bool = False,
    ) -> IngestResult:
        """Make a fully-staged profile directory live in this store.

        One transaction, so a new record and its owner commit together or not
        at all. A live ``slug`` is a replace, and so is a former slug about
        the same rid (see :meth:`put`); the result names the live slug.

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
        profile_id = self.put(staged, slug=slug, owner=owner, include_binary=True)
        return IngestResult(
            slug=self.resolve_slug(profile_id),
            rid=staged.metadata.rid,
            name=name,
            level=level,
            indexed=indexed,
            profile_id=profile_id,
        )

    def merge_into(
        self,
        retired_ref: Ref,
        staging: Path,
        *,
        survivor_ref: Optional[Ref],
        survivor_rid: str,
        survivor_slug: str,
        build_missing_index: bool = True,
    ) -> IngestResult:
        """Retire one record into another, in ONE write unit.

        See :meth:`~researcher_profiles.store.ProfileStore.merge_into`. Hooks
        run on the unit's own session, so a host can re-key its rows in the
        same transaction.

        Raises :class:`ProfileWriteError` when the retired and survivor
        records are the same, the survivor is not about ``survivor_rid``, a
        convert keeps the rid or would make a second self profile, or the
        staged rid is not ``survivor_rid``; :class:`RetiredRidError` when
        ``survivor_rid`` was itself retired or ``survivor_slug`` redirects to
        another record; :class:`ProfileNotFoundError` when a ref names no live
        record; :class:`UploadError` when the staged directory does not load.
        """
        with self.session() as s:
            retired = self._row_for(s, retired_ref)
            if retired is None:
                raise ProfileNotFoundError(f"no live profile {retired_ref!r} in {self.url}")
            retired_pid, retired_rid, retired_slug = (
                int(retired.profile_id),
                retired.rid,
                retired.slug,
            )
            survivor_pid: Optional[int] = None
            survivor_old_slug: Optional[str] = None
            if survivor_ref is not None:
                survivor = self._row_for(s, survivor_ref)
                if survivor is None:
                    raise ProfileNotFoundError(f"no live profile {survivor_ref!r} in {self.url}")
                if int(survivor.profile_id) == retired_pid:
                    raise ProfileWriteError(self.url, "cannot merge a profile into itself")
                if survivor.rid != survivor_rid:
                    raise ProfileWriteError(
                        self.url,
                        f"survivor {survivor.slug!r} is about {survivor.rid!r}, "
                        f"not {survivor_rid!r}",
                    )
                survivor_pid, survivor_old_slug = int(survivor.profile_id), survivor.slug
            else:
                if survivor_rid == retired_rid:
                    raise ProfileWriteError(
                        self.url, f"a convert must change the rid; {retired_rid!r} is unchanged"
                    )
                self_row = s.exec(
                    select(ProfileRow.slug).where(
                        ProfileRow.rid == survivor_rid,
                        ProfileRow.is_self == True,  # noqa: E712 - SQL expression
                    )
                ).first()
                if self_row is not None:
                    raise ProfileWriteError(
                        self.url,
                        f"cannot convert {retired_slug!r} to {survivor_rid!r}: "
                        f"{self_row!r} is already that person's own profile",
                    )
            # A merge must never make a retired rid live again, nor take a slug
            # that still redirects to (or names) another record.
            self._refuse_retired(s, survivor_rid, None)
            ours = {retired_pid, survivor_pid}
            slug_alias = s.get(SlugAliasRow, survivor_slug)
            if slug_alias is not None and slug_alias.profile_id not in ours:
                self._refuse_retired(s, survivor_rid, survivor_slug)
            holder = s.exec(
                select(ProfileRow.profile_id).where(ProfileRow.slug == survivor_slug)
            ).first()
            if holder is not None and holder not in ours:
                raise ProfileWriteError(
                    self.url, f"slug {survivor_slug!r} names another profile in this store"
                )
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

        target_pid = survivor_pid if survivor_pid is not None else retired_pid
        storage = SqlArtifactStorage(self, target_pid, slug=survivor_slug, rid=survivor_rid)
        storage.context_extra = {
            "retired_profile_id": retired_pid if survivor_pid is not None else None,
            "retired_rid": retired_rid if retired_rid != survivor_rid else None,
            "retired_slug": retired_slug,
        }
        prof = self._admit(ResearcherProfile(storage))
        try:
            self._merge_unit(
                prof,
                storage,
                staged,
                retired_pid=retired_pid,
                retired_rid=retired_rid,
                retired_slug=retired_slug,
                survivor_pid=survivor_pid,
                survivor_rid=survivor_rid,
                survivor_slug=survivor_slug,
                survivor_old_slug=survivor_old_slug,
            )
        finally:
            storage.context_extra = {}
        self._bump_generation()
        return IngestResult(
            slug=survivor_slug,
            rid=survivor_rid,
            name=name,
            level=level,
            indexed=indexed,
            profile_id=target_pid,
        )

    def _merge_unit(
        self,
        prof: ResearcherProfile,
        storage: SqlArtifactStorage,
        staged: ResearcherProfile,
        *,
        retired_pid: int,
        retired_rid: str,
        retired_slug: str,
        survivor_pid: Optional[int],
        survivor_rid: str,
        survivor_slug: str,
        survivor_old_slug: Optional[str],
    ) -> None:
        """The one write unit behind :meth:`merge_into`.

        A regular merge (``survivor_pid`` given) deletes the retired record and
        rewrites the survivor; a convert rewrites the retired record under the
        new rid. Either way the record that survives keeps its ``profile_id``,
        and every slug it or the retired record was known by keeps resolving
        to it.
        """
        with prof.write_unit("merge") as ctx:
            s = ctx.session
            if survivor_pid is not None:
                # The retired record's old handles follow it onto the survivor.
                for alias in s.exec(
                    select(SlugAliasRow).where(SlugAliasRow.profile_id == retired_pid)
                ).all():
                    alias.profile_id = survivor_pid
                    s.add(alias)
                s.flush()
                self._delete_children(s, retired_pid)
                gone = s.get(ProfileRow, retired_pid)
                if gone is not None:
                    s.delete(gone)
                s.flush()
                row = s.get(ProfileRow, survivor_pid)
            else:
                row = s.get(ProfileRow, retired_pid)
                row.rid = survivor_rid
            row.slug = survivor_slug
            s.add(row)
            try:
                s.flush()
            except IntegrityError as e:
                raise ProfileWriteError(self.url, f"merge refused by the database: {e.orig}") from e
            target_pid = int(row.profile_id)
            self._write_profile_rows(s, staged, slug=survivor_slug, row=row, include_binary=True)

            if retired_rid != survivor_rid:
                # Every other record about the retired person follows the rid.
                for other in s.exec(select(ProfileRow).where(ProfileRow.rid == retired_rid)).all():
                    other.rid = survivor_rid
                    s.add(other)
                try:
                    s.flush()
                except IntegrityError as e:
                    raise ProfileWriteError(
                        self.url,
                        f"cannot move the records about {retired_rid!r} to {survivor_rid!r}: {e.orig}",
                    ) from e
                # A live rid beats an alias; collapse chains onto the survivor.
                stale = s.get(RidAliasRow, survivor_rid)
                if stale is not None:
                    s.delete(stale)
                for chained in s.exec(
                    select(RidAliasRow).where(RidAliasRow.successor_rid == retired_rid)
                ).all():
                    chained.successor_rid = survivor_rid
                    s.add(chained)
                s.flush()
                alias = s.get(RidAliasRow, retired_rid)
                if alias is None:
                    s.add(RidAliasRow(old_rid=retired_rid, successor_rid=survivor_rid))
                else:
                    alias.successor_rid = survivor_rid
                    s.add(alias)

            # Retired handles redirect to the record that survives; a live slug
            # beats an alias.
            for old in {retired_slug, survivor_old_slug} - {None, survivor_slug}:
                alias = s.get(SlugAliasRow, old)
                if alias is None:
                    s.add(SlugAliasRow(old_slug=old, profile_id=target_pid))
                else:
                    alias.profile_id = target_pid
                    s.add(alias)
            live_alias = s.get(SlugAliasRow, survivor_slug)
            if live_alias is not None:
                s.delete(live_alias)
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

    def refresh_vectors(self, ref: Ref) -> bool:
        """Rebuild one profile's vector rows from its current papers.

        Exports the stored profile to a temp directory, builds the embedding
        index there, shreds the result back into ``rp_chunk_vectors`` /
        ``rp_profile_vectors``, and cleans up. Best-effort: returns ``True``
        on success, ``False`` when the embedding stack is unavailable or the
        build fails for any reason. Never raises.
        """
        import shutil
        import tempfile

        tmp = None
        try:
            profile_id = self.profile_id_for(ref)
            tmp = Path(tempfile.mkdtemp(prefix="rp_refresh_"))
            staging = self.export_directory(profile_id, tmp / "profile")
            if not self._try_build_index(staging):
                return False
            staged = ResearcherProfile.from_files(staging)
            new_rows = _vectors.shred_profile(profile_id, staged)
            if not new_rows:
                return False
            with self.session() as s:
                for old in s.exec(
                    select(ChunkVectorRow).where(ChunkVectorRow.profile_id == profile_id)
                ).all():
                    s.delete(old)
                for old in s.exec(
                    select(ProfileVectorRow).where(ProfileVectorRow.profile_id == profile_id)
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

    def _new_profile(
        self, document: ProfileDocument, *, slug: str, owner: Optional[Ownership]
    ) -> ResearcherProfile:
        """A profile whose bare row is inserted when its first write unit opens.

        Refuses a taken slug, a retired slug and a retired rid, and, with no
        owner, a rid that already has a record. A second self profile for the
        rid or the owner is refused by the database, as a
        ``ProfileWriteError``, when the unit opens.
        """
        rid = document.rid
        # Resolved before the session opens; see :meth:`put`.
        owner = self._owner_for_insert(owner)
        with self.session() as s:
            self._refuse_retired(s, rid, slug)
            if s.exec(select(ProfileRow.profile_id).where(ProfileRow.slug == slug)).first():
                raise ProfileWriteError(
                    self.url,
                    f"cannot create {slug!r}: a profile with that slug already exists in this store",
                )
            self._refuse_second_ownerless(s, rid, slug, owner)
        storage = SqlArtifactStorage(self, None, slug=slug, rid=rid)
        storage.pending_row = ProfileRow(
            rid=rid,
            slug=slug,
            document={},
            name=document.name,
            provenance=str(document.provenance),
            **_owner_columns(owner),
        )
        return self._admit(ResearcherProfile(storage))

    def create(
        self, document: ProfileDocument, *, slug: str, owner: Optional[Ownership] = None
    ) -> ResearcherProfile:
        """Create a new profile from a validated document, in one write unit.

        The row is inserted bare and then written through
        :meth:`ResearcherProfile.save_profile`, so it takes the same path as
        every other write. ``owner`` (else the ``owner_resolver``) lands on it.
        """
        prof = self._new_profile(document, slug=slug, owner=owner)
        with prof.write_unit("create"):
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
        owner: Optional[Ownership] = None,
    ) -> ResearcherProfile:
        """Create a document and its authored artifacts in one transaction."""
        prof = self._new_profile(document, slug=slug, owner=owner)
        with prof.write_unit("create"):
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

    def put_document(
        self, slug: str, document: ProfileDocument, *, owner: Optional[Ownership] = None
    ) -> ResearcherProfile:
        """See :meth:`~researcher_profiles.store.ProfileStore.put_document`.

        A former slug (renamed or merged) whose record is about the document's
        rid replaces that record, which keeps its live slug; one about
        another rid is a :class:`RetiredRidError`.
        """
        with self.session() as s:
            live = self._slug_row(s, slug)
            if live is not None and live.slug != slug and live.rid != document.rid:
                raise self._former_slug_error(slug, live, document.rid)
            existing = None if live is None else (int(live.profile_id), live.rid)
        if existing is None:
            return self.create(document, slug=slug, owner=owner)
        profile_id, existing_rid = existing
        if document.rid != existing_rid:
            raise ProfileWriteError(
                self.url,
                f"cannot replace {slug!r}: document rid {document.rid!r} does not "
                f"match existing rid {existing_rid!r}",
            )
        prof = self.get(profile_id)
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
        profile_id: int,
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
                profile_id,
                part,
                manifest_slot=slot,
                ordinal=ordinal,
                rendered=True,
                envelope=bodies.collection_envelope(part.content_url),
            )
        if _is_text(part.encoding_format, part.content_url):
            return ArtifactRow.from_part(
                profile_id,
                part,
                manifest_slot=slot,
                ordinal=ordinal,
                text=bodies.artifact_text(part.content_url),
            )
        data = bodies.artifact_bytes(part.content_url) if include_binary else None
        return ArtifactRow.from_part(
            profile_id, part, manifest_slot=slot, ordinal=ordinal, data=data
        )

    # --- identity ------------------------------------------------------------

    def rename(self, ref: Ref, new_slug: str) -> None:
        """Change a profile's display handle. One update; no child row moves.

        The old slug is kept in ``rp_slug_aliases``, so links to it keep
        resolving. The unique constraint on ``slug`` still applies, and a slug
        that redirects to another record is refused.
        """
        with self.session() as s:
            row = self._require_row(s, ref)
            old = row.slug
            if old == new_slug:
                return
            alias = s.get(SlugAliasRow, new_slug)
            if alias is not None:
                if alias.profile_id != row.profile_id:
                    self._refuse_retired(s, None, new_slug)
                s.delete(alias)
                s.flush()
            row.slug = new_slug
            s.add(row)
            s.flush()
            s.add(SlugAliasRow(old_slug=old, profile_id=row.profile_id))
            s.commit()
        self._bump_generation()

    def delete(self, ref: Ref) -> str:
        """Remove a profile, every child row and its slug aliases. Returns its rid."""
        with self.session() as s:
            row = self._require_row(s, ref)
            profile_id, rid = int(row.profile_id), row.rid
            self._delete_children(s, profile_id)
            for alias in s.exec(
                select(SlugAliasRow).where(SlugAliasRow.profile_id == profile_id)
            ).all():
                s.delete(alias)
            s.flush()
            s.delete(row)
            s.commit()
        self._bump_generation()
        return rid

    @staticmethod
    def _delete_children(s: Session, profile_id: int) -> None:
        for model in (
            PaperRow,
            GrantRow,
            ExpertiseTopicRow,
            ArtifactRow,
            ChunkVectorRow,
            ProfileVectorRow,
            BuildStateRow,
        ):
            for row in s.exec(select(model).where(model.profile_id == profile_id)).all():
                s.delete(row)
        s.flush()

    # --- the manifest ---------------------------------------------------------

    def manifest_from_rows(self, ref: Ref) -> tuple[list[ArtifactRef], list[ArtifactRef]]:
        """``(hasPart, subjectOf)`` from ``rp_artifacts``.

        The row-based twin of ``build_manifest``.
        """
        with self.session() as s:
            rows = artifact_rows(s, int(self._require_row(s, ref).profile_id))
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

    def has_vector_index(self, ref: Ref) -> bool:
        """Whether ``ref`` has chunk vectors. A ``LIMIT 1``, never a fetch."""
        with self.session() as s:
            row = self._row_for(s, ref)
            return row is not None and _vectors.has_vectors(s, int(row.profile_id))

    def vector_index(self, ref: Ref) -> "VectorIndex":
        """The profile's chunk-level index, built from its rows.

        The rows are serialized into the published byte shapes and read back by
        :class:`~researcher_profiles.embeddings.flat.FlatEmbeddingIndex`, the
        single read implementation behind all backends.
        """
        with self.session() as s:
            payload = _vectors.flat_bytes(s, int(self._require_row(s, ref).profile_id))
        if payload is None:
            from ...embeddings._sqlite import IndexNotBuiltError

            raise IndexNotBuiltError(
                f"profile {ref!r} in {self.url} has no rows in rp_chunk_vectors: "
                "it was ingested without a built index, or every chunk was private"
            )

        from ...embeddings.flat import FlatEmbeddingIndex

        return FlatEmbeddingIndex.from_bytes(*payload)

    def centroid(self, ref: Ref) -> "np.ndarray":
        """The profile's stored centroid: one row, already normalized.

        Not derived from :meth:`vector_index`: the centroid covers the whole
        index, private chunks included (one averaged vector is not invertible,
        spec section 6), while the chunk rows are the public subset.
        """
        with self.session() as s:
            profile_id = int(self._require_row(s, ref).profile_id)
            vec = _vectors.centroid(s, profile_id)
        if vec is None:
            return self.vector_index(profile_id).centroid()
        return vec

    def centroids_matrix(self) -> "tuple[list[str], np.ndarray] | None":
        """The whole roster's centroids in one ``SELECT``, or ``None``."""
        with self.session() as s:
            return _vectors.centroids_matrix(s)

    # --- export --------------------------------------------------------------

    def export_directory(self, ref: Ref, dest: str | Path, *, with_build: bool = False) -> Path:
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
        out = Path(dest).expanduser()
        out.mkdir(parents=True, exist_ok=True)

        with self.session() as s:
            row = self._require_row(s, ref)
            profile_id = int(row.profile_id)
            # ``newline=""`` throughout, so ``\n`` is never translated and the
            # export stays verbatim on every platform.
            (out / "profile.jsonld").write_text(
                canonical_dumps(row.document or {}), encoding="utf-8", newline=""
            )

            for art in artifact_rows(s, profile_id):
                target = out / art.content_url
                target.parent.mkdir(parents=True, exist_ok=True)
                if art.rendered:
                    body = render_collection(s, profile_id, art)
                    if body is not None:
                        target.write_text(body, encoding="utf-8", newline="")
                elif art.text is not None:
                    target.write_text(art.text, encoding="utf-8", newline="")
                elif art.data is not None:
                    target.write_bytes(art.data)

            self._write_flat_export(s, profile_id, out)

            if with_build:
                state_row = s.get(BuildStateRow, profile_id)
                if state_row is not None:
                    BuildState.model_validate(state_row.state or {}).save(out)
        return out

    @staticmethod
    def _write_flat_export(s: Session, profile_id: int, out: Path) -> None:
        """Write ``embeddings/`` from the vector rows. No-op without any.

        The rows are the authority, not the ``embeddings/index.json`` artifact
        body, whose ``sha256`` may describe a different blob. Regenerating both
        together keeps the pair consistent.
        """
        payload = _vectors.flat_bytes(s, profile_id)
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

    def document_bytes(self, ref: Ref) -> bytes:
        """The canonical ``profile.jsonld`` bytes for ``ref``.

        Serialized on demand from the stored ``document``.
        :func:`researcher_profiles.schema.jsonld.canonical_dumps` is a pure
        function of the mapping, so these are the bytes ``save_profile``
        persisted.
        """
        with self.session() as s:
            row = self._require_row(s, ref)
            return canonical_dumps(row.document or {}).encode("utf-8")

    def held_artifacts(self, ref: Ref) -> dict[str, int | None]:
        """One ``SELECT`` over the ``rp_artifacts`` rows that hold a body.

        A rendered collection always has one (it is regenerated on the way
        out); any other row needs a ``text`` or ``data`` body. The size is the
        row's ``size_bytes``, else the stored body's length.
        """
        with self.session() as s:
            profile_id = int(self._require_row(s, ref).profile_id)
            rows = s.exec(
                select(
                    ArtifactRow.content_url,
                    ArtifactRow.size_bytes,
                    func.length(ArtifactRow.text),
                    func.length(ArtifactRow.data),
                )
                .where(ArtifactRow.profile_id == profile_id)
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

    def artifact_bytes(self, ref: Ref, content_url: str) -> bytes:
        """One manifest artifact's bytes.

        No traversal check is needed: ``content_url`` is a row key, not a path.
        A ``rendered`` collection is regenerated from the tables.
        """
        with self.session() as s:
            profile_id = int(self._require_row(s, ref).profile_id)
            row = s.exec(
                select(ArtifactRow)
                .where(ArtifactRow.profile_id == profile_id)
                .where(ArtifactRow.content_url == content_url)
            ).first()
            if row is None:
                raise ProfileNotFoundError(
                    f"artifact {content_url!r} is not an artifact of {ref!r} in {self.url}"
                )
            if row.rendered:
                body = render_collection(s, profile_id, row)
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

    def content_hash(self, ref: Ref) -> str:
        """The stored ``content_hash`` column, not a recompute."""
        with self.session() as s:
            row = self._require_row(s, ref)
            profile_id = int(row.profile_id)
            if row.content_hash:
                return row.content_hash
        return self.get(profile_id).content_hash()
