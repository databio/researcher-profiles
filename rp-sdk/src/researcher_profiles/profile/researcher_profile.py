r"""Core ``ResearcherProfile``: identity, caches, and the public write surface.

This is the one Python class that represents a researcher profile. It holds
almost no logic of its own: it wires together a storage backend (where the
profile lives) and five capability managers (what you can do with it).

How a profile is assembled
--------------------------

* Where the profile lives is the ``storage`` backend. The same profile can be
  a folder of files (``storage.DirectoryArtifactStorage``), rows in a database
  (``store.sql.SqlArtifactStorage``), a live API server
  (``client.ApiArtifactStorage``), or a published static directory
  (``client.StaticArtifactStorage``). Each backend implements
  ``ArtifactStorage``; none of them subclasses this class.
* The transaction and hook engine is the composed
  :class:`~researcher_profiles.profile.write_unit.WriteUnit`, reached through
  :meth:`~ResearcherProfile.write_unit` /
  :meth:`~ResearcherProfile.add_pre_commit_hook` /
  :meth:`~ResearcherProfile.add_post_commit_hook`.
* Capabilities are five composed managers, each with ``get()`` (or its verb)
  as its primary read:

  =================  ====================================================
  Accessor           Manager
  =================  ====================================================
  ``prof.persona``   ``profile.persona.PersonaManager``: ask, review,
                     innovate, riff, chat
  ``prof.index``     ``profile.index.IndexManager``: build, search,
                     search_similar, embedding
  ``prof.cite``      ``profile.cite.CitationManager``: get, many, verify,
                     export
  ``prof.coverage``  ``profile.coverage.CoverageManager``: get, staleness,
                     recent_work, last_updated
  ``prof.topics``    ``profile.topics.TopicManager``: get, relevance
  =================  ====================================================

  Each is built lazily, so a core-only install still imports this module and a
  missing extra raises an ``ImportError`` naming it at first use.

Package layout
--------------

Each capability manager sits in its own file beside this one
(``profile/persona.py``, ``profile/index.py``, ``profile/cite.py``,
``profile/coverage.py``, ``profile/topics.py``) and holds only the thin manager
class. The real work lives in :mod:`~researcher_profiles.generative`,
:mod:`~researcher_profiles.embeddings`, and the ``cite`` / ``coverage`` /
``topics`` modules.

What this class keeps
---------------------

Identity (``slug`` / ``rid``), the lazy caches, and the ``save_*`` writers. Each
writer runs inside one write unit and calls one storage method, and along the
way it validates the change, stamps ``dateModified``, and updates the caches, so
every backend gets that for free.

The persona verbs raise ``PersonaUnavailableError`` when ``not self.has_persona``
(for example a ``lite`` profile that never synthesized a SOUL or expertise),
rather than role-play an empty persona. The HTTP layer maps that to a 409.
"""

import json
import logging
import os
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..build_state import BuildState
from ..errors import (
    CapabilityUnavailableError,
    ProfileError,
    ProfileValidationError,
    ProfileWriteError,
)
from ..schema import (
    ArtifactRef,
    GrantRecord,
    PaperRecord,
    ProfileDocument,
    orcid_of,
)
from ..schema.jsonld import canonical_dumps
from .storage import ArtifactStorage, DirectoryArtifactStorage
from .write_unit import WriteContext, WriteHook, WriteUnit

__all__ = [
    "ResearcherProfile",
    "UNSET",
]

logger = logging.getLogger(__name__)

UNSET: Any = object()


class ResearcherProfile:
    """A researcher profile loaded from a directory on disk.

    Use :meth:`from_files` as the documented entry point; the bare
    constructor is the same code path with no validation.
    """

    def __init__(self, storage: ArtifactStorage, *, eager: bool = False) -> None:
        # One argument, a storage backend, never a path. ``from_files`` /
        # ``from_db`` / ``from_api`` / ``from_url`` are the documented entry
        # points and each builds the right backend.
        self._storage: ArtifactStorage = storage
        self._slug: str = storage.slug

        # The transaction and hook engine: the hook lists, the open context,
        # the nesting depth, the deferred-hook flag, and the compensation list
        # all live on this object.
        self._writes = WriteUnit(self, self._storage)

        # Lazily-built capability managers. Deferred so a core-only install
        # still imports, and a missing extra raises an ImportError naming it
        # at first use rather than at import time.
        self._persona_mgr: Any = None
        self._index_mgr: Any = None
        self._cite_mgr: Any = None
        self._coverage_mgr: Any = None
        self._topics_mgr: Any = None
        self._edit_mgr: Any = None

        # lazy-load sentinels
        self._metadata: Any = UNSET
        self._build_state: Any = UNSET
        self._expertise: Any = UNSET
        self._soul: Any = UNSET
        self._papers: Any = UNSET
        self._grants: Any = UNSET
        self._citations: Any = UNSET
        self._summaries: Any = UNSET

        if eager:
            # Touch each lazy property to force loading and surface errors.
            _ = self.metadata
            _ = self.expertise
            _ = self.soul
            _ = self.papers
            _ = self.citations
            _ = self.summaries

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------

    @classmethod
    def from_files(
        cls,
        path: str | os.PathLike,
        *,
        eager: bool = False,
        validate: bool = False,
    ) -> "ResearcherProfile":
        """Construct from a local directory or a published profile URL.

        ``path`` may also be an ``http(s)://`` or ``s3://`` URL pointing at
        a statically published profile directory, in which case this
        delegates to :meth:`from_url`.

        ``validate``: run the on-disk format check and raise on any error.
        Off by default: most callers tolerate gaps (missing sources/, missing
        citations.json, etc.) which are common during profile construction.
        Requires a local directory.
        """
        if isinstance(path, str) and path.startswith(("http://", "https://", "s3://")):
            if validate:
                raise ValueError("validate=True requires a local profile directory")
            return cls.from_url(path, eager=eager)
        p = cls(DirectoryArtifactStorage(path), eager=eager)
        if validate:
            from ..validate import validate_profile_dir

            report = validate_profile_dir(p.require_directory("validate()"))
            if not report.ok:
                raise ProfileValidationError(report)
        return p

    @classmethod
    def from_api(
        cls,
        url: str,
        *,
        slug: str | None = None,
        token: str | None = None,
        timeout: float = 60.0,
        client: Any = None,
    ) -> "ResearcherProfile":
        """Construct from a live ``researcher_profiles.api`` server.

        ``url`` may be the server root (e.g. ``http://localhost:8109``), in
        which case ``slug`` must be provided, or a slug-qualified URL like
        ``http://localhost:8109/api/v1/profiles/jane-doe``.

        The returned object is an ordinary ``ResearcherProfile`` over an
        ``ApiArtifactStorage``: read-only, with an HTTP-backed ``persona`` and a
        ``search``-only ``index``. The import is deferred, same shape as
        :meth:`from_db`, so a core-only install still imports this module.
        """
        from ..client import ApiArtifactStorage, _split_profile_url

        base_url, parsed_slug = _split_profile_url(url)
        slug = slug or parsed_slug
        if slug is None:
            raise ValueError("slug required: pass slug=... or use a /api/v1/profiles/<slug> URL")
        return cls(
            ApiArtifactStorage(
                slug=slug,
                base_url=base_url,
                token=token or os.environ.get("RESEARCHER_PROFILES_TOKEN") or None,
                timeout=timeout,
                client=client,
            )
        )

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        eager: bool = False,
        timeout: float = 30.0,
        client: Any = None,
    ) -> "ResearcherProfile":
        """Construct from a published profile directory on a static host.

        ``url`` points at one profile's directory, e.g.
        ``https://profiles.example.org/jane-doe`` or
        ``s3://my-bucket/profiles/jane-doe`` (public buckets only; the ``s3``
        scheme is translated to the HTTPS endpoint). For a live API server,
        use :meth:`from_api` instead.
        """
        from ..client import StaticArtifactStorage

        return cls(StaticArtifactStorage(url, timeout=timeout, client=client), eager=eager)

    @classmethod
    def list_remote(
        cls,
        url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        client: Any = None,
    ) -> list[dict[str, Any]]:
        """List the profiles a remote server holds.

        Returns plain dicts shaped like ``ProfileSummary``. A listing is not a
        profile, so this is a classmethod on the entry-point class rather than
        anything on an instance.
        """
        from ..client import _fetch_profiles, _split_profile_url

        base_url, _ = _split_profile_url(url)
        return _fetch_profiles(
            base_url,
            token=token or os.environ.get("RESEARCHER_PROFILES_TOKEN") or None,
            timeout=timeout,
            client=client,
        )

    @classmethod
    def from_db(
        cls,
        engine_or_url: Any,
        ref: str,
        *,
        eager: bool = False,
    ) -> "ResearcherProfile":
        """Construct from the SQL profile store. Requires the ``sql`` extra.

        ``engine_or_url`` is a SQLAlchemy ``Engine`` or a connection URL; ``ref``
        is a rid or a slug (rid wins, as everywhere)::

            prof = ResearcherProfile.from_db("postgresql://…/rp", "jane-doe")

        The import below is deferred, the same shape as ``from_api`` /
        ``from_url``: naming this method must not pull SQLAlchemy, which is
        exactly the cost the ``sql`` extra exists to avoid.
        """
        from ..store.sql import SqlProfileStore

        return SqlProfileStore(engine_or_url).get(ref, eager=eager)

    # ------------------------------------------------------------------
    # Locators
    # ------------------------------------------------------------------

    @property
    def storage(self) -> "ArtifactStorage":
        """The composed backend. Where every artifact of this profile lives."""
        return self._storage

    @property
    def directory(self) -> Path | None:
        """The filesystem directory backing this profile, or ``None``.

        This is the only filesystem admission. Only the serve-time derived
        caches under ``.cache/`` (which ``ArtifactStorage`` does not cover) may
        branch on it, and they should use :meth:`require_directory`.
        """
        return self._storage.directory

    def require_directory(self, what: str) -> Path:
        """The backing directory, or a :class:`CapabilityUnavailableError`.

        The one place a directory-needing capability asks, so the message
        naming the way out is written once instead of in three backends.
        """
        d = self.directory
        if d is None:
            raise CapabilityUnavailableError(
                f"{what} needs a local profile directory; this profile is backed "
                f"by {self.locate()}. Export it to a directory "
                "(SqlProfileStore.export_directory / `rp db pull`, or "
                "client.install_profile) and load that with "
                "ResearcherProfile.from_files"
            )
        return d

    def locate(self, *parts: str) -> str:
        """A human-readable locator for a logical artifact, display only.

        Never parse this, never join to it, never open it. Storage backends
        return whatever names the artifact best: a path, a URL, a table/row
        reference. It exists so error messages and CLI output can say where
        something lives without anyone assuming that where is a directory.
        """
        return self._storage.locate(*parts)

    def persisted_document(self) -> dict[str, Any]:
        """The serialized document the store currently holds; ``{}`` when absent.

        The ``dateModified`` comparison basis. Public because an ingest
        legitimately needs it, though it is not :attr:`metadata`, which is
        validated and may carry unsaved edits.
        """
        return self._storage.load_persisted_document()

    def content_hash(self) -> str:
        """``"sha256:<hex>"`` over the canonical document and the SOUL text.

        Store-maintained derived state, refreshed inside the write unit before
        the pre-commit hooks run, so a hook reading it observes post-write
        content. Identical across backends by construction.
        """
        return self._storage.content_hash()

    # ------------------------------------------------------------------
    # Lazy properties
    # ------------------------------------------------------------------

    @property
    def slug(self) -> str:
        """Display handle, derived from the profile directory name.

        This is not identity. It is a human-readable label that carries no
        authority, may collide across registry roots, and may be renamed
        freely by renaming the directory. Never persist it as a foreign
        key, and never use it to join across systems. Use :attr:`rid` instead.
        """
        return self._slug

    @property
    def rid(self) -> str:
        """The identity of this profile: a canonical ORCID or a ``local:`` id.

        The single join key for every cross-system mapping. Required on disk;
        a profile without one fails to load.
        """
        return self.metadata.rid

    def rid_or_empty(self) -> str:
        """The rid, or ``""`` when no document is readable yet.

        A profile being created has no readable document; identity is not a
        precondition for writing one. Used to name a write unit.
        """
        try:
            return self.rid
        except ProfileError:
            return ""

    # ==================================================================
    # The write unit
    # ==================================================================
    #
    # Persistence itself lives on the composed ``storage`` backend; what stays
    # here is the hook registration a management host uses, and
    # the public ``save_*`` writers below, which carry the validation,
    # ``dateModified`` stamping and cache bookkeeping every backend inherits
    # for free.

    def add_pre_commit_hook(self, hook: WriteHook) -> None:
        """Register a callable to run inside every write unit, before commit.

        Hooks run in registration order and receive one :class:`WriteContext`.
        A hook that raises aborts the write (see :meth:`write_unit`).

        Registration belongs on the store, not on an HTTP app: a hook
        registered here fires for API routes, CLI writes, and out-of-process
        pipeline writes alike. :meth:`ProfileStore.add_pre_commit_hook`
        is the store-level entry point that threads hooks onto every profile
        it hands out.
        """
        self._writes.add_pre_commit_hook(hook)

    def _run_pre_commit_hooks(self, ctx: "WriteContext") -> None:
        """Fire the pre-commit hooks for this unit, exactly once."""
        self._writes.run_pre_commit_hooks(ctx)

    def add_post_commit_hook(self, hook: WriteHook) -> None:
        """Register a callable to run after a write unit commits successfully.

        The pre-commit/post-commit distinction is about what a hook is allowed
        to do, not only when it runs. A pre-commit hook observes a write in
        progress and may abort it (a raise rolls the unit back). A post-commit
        hook observes a write that has already landed and must not abort
        anything, because there is nothing left to roll back. Use this for
        fire-and-forget notifications to something outside the store (e.g.
        pushing to an external search index) where blocking, or failing, a
        profile write on that system's availability would be wrong.

        Fires exactly once per outermost :meth:`write_unit`, after that unit's
        own commit. Nested units never fire it, mirroring how pre-commit
        hooks collapse a batch of writes wrapped in one explicit ``write_unit``
        into a single hook run. A raising hook is caught and logged at
        ``WARNING``, never propagated; see :meth:`_fire_post_commit_hooks`.

        Registration belongs on the store, not on an HTTP app, for the same
        reason ``add_pre_commit_hook`` does: it must fire for API routes, CLI
        writes, and out-of-process pipeline writes alike.
        """
        self._writes.add_post_commit_hook(hook)

    def write_unit(self, kind: str) -> AbstractContextManager["WriteContext"]:
        """The transactional boundary for one logical write.

        A one-line delegation to
        :meth:`researcher_profiles.profile.write_unit.WriteUnit.open`, which owns the
        ordering and failure contracts. This stays a real method because
        ``api.deps`` and the guardrail suite monkeypatch it.
        """
        return self._writes.open(kind)

    # ------------------------------------------------------------------
    # Public cached properties (call the hooks)
    # ------------------------------------------------------------------

    def _cached(self, name: str, loader: Any) -> Any:
        """Read a cache, including a value staged by the open write unit."""
        staged, value = self._writes.pending_cache(name)
        if staged:
            return loader() if value is UNSET else value
        value = getattr(self, name)
        if value is UNSET:
            value = loader()
            setattr(self, name, value)
        return value

    @property
    def metadata(self) -> ProfileDocument:
        """The parsed ``profile.jsonld`` document."""
        return self._cached("_metadata", self._storage.load_document)

    @property
    def build_state(self) -> BuildState:
        """Build bookkeeping from ``.build/<slug>/meta/build_state.json``.

        Not part of the published record. Empty when the sidecar is absent.
        """
        return self._cached("_build_state", self._storage.load_build_state)

    @property
    def provenance(self) -> str:
        """Who asserted this profile and on what basis."""
        return str(self.metadata.provenance)

    @property
    def license(self) -> str | None:
        """Reuse terms for the published record."""
        return self.metadata.license_

    # Convenience delegations to metadata.

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def orcid(self) -> str | None:
        """Derived from :attr:`rid`; ``None`` for a locally-minted identity."""
        return orcid_of(self.rid)

    @property
    def affiliation(self) -> str | None:
        return self.metadata.affiliation

    @property
    def field(self) -> str | None:
        return self.metadata.field

    @property
    def summary(self) -> str | None:
        return self.metadata.summary

    @property
    def expertise(self) -> str:
        return self._cached("_expertise", self._storage.load_expertise)

    @property
    def soul(self) -> str:
        return self._cached("_soul", self._storage.load_soul)

    @property
    def level(self) -> str:
        """Profile depth tier: ``"lite"`` / ``"full"`` / ``"deep"``.

        Read from ``metadata.level`` only. Every profile carries ``level``
        explicitly, so there is no config fallback and no "absent means full"
        chain to disagree with.
        """
        return str(self.metadata.level)

    @property
    def has_persona(self) -> bool:
        """Whether this profile can role-play as a synthesized persona.

        True only for a ``full`` or ``deep`` profile that carries both a
        ``soul`` and an ``expertise`` narrative; those two documents are what
        the persona prompt is built from. A ``lite`` profile never synthesizes
        them. Summaries and papers feed retrieval grounding only and do not
        make a profile persona-ready.
        """
        if self.level not in ("full", "deep"):
            return False
        return bool(self.expertise.strip()) and bool(self.soul.strip())

    @property
    def papers(self) -> list[PaperRecord]:
        return self._cached("_papers", self._storage.load_papers)

    @property
    def grants(self) -> list[GrantRecord]:
        """Grant records from ``sources/grants.jsonld``; empty when absent."""
        return self._cached("_grants", self._storage.load_grants)

    @property
    def citations(self) -> Any:
        return self._cached("_citations", self._storage.load_citations)

    @property
    def summaries(self) -> Mapping[str, str]:
        return self._cached("_summaries", self._storage.load_summaries)

    # ------------------------------------------------------------------
    # Capability managers
    # ------------------------------------------------------------------
    #
    # Five sub-objects, one per capability, each built on first access. A
    # backend may supply its own for the two that can be served remotely
    # (``ArtifactStorage.persona`` / ``.index``); the other three are always
    # local because they are pure functions of artifacts this profile already
    # has, or of the derived caches under ``.cache/``.

    @property
    def persona(self) -> Any:
        """``prof.persona``: ask, review, innovate, riff, chat.

        ``ApiArtifactStorage`` supplies an HTTP-backed manager that POSTs to a server
        owning the model, the corpus and the refusal policy; every other
        backend gets the local
        :class:`researcher_profiles.profile.persona.PersonaManager`.
        """
        if self._persona_mgr is None:
            supplied = self._storage.persona(self)
            if supplied is None:
                from .persona import PersonaManager

                supplied = PersonaManager(self)
            self._persona_mgr = supplied
        return self._persona_mgr

    @property
    def index(self) -> Any:
        """``prof.index``: build, search, search_similar, embedding."""
        if self._index_mgr is None:
            supplied = self._storage.index(self)
            if supplied is None:
                from .index import IndexManager

                supplied = IndexManager(self)
            self._index_mgr = supplied
        return self._index_mgr

    @property
    def cite(self) -> Any:
        """``prof.cite``: get, many, verify, export."""
        if self._cite_mgr is None:
            from .cite import CitationManager

            self._cite_mgr = CitationManager(self)
        return self._cite_mgr

    @property
    def coverage(self) -> Any:
        """``prof.coverage``: get, staleness, recent_work, last_updated."""
        if self._coverage_mgr is None:
            from .coverage import CoverageManager

            self._coverage_mgr = CoverageManager(self)
        return self._coverage_mgr

    @property
    def topics(self) -> Any:
        """``prof.topics``: get, relevance."""
        if self._topics_mgr is None:
            from .topics import TopicManager

            self._topics_mgr = TopicManager(self)
        return self._topics_mgr

    @property
    def edit(self) -> Any:
        """Owner metadata, SOUL, and visibility changes."""
        if self._edit_mgr is None:
            from .edit import EditManager

            self._edit_mgr = EditManager(self)
        return self._edit_mgr

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> Any:
        """Validate this profile directory against the on-disk format schemas.

        Returns a
        :class:`researcher_profiles.validate.ProfileValidationReport`, which
        carries an ``ok`` property, the per-artifact results, and the
        violations behind them.
        """
        from ..validate import validate_profile_dir

        return validate_profile_dir(self.require_directory("validate()"))

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self, *, include_summaries: bool = False) -> dict[str, Any]:
        """Return a JSON-serializable dict representation.

        By default ``summary_ids`` (only the keys) is included to keep
        payloads small. Pass ``include_summaries=True`` to substitute
        ``summaries: {id: text}`` with the full bodies.
        """
        out: dict[str, Any] = {
            "slug": self.slug,
            "rid": self.rid,
            "path": self.locate(),
            "provenance": self.provenance,
            "license": self.license,
            "metadata": self.metadata.model_dump(mode="json"),
            "expertise": self.expertise,
            "soul": self.soul,
            "papers": [p.model_dump(mode="json") for p in self.papers],
        }
        if include_summaries:
            out["summaries"] = {k: self.summaries[k] for k in self.summaries}
        else:
            out["summary_ids"] = list(self.summaries.keys())
        return out

    def to_agent_seed(self) -> dict[str, Any]:
        """Return the minimal dict an external agent runtime needs to seed
        an agent row from this profile.

        Intended for downstream tools that want to
        ground a simulated agent in a real researcher's profile but do not
        need the full ``to_dict()`` payload at seeding time. Includes only
        identity fields plus the full expertise/soul markdown bodies and a
        paper count.
        """
        return {
            "slug": self.slug,
            # `rid` is the join key a downstream runtime should key its agent
            # row on; `slug` is a display handle and `orcid` is derived.
            "rid": self.rid,
            "name": self.metadata.name,
            "level": self.level,
            "orcid": self.metadata.orcid,
            "provenance": self.provenance,
            "license": self.license,
            "affiliation": self.metadata.affiliation,
            "field": self.metadata.field,
            "summary": self.metadata.summary,
            "expertise_md": self.expertise,
            "soul_md": self.soul,
            "n_papers": len(self.papers),
        }

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def manifest(self) -> list[ArtifactRef]:
        """The manifest as recorded in ``profile.jsonld`` (hasPart + subjectOf)."""
        return [*self.metadata.has_part, *self.metadata.subject_of]

    def build_manifest(self, *, write: bool = False) -> list[ArtifactRef]:
        """Generate the manifest from what the backend actually holds.

        The backend answers with a directory walk for files, the
        ``rp_artifacts`` rows for SQL, or the recorded manifest for a static
        host, so there is no directory walk here.

        With ``write=True`` the regenerated ``hasPart`` / ``subjectOf`` are
        stored back through :meth:`save_profile`, which stamps
        ``dateModified``: a manifest whose ``sha256`` entries moved is a real
        content change and the vintage must follow it.
        """
        parts, subjects = self._storage.build_manifest()
        if write:
            self.save_profile(
                self.metadata.model_copy(update={"has_part": parts, "subject_of": subjects})
            )
        return [*parts, *subjects]

    # ------------------------------------------------------------------
    # Public write surface
    # ------------------------------------------------------------------
    #
    # The only methods anything outside this module may call to persist. Each
    # one validates, stamps where applicable, runs inside exactly one
    # ``write_unit``, calls exactly one ``storage.save_*`` method, and updates
    # the in-memory cache slot after the unit exits cleanly so the object
    # matches what was persisted.

    def _persist_artifact(self, kind: str, value: Any, save: Any, cache_name: str) -> None:
        """Persist one simple artifact and stage its cache for outer commit."""
        with self.write_unit(kind) as ctx:
            save(value)
            self._storage.refresh_derived(ctx)
            self._run_pre_commit_hooks(ctx)
            self._writes.defer_cache_update(cache_name, value)

    def save_profile(self, doc: ProfileDocument | None = None) -> ProfileDocument:
        """Validate, stamp ``dateModified``, and persist the profile document.

        The single write path for the profile document. ``doc`` defaults to
        the current in-memory :attr:`metadata`.

        The order matters and is fixed: dump -> stamp against what the store
        already holds -> canonicalize -> re-validate -> persist. Re-validation
        happens before the storage hook is called, so a document that would
        fail to load never reaches the store and the in-memory profile is
        untouched.

        Stamping compares serialized dicts, not models: pydantic prunes
        ``None`` and empty collections, and comparing an un-normalized input
        against a normalized stored document would report a change on every
        write (see :mod:`researcher_profiles.utils.date_modified`). Any
        ``dateModified`` already sitting in the dumped document is ignored:
        it was carried over from whatever was loaded, and honouring it would
        freeze the stamp at whatever the first build wrote.

        Returns the re-parsed :class:`ProfileDocument`.
        """
        from ..utils.date_modified import stamp_date_modified

        candidate = self.metadata if doc is None else doc
        data = candidate.model_dump(mode="json")
        previous = self._storage.load_persisted_document()
        stamped = stamp_date_modified(data, previous)
        raw = canonical_dumps(stamped)
        try:
            reparsed = ProfileDocument.model_validate(json.loads(raw))
        except (ValidationError, ValueError) as e:
            raise ProfileWriteError(
                self.locate("profile.jsonld"),
                f"refusing to persist an invalid profile document: {e}",
                e,
            ) from e

        with self.write_unit("document") as ctx:
            if previous:
                self._writes.register_compensation(lambda: self._storage.save_document(previous))
            self._storage.save_document(json.loads(raw))
            self._storage.refresh_derived(ctx)
            self._run_pre_commit_hooks(ctx)
            self._writes.defer_cache_update("_metadata", reparsed)
        return reparsed

    def save_expertise(self, text: str | None = None) -> None:
        """Persist the expertise narrative; ``None`` persists what is in memory."""
        value = self.expertise if text is None else text
        self._persist_artifact("expertise", value, self._storage.save_expertise, "_expertise")

    def save_soul(self, text: str | None = None) -> None:
        """Persist the SOUL narrative; ``None`` persists what is in memory."""
        value = self.soul if text is None else text
        self._persist_artifact("soul", value, self._storage.save_soul, "_soul")

    def save_papers(self, papers: list[PaperRecord] | None = None) -> None:
        """Persist the paper corpus; ``None`` persists what is in memory."""
        value = self.papers if papers is None else papers
        self._persist_artifact("papers", value, self._storage.save_papers, "_papers")

    def save_grants(self, grants: list[GrantRecord] | None = None) -> None:
        """Persist the grant records; ``None`` persists what is in memory."""
        value = self.grants if grants is None else grants
        self._persist_artifact("grants", value, self._storage.save_grants, "_grants")

    def save_citations(self, data: Any = UNSET) -> None:
        """Persist the citation graph; ``None`` deletes it.

        Omitting ``data`` persists what is in memory. That is why the
        default is the ``UNSET`` sentinel and not ``None``: ``None`` is a
        meaningful value here.
        """
        value = self.citations if data is UNSET else data
        self._persist_artifact("citations", value, self._storage.save_citations, "_citations")

    def save_summary(self, paper_id: str, text: str) -> None:
        """Persist one paper summary body."""
        with self.write_unit("summary") as ctx:
            self._storage.save_summary(paper_id, text)
            self._storage.refresh_derived(ctx)
            self._run_pre_commit_hooks(ctx)
            self._writes.defer_cache_update("_summaries", UNSET)

    def delete_summary(self, paper_id: str) -> None:
        """Remove one paper summary body; absent is not an error."""
        with self.write_unit("summary") as ctx:
            self._storage.delete_summary(paper_id)
            self._storage.refresh_derived(ctx)
            self._run_pre_commit_hooks(ctx)
            self._writes.defer_cache_update("_summaries", UNSET)

    def save_build_state(self, state: BuildState | None = None) -> None:
        """Persist build state; ``None`` persists what is in memory.

        Build state is separate from published content. A backend may serve
        published content while having no build state at
        all. That is exactly what ``ReadOnlyArtifactStorage.load_build_state``
        models by returning an empty :class:`BuildState`.
        """
        value = self.build_state if state is None else state
        self._persist_artifact("build_state", value, self._storage.save_build_state, "_build_state")

    def set_paper_contaminated(self, paper_id: str, contaminated: bool) -> bool:
        """Flag a paper contaminated. Returns True if the paper exists.

        Contamination is build state, so this mutates
        ``.build/<slug>/meta/build_state.json`` through :meth:`save_build_state`
        and never touches the published record.
        """
        if not any(p.paper_id == paper_id for p in self.papers):
            return False
        state = self.build_state.model_copy(deep=True)
        state.paper(paper_id).contaminated = contaminated
        self.save_build_state(state)
        return True

    # ------------------------------------------------------------------
    # Dunders
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        papers_str = "?" if self._papers is UNSET else str(len(self._papers))
        # Name the backend, then whatever identity is already cached: a repr
        # must never trigger a load.
        head = f"ResearcherProfile(key={self._storage.key!r}, slug={self._slug!r}"
        if self._metadata is UNSET:
            return f"{head}, papers={papers_str})"
        return (
            f"{head}, rid={self._metadata.rid!r}, "
            f"name={self._metadata.name!r}, papers={papers_str})"
        )

    def close(self) -> None:
        """Release whatever the backend holds open (an HTTP client, say).

        A no-op on a backend with nothing to close, so
        ``with ResearcherProfile.from_api(...) as prof:`` works everywhere.
        """
        closer = getattr(self._storage, "close", None)
        if closer is not None:
            closer()

    def refresh(self) -> None:
        """Drop cached reads so the next access re-fetches.

        Clears this profile's own lazy slots and asks the backend to drop any
        caching of its own (``ApiArtifactStorage`` holds the combined detail payload).
        """
        self._metadata = UNSET
        self._build_state = UNSET
        self._expertise = UNSET
        self._soul = UNSET
        self._papers = UNSET
        self._grants = UNSET
        self._citations = UNSET
        self._summaries = UNSET
        backend_refresh = getattr(self._storage, "refresh", None)
        if backend_refresh is not None:
            backend_refresh()

    def __enter__(self) -> "ResearcherProfile":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _identity(self) -> str:
        """The opaque identity token two profiles are compared on.

        ``storage.key``: ``file:/abs/path``, ``db:<url>#<rid>``,
        ``api:<base>/<slug>``, ``static:<base>``. Compared, never parsed.
        """
        return self._storage.key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ResearcherProfile):
            return NotImplemented
        return self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())
