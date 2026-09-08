"""``SqlArtifactStorage``: one profile's ``rp_*`` rows as a
:class:`~researcher_profiles.profile.storage.ArtifactStorage`.
"""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Optional

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from ...build_state import BuildState
from ...errors import ProfileError, ProfileLoadError, ProfileWriteError
from ...profile import ResearcherProfile
from ...profile.storage import ArtifactStorage, LazySummaries, NoLocalIndex
from ...profile.write_unit import WriteContext
from ...schema import (
    ArtifactRef,
    GrantRecord,
    GrantsDocument,
    PaperRecord,
    PapersDocument,
    ProfileDocument,
)
from ...schema.jsonld import canonical_dumps
from ..db import (
    DEFAULT_MANIFEST_SLOT,
    ArtifactRow,
    BuildStateRow,
    GrantRow,
    PaperRow,
    ProfileRow,
    content_hash_for,
)
from ._shared import (
    CITATIONS_URL,
    EXPERTISE_URL,
    GRANTS_URL,
    PAPERS_URL,
    SOUL_URL,
    SUMMARY_ROLES,
    SUMMARY_SUFFIX,
    _summary_id,
    artifact_rows,
    render_collection,
)

if TYPE_CHECKING:  # pragma: no cover
    from ._store import SqlProfileStore


class SqlArtifactStorage(ArtifactStorage):
    """One profile's ``rp_*`` rows.

    Writes run in a real transaction: :attr:`session` is the SQLAlchemy
    ``Session`` the unit opened, ``WriteContext.atomic`` is ``True``, and the
    whole unit (every ``save_*`` call inside it, the derived-state refresh,
    and every pre-commit hook) commits or rolls back together.

    Nothing here ever touches a filesystem: :attr:`directory` is ``None`` and
    the capabilities that need one refuse with a message naming
    :meth:`SqlProfileStore.export_directory`.
    """

    def __init__(self, store: "SqlProfileStore", rid: str, *, slug: str):
        self._store = store
        self._rid = rid
        self._slug = slug
        #: The session of the open write unit, or ``None``. Every writer goes
        #: through this; opening a second connection inside a unit would
        #: deadlock against rows the unit already holds. Public because
        #: ``SqlProfileStore.create`` inserts the bare row through the unit's
        #: own session, via ``WriteContext.session``.
        self.session: Optional[Session] = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SqlArtifactStorage(url={self._store.url!r}, rid={self._rid!r})"

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def key(self) -> str:
        return f"db:{self._store.url}#{self._rid}"

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def rid_hint(self) -> str:
        """The row key, known without reading a document.

        A stored profile is addressed by rid before it is loaded, which is
        also what lets a write unit name the profile it is writing while the
        document is being replaced.
        """
        return self._rid

    @property
    def directory(self) -> None:
        """Always ``None``: rows are not a directory."""
        return None

    def locate(self, *parts: str) -> str:
        """Display-only locator: a ``db://`` reference naming the row."""
        tail = "/".join(parts)
        base = f"db://{self._store.url}#{self._rid}"
        return f"{base}/{tail}" if tail else base

    # ------------------------------------------------------------------
    # Session plumbing
    # ------------------------------------------------------------------

    @contextmanager
    def _read(self) -> Iterator[Session]:
        """A session for a read. Joins the open write unit's session if any.

        Joining matters: a read inside a write unit must observe that unit's
        uncommitted rows, or :meth:`refresh_derived` would hash the pre-write
        document and every hook would see stale content.
        """
        if self.session is not None:
            yield self.session
            return
        with self._store.session() as s:
            yield s

    def _require_session(self, what: str) -> Session:
        if self.session is None:  # pragma: no cover - every writer opens a unit
            raise ProfileWriteError(
                self.locate(what),
                "no open write unit; every persistence path must go through "
                "ResearcherProfile.write_unit",
            )
        return self.session

    def _profile_row(self, s: Session) -> ProfileRow:
        row = s.get(ProfileRow, self._rid)
        if row is None:
            raise ProfileLoadError(
                self.locate("profile.jsonld"),
                f"no rp_profiles row with rid={self._rid!r}",
            )
        return row

    # ------------------------------------------------------------------
    # The profile document
    # ------------------------------------------------------------------

    def load_persisted_document(self) -> dict[str, Any]:
        """The document currently in the store; ``{}`` when absent.

        Comparison basis for ``dateModified``, not a load path: errors here
        are swallowed, exactly as on the filesystem.

        The stored document carries ``hasPart``/``subjectOf`` regenerated from
        the artifact rows, so an empty manifest is persisted as ``[]`` here
        while the canonical model dump the incoming write is compared against
        omits those keys entirely. Left as-is, ``content_changed`` would read
        the extra ``[]`` slots as a change on every write (including an
        identical re-push) and churn ``dateModified`` and ``content_hash``,
        which would make every re-push look like an edit and break ``If-Match``
        sync. Empty manifest slots are dropped so this basis matches the model
        dump; a genuinely non-empty slot is untouched, so removing real parts is
        still seen as the content change it is.
        """
        try:
            with self._read() as s:
                row = s.get(ProfileRow, self._rid)
                doc = dict(row.document or {}) if row is not None else {}
        except SQLAlchemyError:  # pragma: no cover - an unreadable predecessor is no predecessor
            return {}
        for slot in ("hasPart", "subjectOf"):
            if not doc.get(slot):
                doc.pop(slot, None)
        return doc

    def load_document(self) -> ProfileDocument:
        with self._read() as s:
            row = self._profile_row(s)
            data = row.document or {}
        try:
            return ProfileDocument.model_validate(data)
        except (ValidationError, ValueError) as e:
            raise ProfileLoadError(self.locate("profile.jsonld"), f"schema error: {e}", e) from e

    def save_document(self, data: Mapping[str, Any]) -> None:
        """Persist the document, and keep it consistent with ``rp_artifacts``.

        Three things happen here, in order, and the order is the contract:

        1. The incoming manifest is applied onto the artifact rows, so an
           owner edit (``set_visibility``) reaches the rows rather than living
           only in a JSON blob the rows disagree with.
        2. ``hasPart`` / ``subjectOf`` are regenerated from the rows. The rows
           are the manifest; the document's copy is a projection of them, which
           is what makes "the document and the rows disagree" unrepresentable.
        3. The regenerated document and every scalar projection are written
           through the single writer, :meth:`ProfileRow.from_document`.

        ``data`` arrives already stamped, canonicalized and re-validated by
        :meth:`ResearcherProfile.save_profile`, so nothing that would fail to
        load reaches the store.
        """
        s = self._require_session("profile.jsonld")
        row = self._profile_row(s)
        payload = dict(data)

        rows = {r.content_url: r for r in artifact_rows(s, self._rid)}
        try:
            incoming = ProfileDocument.model_validate(payload)
        except (ValidationError, ValueError) as e:  # pragma: no cover - save_profile validated
            raise ProfileWriteError(
                self.locate("profile.jsonld"), f"refusing to persist: {e}", e
            ) from e
        for part in [*incoming.has_part, *incoming.subject_of]:
            existing = rows.get(part.content_url)
            if existing is not None:
                existing.apply_part(part)
                s.add(existing)
        s.flush()

        parts, subjects = self._manifest_rows(s)
        payload["hasPart"] = [p.model_dump(mode="json") for p in parts]
        payload["subjectOf"] = [p.model_dump(mode="json") for p in subjects]
        payload = json.loads(canonical_dumps(payload))
        meta = ProfileDocument.model_validate(payload)

        fresh = ProfileRow.from_document(
            meta, slug=row.slug, document=payload, soul=self.artifact_text(SOUL_URL) or ""
        )
        for name in type(row).model_fields:
            if name in ("rid", "slug"):
                continue
            setattr(row, name, getattr(fresh, name))
        s.add(row)
        s.flush()

    def _manifest_rows(self, s: Session) -> tuple[list[ArtifactRef], list[ArtifactRef]]:
        rows = artifact_rows(s, self._rid)
        return (
            [r.to_part() for r in rows if r.manifest_slot != "subjectOf"],
            [r.to_part() for r in rows if r.manifest_slot == "subjectOf"],
        )

    def content_hash(self) -> str:
        """The stored ``content_hash`` column: derived state, not recomputed.

        The write unit refreshes it (:meth:`refresh_derived`) before the
        pre-commit hooks run, so a hook reading this observes the new content.
        """
        with self._read() as s:
            row = s.get(ProfileRow, self._rid)
            if row is None or not row.content_hash:
                return content_hash_for(
                    (row.document if row is not None else {}) or {},
                    self.artifact_text(SOUL_URL) or "",
                )
            return row.content_hash

    # ------------------------------------------------------------------
    # Free-form artifacts
    # ------------------------------------------------------------------

    def _artifact_row(self, s: Session, content_url: str) -> Optional[ArtifactRow]:
        return s.exec(
            select(ArtifactRow)
            .where(ArtifactRow.profile_rid == self._rid)
            .where(ArtifactRow.content_url == content_url)
        ).first()

    def artifact_text(self, content_url: str) -> Optional[str]:
        """One artifact's stored text body, or ``None``.

        A ``rendered`` collection has no stored body; it is regenerated from
        ``rp_papers`` / ``rp_grants`` on the way out.
        """
        with self._read() as s:
            row = self._artifact_row(s, content_url)
            if row is None:
                return None
            if row.rendered:
                return render_collection(s, self._rid, row)
            return row.text

    def artifact_bytes(self, content_url: str) -> Optional[bytes]:
        with self._read() as s:
            row = self._artifact_row(s, content_url)
            if row is None:
                return None
            if row.rendered:
                body = render_collection(s, self._rid, row)
                return None if body is None else body.encode("utf-8")
            if row.text is not None:
                return row.text.encode("utf-8")
            return bytes(row.data) if row.data is not None else None

    def collection_envelope(self, content_url: str) -> dict:
        """A collection node minus ``hasPart``, so a re-render is faithful."""
        with self._read() as s:
            row = self._artifact_row(s, content_url)
            if row is not None and row.envelope:
                return {k: v for k, v in dict(row.envelope).items() if k != "hasPart"}
        return self._empty_envelope(content_url)

    def _empty_envelope(self, content_url: str) -> dict:
        about = None
        try:
            about = self.load_document().id_
        except ProfileError:  # pragma: no cover - a profile mid-creation
            pass
        doc = (
            PapersDocument(about=about, has_part=[])
            if content_url == PAPERS_URL
            else GrantsDocument(about=about, has_part=[])
        )
        return {k: v for k, v in doc.model_dump(mode="json").items() if k != "hasPart"}

    def _write_artifact(
        self,
        content_url: str,
        text: Optional[str],
        *,
        role: str,
        name: str,
        encoding_format: str = "text/markdown",
        manifest_slot: str = DEFAULT_MANIFEST_SLOT,
        paper_id: Optional[str] = None,
    ) -> None:
        """Upsert one artifact row, creating its manifest entry when new."""
        s = self._require_session(content_url)
        row = self._artifact_row(s, content_url)
        if row is None:
            part = ArtifactRef(
                **{
                    "@type": "DigitalDocument",
                    "name": name,
                    "encodingFormat": encoding_format,
                    "contentUrl": content_url,
                    "role": role,
                    "paperId": paper_id,
                }
            )
            ordinal = self._next_ordinal(s, manifest_slot)
            row = ArtifactRow.from_part(
                self._rid, part, manifest_slot=manifest_slot, ordinal=ordinal, text=text
            )
        else:
            row.text = text
        s.add(row)
        s.flush()

    def _next_ordinal(self, s: Session, manifest_slot: str) -> int:
        rows = [r for r in artifact_rows(s, self._rid) if r.manifest_slot == manifest_slot]
        return (max((r.ordinal for r in rows), default=-1)) + 1

    def _delete_artifact(self, content_url: str) -> None:
        s = self._require_session(content_url)
        row = self._artifact_row(s, content_url)
        if row is not None:
            s.delete(row)
            s.flush()

    # --- persona documents -----------------------------------------------

    def load_expertise(self) -> str:
        return self.artifact_text(EXPERTISE_URL) or ""

    def save_expertise(self, text: str) -> None:
        self._write_artifact(
            EXPERTISE_URL,
            text,
            role="expertise",
            name="Expertise",
            manifest_slot="subjectOf",
        )

    def load_soul(self) -> str:
        return self.artifact_text(SOUL_URL) or ""

    def save_soul(self, text: str) -> None:
        self._write_artifact(SOUL_URL, text, role="soul", name="SOUL", manifest_slot="subjectOf")

    # --- collections -------------------------------------------------------

    def load_papers(self) -> list[PaperRecord]:
        with self._read() as s:
            records = [
                r.record
                for r in s.exec(
                    select(PaperRow)
                    .where(PaperRow.profile_rid == self._rid)
                    .order_by(PaperRow.ordinal)
                ).all()
            ]
        try:
            return [PaperRecord.model_validate(r) for r in records]
        except (ValidationError, ValueError) as e:
            raise ProfileLoadError(self.locate(PAPERS_URL), f"schema error: {e}", e) from e

    def save_papers(self, papers: list[PaperRecord]) -> None:
        s = self._require_session(PAPERS_URL)
        for row in s.exec(select(PaperRow).where(PaperRow.profile_rid == self._rid)).all():
            s.delete(row)
        s.flush()
        for ordinal, paper in enumerate(papers):
            s.add(PaperRow.from_record(self._rid, paper, ordinal=ordinal))
        self._ensure_rendered_row(s, PAPERS_URL, name="Works", role="works")
        s.flush()

    def load_grants(self) -> list[GrantRecord]:
        with self._read() as s:
            records = [
                r.record
                for r in s.exec(
                    select(GrantRow)
                    .where(GrantRow.profile_rid == self._rid)
                    .order_by(GrantRow.ordinal)
                ).all()
            ]
        try:
            return [GrantRecord.model_validate(r) for r in records]
        except (ValidationError, ValueError) as e:
            raise ProfileLoadError(self.locate(GRANTS_URL), f"schema error: {e}", e) from e

    def save_grants(self, grants: list[GrantRecord]) -> None:
        s = self._require_session(GRANTS_URL)
        for row in s.exec(select(GrantRow).where(GrantRow.profile_rid == self._rid)).all():
            s.delete(row)
        s.flush()
        for ordinal, grant in enumerate(grants):
            s.add(GrantRow.from_record(self._rid, grant, ordinal=ordinal))
        self._ensure_rendered_row(s, GRANTS_URL, name="Grants", role="grants")
        s.flush()

    def _ensure_rendered_row(self, s: Session, content_url: str, *, name: str, role: str) -> None:
        """A collection always keeps its manifest row, envelope and all."""
        row = self._artifact_row(s, content_url)
        if row is not None:
            return
        part = ArtifactRef(
            **{
                "@type": "Collection",
                "name": name,
                "encodingFormat": "application/ld+json",
                "contentUrl": content_url,
                "role": role,
            }
        )
        s.add(
            ArtifactRow.from_part(
                self._rid,
                part,
                manifest_slot="hasPart",
                ordinal=self._next_ordinal(s, "hasPart"),
                rendered=True,
                envelope=self._empty_envelope(content_url),
            )
        )

    # --- citations and summaries -------------------------------------------

    def load_citations(self) -> Any:
        raw = self.artifact_text(CITATIONS_URL)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProfileLoadError(self.locate(CITATIONS_URL), f"JSON parse error: {e}", e) from e

    def save_citations(self, data: Any) -> None:
        """Write the citation graph; ``None`` DELETES the artifact.

        Same asymmetry the filesystem backend has: :meth:`load_citations`
        already models "legitimately absent" as ``None``, so ``None`` here is a
        delete rather than a stored JSON ``null``.
        """
        if data is None:
            self._delete_artifact(CITATIONS_URL)
            return
        self._write_artifact(
            CITATIONS_URL,
            json.dumps(data, indent=2),
            role="citations",
            name="Citations",
            encoding_format="application/json",
        )

    def load_summaries(self) -> Mapping[str, str]:
        with self._read() as s:
            rows = s.exec(select(ArtifactRow).where(ArtifactRow.profile_rid == self._rid)).all()
        keys: dict[str, str] = {}
        for row in rows:
            if row.role in SUMMARY_ROLES or row.content_url.endswith(SUMMARY_SUFFIX):
                sid = _summary_id(row)
                if sid:
                    keys[sid] = row.content_url
        return LazySummaries(keys, lambda k: self.artifact_text(keys[k]) or "")

    @staticmethod
    def _summary_url(paper_id: str) -> str:
        return f"sources/summaries/{paper_id}{SUMMARY_SUFFIX}"

    def save_summary(self, paper_id: str, text: str) -> None:
        self._write_artifact(
            self._summary_url(paper_id),
            text,
            role="paper_summary",
            name=f"Summary: {paper_id}",
            paper_id=paper_id,
        )

    def delete_summary(self, paper_id: str) -> None:
        self._delete_artifact(self._summary_url(paper_id))

    # --- build state ---------------------------------------------------------

    def load_build_state(self) -> BuildState:
        """Build state from ``rp_build_state``; an empty state when absent.

        Absent is normal: a published store legitimately has none, the
        same contract a published directory has. A missing table is absent too:
        ``DROP TABLE rp_build_state`` has to stay as free as ``rm -rf .build/``,
        so a store published without that table must still serve every profile.
        """
        from sqlalchemy.exc import DatabaseError

        try:
            with self._read() as s:
                row = s.get(BuildStateRow, self._rid)
                data = dict(row.state or {}) if row is not None else None
        except DatabaseError:
            return BuildState()
        if not data:
            return BuildState()
        return BuildState.model_validate(data)

    def save_build_state(self, state: BuildState) -> None:
        s = self._require_session("build_state.json")
        row = s.get(BuildStateRow, self._rid)
        payload = state.model_dump(mode="json", exclude_none=True)
        if row is None:
            row = BuildStateRow(profile_rid=self._rid)
        row.schema_version = int(state.schema_version)
        row.state = payload
        s.add(row)
        s.flush()

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def build_manifest(self) -> tuple[list[ArtifactRef], list[ArtifactRef]]:
        """Generate the manifest from ``rp_artifacts``, not by walking a path.

        The rows are the directory listing.
        """
        with self._read() as s:
            return self._manifest_rows(s)

    # ------------------------------------------------------------------
    # Transaction
    # ------------------------------------------------------------------

    def new_write_context(self, profile: ResearcherProfile, kind: str) -> WriteContext:
        """Open a transaction and hand it to the unit.

        ``atomic=True``: everything inside this unit, hooks included, commits
        or rolls back together. That is the whole reason a hook is told to
        branch on ``ctx.atomic`` rather than on ``session is None``.
        """
        self.session = Session(self._store.engine)
        return WriteContext(
            profile=profile,
            slug=self._slug,
            rid=self._rid,
            kind=kind,
            session=self.session,
            atomic=True,
        )

    def refresh_derived(self, ctx: WriteContext) -> None:  # noqa: ARG002
        """Recompute ``content_hash`` inside the unit, before the hooks run.

        The digest spans the document and the SOUL, so a soul-only write has to
        refresh it too. That is why this is the write unit's job and
        not :meth:`save_document`'s.
        """
        s = self.session
        if s is None:  # pragma: no cover - only reachable outside a unit
            return
        row = s.get(ProfileRow, self._rid)
        if row is None:
            return
        row.content_hash = content_hash_for(row.document or {}, self.artifact_text(SOUL_URL) or "")
        s.add(row)
        s.flush()

    def commit(self, ctx: WriteContext) -> None:  # noqa: ARG002
        if self.session is None:  # pragma: no cover
            return
        try:
            self.session.commit()
        finally:
            self.session.close()
            self.session = None

    def rollback(self, ctx: WriteContext) -> None:  # noqa: ARG002
        """A real rollback: nothing this unit wrote survives.

        Contrast the filesystem backend, which can only issue a compensating
        write for the profile document.
        """
        if self.session is None:  # pragma: no cover
            return
        try:
            self.session.rollback()
        finally:
            self.session.close()
            self.session = None

    # ------------------------------------------------------------------
    # Capabilities that need a directory
    # ------------------------------------------------------------------

    #: What every directory-needing capability says on this backend.
    EXPORT_REMEDY = (
        "export this profile with SqlProfileStore.export_directory (or "
        "`rp db pull`) and load the result with ResearcherProfile.from_files"
    )

    def index(self, profile: ResearcherProfile) -> NoLocalIndex:  # noqa: ARG002
        """No directory, so no sqlite index handle: every operation refuses."""
        return NoLocalIndex(self.EXPORT_REMEDY)
