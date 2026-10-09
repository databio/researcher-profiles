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

The ``save_*`` writers validate, stamp ``dateModified``, and update the caches
inside one write unit, so every backend gets that for free.

The persona verbs raise ``PersonaUnavailableError`` when ``not self.has_persona``
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
    TrialRecord,
    orcid_of,
)
from ..schema.jsonld import canonical_dumps
from .storage import CLINICAL_EXPERTISE_URL, ArtifactStorage, DirectoryArtifactStorage
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
        self._storage: ArtifactStorage = storage
        self._slug: str = storage.slug

        self._writes = WriteUnit(self, self._storage)

        # Built on first access; see the module docstring.
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
        self._trials: Any = UNSET
        self._citations: Any = UNSET
        self._summaries: Any = UNSET

        if eager:
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

        The result is read-only, with an HTTP-backed ``persona`` and a
        ``search``-only ``index``.
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
        """List the profiles a remote server holds, as dicts shaped like ``ProfileSummary``."""
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
        """
        from ..store.sql import SqlProfileStore

        return SqlProfileStore(engine_or_url).get(ref, eager=eager)

    # ------------------------------------------------------------------
    # Locators
    # ------------------------------------------------------------------

    @property
    def storage(self) -> "ArtifactStorage":
        """The storage backend holding every artifact of this profile."""
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
        """The backing directory, or raise :class:`CapabilityUnavailableError`."""
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

        Never parse, join, or open it. It may be a path, a URL, or a table/row
        reference.
        """
        return self._storage.locate(*parts)

    def persisted_document(self) -> dict[str, Any]:
        """The serialized document the store currently holds; ``{}`` when absent.

        The ``dateModified`` comparison basis. Unlike :attr:`metadata`, it never
        carries unsaved edits.
        """
        return self._storage.load_persisted_document()

    def content_hash(self) -> str:
        """``"sha256:<hex>"`` over the canonical document and the SOUL text.

        Refreshed inside the write unit before the pre-commit hooks run, so a
        hook sees post-write content. Identical across backends.
        """
        return self._storage.content_hash()

    # ------------------------------------------------------------------
    # Lazy properties
    # ------------------------------------------------------------------

    @property
    def slug(self) -> str:
        """Display handle, derived from the profile directory name.

        Not identity: it may collide across registry roots and may be renamed.
        Never persist it as a foreign key or join on it. Use :attr:`rid`.
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

        A profile being created has no readable document yet.
        """
        try:
            return self.rid
        except ProfileError:
            return ""

    # ------------------------------------------------------------------
    # The write unit
    # ------------------------------------------------------------------

    def add_pre_commit_hook(self, hook: WriteHook) -> None:
        """Register a callable to run inside every write unit, before commit.

        Hooks run in registration order and receive one :class:`WriteContext`.
        A hook that raises aborts the write (see :meth:`write_unit`).
        :meth:`ProfileStore.add_pre_commit_hook` registers one on every
        profile a store hands out.
        """
        self._writes.add_pre_commit_hook(hook)

    def _run_pre_commit_hooks(self, ctx: "WriteContext") -> None:
        """Fire the pre-commit hooks for this unit, exactly once."""
        self._writes.run_pre_commit_hooks(ctx)

    def add_post_commit_hook(self, hook: WriteHook) -> None:
        """Register a callable to run after a write unit commits successfully.

        For fire-and-forget notifications outside the store, where a write
        should not fail because another system is down. Fires once per
        outermost :meth:`write_unit`. A raising hook is logged at ``WARNING``,
        never propagated.
        """
        self._writes.add_post_commit_hook(hook)

    def write_unit(self, kind: str) -> AbstractContextManager["WriteContext"]:
        """The transactional boundary for one logical write.

        See :meth:`researcher_profiles.profile.write_unit.WriteUnit.open` for
        the ordering and failure contracts. ``api.deps`` and the guardrail
        suite monkeypatch this method.
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

        Read from ``metadata.level`` only; there is no fallback.
        """
        return str(self.metadata.level)

    @property
    def has_persona(self) -> bool:
        """Whether this profile can role-play as a synthesized persona.

        True only for a ``full`` or ``deep`` profile with both a ``soul`` and an
        ``expertise`` narrative, which the persona prompt is built from.
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
    def trials(self) -> list[TrialRecord]:
        """Clinical trials from ``sources/trials.jsonld``; empty when absent.

        The optional clinical extension; empty is normal.
        """
        return self._cached("_trials", self._storage.load_trials)

    @property
    def clinical_expertise(self) -> str:
        """``personality/clinical_expertise.md``; ``""`` when absent."""
        return self._storage.artifact_text(CLINICAL_EXPERTISE_URL) or ""

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
    # A backend may supply its own ``persona`` and ``index``; the other three
    # are always local because they depend only on local artifacts and caches.

    @property
    def persona(self) -> Any:
        """``prof.persona``: ask, review, innovate, riff, chat."""
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
        """Return the minimal dict an external agent runtime needs to seed an agent.

        Identity fields, the expertise/soul bodies, and a paper count. Key
        agent rows on ``rid``.
        """
        return {
            "slug": self.slug,
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

        With ``write=True`` the result is stored through :meth:`save_profile`,
        which stamps ``dateModified``: changed ``sha256`` entries are a real
        content change.
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
    # runs in one ``write_unit``, calls one ``storage.save_*`` method, and
    # updates the cache only after the unit exits cleanly.

    def _persist_artifact(self, kind: str, value: Any, save: Any, cache_name: str) -> None:
        """Persist one simple artifact and stage its cache for outer commit."""
        with self.write_unit(kind) as ctx:
            save(value)
            self._storage.refresh_derived(ctx)
            self._run_pre_commit_hooks(ctx)
            self._writes.defer_cache_update(cache_name, value)

    def save_profile(self, doc: ProfileDocument | None = None) -> ProfileDocument:
        """Validate, stamp ``dateModified``, and persist the profile document.

        ``doc`` defaults to the in-memory :attr:`metadata`.

        Order is fixed: dump, stamp against the stored document, canonicalize,
        re-validate, persist. A document that would fail to load never reaches
        the store. Stamping compares serialized dicts (see
        :mod:`researcher_profiles.utils.date_modified`); any incoming
        ``dateModified`` is ignored so the stamp cannot freeze.

        Returns the re-parsed :class:`ProfileDocument`.
        """
        from ..schema import strip_registry_issued_from_document
        from ..utils.date_modified import stamp_date_modified

        candidate = self.metadata if doc is None else doc
        # Registry-issued proofs are computed when served, never stored.
        # Stripping here keeps the cached document identical to the stored one.
        data = strip_registry_issued_from_document(
            candidate.model_dump(mode="json"), where=self.locate("profile.jsonld")
        )
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

    def save_trials(self, trials: list[TrialRecord] | None = None) -> None:
        """Persist the clinical trials; ``None`` persists what is in memory."""
        value = self.trials if trials is None else trials
        self._persist_artifact("trials", value, self._storage.save_trials, "_trials")

    def save_clinical_expertise(self, text: str) -> None:
        """Persist ``personality/clinical_expertise.md``."""
        with self.write_unit("clinical_expertise") as ctx:
            self._storage.write_artifact(
                CLINICAL_EXPERTISE_URL,
                text,
                role="clinical_expertise",
                name="Clinical expertise",
                encoding_format="text/markdown",
                manifest_slot="subjectOf",
            )
            self._storage.refresh_derived(ctx)
            self._run_pre_commit_hooks(ctx)

    def save_citations(self, data: Any = UNSET) -> None:
        """Persist the citation graph; ``None`` deletes it.

        Omitting ``data`` persists what is in memory.
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

        Build state is not published content; a read-only backend has none.
        """
        value = self.build_state if state is None else state
        self._persist_artifact("build_state", value, self._storage.save_build_state, "_build_state")

    def set_paper_contaminated(self, paper_id: str, contaminated: bool) -> bool:
        """Flag a paper contaminated. Returns True if the paper exists.

        Contamination is build state; the published record is untouched.
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
        # A repr must never trigger a load.
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

        Also asks the backend to drop its own caches.
        """
        self._metadata = UNSET
        self._build_state = UNSET
        self._expertise = UNSET
        self._soul = UNSET
        self._papers = UNSET
        self._grants = UNSET
        self._trials = UNSET
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
