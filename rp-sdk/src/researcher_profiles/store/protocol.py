"""The store contract and its value types.

:class:`ProfileStore` is the structural (``Protocol``) interface a backend
implements: enumerate profiles, resolve a reference, create/delete one, hand
out its bytes. :class:`VectorStore` is the optional *capability* protocol on
top of it: a backend that can also serve a profile's vectors.
:class:`IngestResult`, :class:`ProfileNotFoundError`, :class:`UploadError` and
:class:`DuplicateIdentityError` are the value and error types that cross that
boundary. See the package docstring for why these are protocols and not base
classes.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

from ..errors import ProfileError
from ..profile import ResearcherProfile
from ..profile.write_unit import WriteHook

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

    from ..analytics.centroids import CentroidManager
    from ..analytics.indexes import IndexFleetManager
    from ..analytics.match import MatchManager
    from ..embeddings.protocol import VectorIndex
    from ..schema import ProfileDocument


class ProfileNotFoundError(ProfileError, KeyError):
    """No profile in the store answers to a given rid or slug.

    Both a :class:`~researcher_profiles.profile.ProfileError` and a
    ``KeyError``. The API layer's 404 path and every ``except KeyError`` around
    a store lookup read "no such key" (which is exactly what this is), while a
    management host wants to catch it alongside the package's other errors.
    Making it one or the other would force every caller of the other kind to
    grow a second ``except`` clause.
    """

    def __str__(self) -> str:
        # ``KeyError.__str__`` reprs its argument, so a message would arrive
        # wrapped in quotes. These carry a sentence, not a key.
        return str(self.args[0]) if self.args else ""


class UploadError(ValueError):
    """A staged profile directory is malformed, unsafe, or does not load.

    Raised by the archive helpers in :mod:`researcher_profiles.api.upload` and
    by :meth:`ProfileStore.commit_directory`. It lives here, not there, because
    committing a staged directory is a *store* operation that both backends
    perform and neither should have to import the HTTP layer to signal.
    """


class DuplicateIdentityError(ValueError):
    """Two profiles in one store claim the same ``rid``.

    A store is the only place this can be detected, because it is the only
    place that sees every profile at once. The filesystem backend is the only
    one that can hit it: SQL makes it structurally impossible (``rid`` is the
    primary key) and a published site's ``by-rid.json`` is a mapping that
    cannot hold two entries under one key.
    """


@dataclass
class IngestResult:
    """Outcome of committing one staged profile directory into a store."""

    slug: str
    rid: str | None
    name: str
    level: str
    #: Whether a built embedding index is present afterwards. Always ``False``
    #: on a store with no filesystem: the index is a ``.cache/embeddings.sqlite``
    #: handle, which ``ArtifactStorage`` does not cover (see the module docstring).
    indexed: bool


@runtime_checkable
class ProfileStore(Protocol):
    """A set of profiles, whatever they are stored in.

    Every method takes ``ref``: a slug or a rid. Resolution is the store's job,
    not the caller's. An HTTP route that had to know which namespace it was
    handed would be re-implementing :meth:`resolve_slug` at every entry point.
    """

    # identity of the store itself

    @property
    def location(self) -> str:
        """A human-readable locator for this store. Display only.

        A directory path, a database URL. Never parse it or join to it. That is
        the same rule :meth:`ResearcherProfile.locate` states one level down.
        """
        ...

    @property
    def root(self) -> Optional[Path]:
        """The filesystem root when this store is a directory, else ``None``.

        The one filesystem admission; see the module docstring. Vectors are no
        longer among the things that need it: they have :class:`VectorStore`.
        This list is the whole sanctioned set -- it is the single copy, and
        ``store/__init__.py`` points here rather than restating it. What still
        branches on ``root``, and nothing else may:

        * ``<root>/.cache/graph.sqlite`` (the co-authorship graph), which has no
          interface yet. Read in ``api.deps``, which turns a ``None`` root into
          a 503 for graph-shaped features, or materializes a temp directory.
        * The optional write-through memos ``<root>/.cache/centroids.npz`` and
          ``<root>/.cache/topics.json``, which a store without a directory simply
          does without (``analytics.centroids``, ``analytics.match``, and
          ``analytics.roster``, which re-exposes the value as ``_Roster.root``
          for those two).
        * Staging and archiving paths that want to ``os.rename`` on the store's
          own filesystem (``api.upload``, ``api.routers.push``); each already
          has a scratch-space fallback for ``None``.
        * Invalidating those same store-wide caches after a write
          (``api._projection``). They sit outside ``ArtifactStorage``, so
          nothing else can drop them; a ``None`` root means there are none.

        ``tests/test_guardrails.py`` holds the allowlist to match, so a new
        reader fails the suite until it is added here too.
        """
        ...

    # enumeration and lookup

    def list_slugs(self) -> list[str]:
        """Every profile's display handle, sorted."""
        ...

    def resolve_slug(self, ref: str) -> str:
        """Map a slug or a rid to the slug. Raises :class:`ProfileNotFoundError`."""
        ...

    def rid_for(self, ref: str) -> str:
        """Map a slug or a rid to the rid. Raises :class:`ProfileNotFoundError`.

        The mirror of :meth:`resolve_slug`. Both exist because the two are used
        for different things: ``rid`` is the identity every cross-system
        mapping joins on; ``slug`` is what a URL and a human say. A bare ORCID
        resolves here too, because an ORCID rid *is* its ORCID
        (``scholarcore.identity.orcid_of`` derives one from the other and
        invents nothing).
        """
        ...

    def write_lookup_index(self) -> Optional[Path]:
        """Persist ``rid <-> slug`` so shell callers resolve without importing.

        A shell caller resolving a rid to a directory reads this file, which is
        what lets a rid work anywhere a slug does regardless of directory name.
        Nothing writes it implicitly: a caller that owns a writable root calls
        this when it wants the file fresh. Returns ``None`` on a store with no
        directory to write into, which is not a degradation: such a store
        answers the same question in process through :meth:`rid_for`.
        """
        ...

    def exists(self, ref: str) -> bool:
        """Whether ``ref`` names a profile in this store."""
        ...

    def get(self, ref: str) -> ResearcherProfile:
        """Load a profile. Raises :class:`ProfileNotFoundError`.

        The returned profile carries every hook registered through
        :meth:`add_pre_commit_hook`, so a write through it fires them whether it
        came from an HTTP route, the CLI, or an out-of-process pipeline run.
        """
        ...

    # mutation

    def create(self, document: "ProfileDocument", *, slug: str) -> ResearcherProfile:
        """Create a new profile from a validated document. Returns it.

        Runs in one write unit of kind ``"create"``, so a management host's
        pre-commit hook can write its ownership row in the same transaction.
        That closes the orphan-profile window between "the profile
        exists" and "somebody owns it".

        Raises :class:`~researcher_profiles.profile.ProfileWriteError` if
        ``slug`` or the document's rid is already taken.
        """
        ...

    def put_document(self, slug: str, document: "ProfileDocument") -> "ResearcherProfile":
        """Create-or-replace a profile's canonical profile.jsonld.

        Runs in one write unit (kind ``"create"`` if new, ``"edit"`` if it
        exists), so hooks fire and ``dateModified`` is stamped. Derived
        artifacts (``sources/``, ``.cache/``) are left untouched: this writes
        the document, not the bundle.

        This is the JSON upsert path: what ``PUT /api/v1/profiles/{slug}``
        dispatches to when the request body is ``application/json`` rather
        than a tarball. It writes identity + expertise + metadata; enrichment
        (papers, embeddings) is added later by a push or a build.

        Returns the profile, so the caller can read ``rid`` / ``name`` /
        ``level`` for the response.
        """
        ...

    def delete(self, ref: str) -> str:
        """Remove a profile and everything belonging to it. Returns its rid."""
        ...

    def commit_directory(
        self, slug: str, staging: Path, *, build_missing_index: bool = False
    ) -> IngestResult:
        """Make a fully-staged profile directory live in this store.

        ``staging`` is a complete profile directory (``profile.jsonld`` at its
        root). The directory is the interchange format, not the storage
        format: a tarball push, a URL import and ``rp db push`` all speak it.
        Each backend decides what "live" means: an atomic rename for the
        filesystem, a transaction for SQL.

        ``build_missing_index`` asks for a best-effort embedding-index build
        afterwards, for a host that wants an ingested profile immediately
        rankable. A backend with no filesystem accepts it and ignores it.

        Raises :class:`UploadError` if the staged directory does not load.
        """
        ...

    def export_directory(self, ref: str, dest: Path) -> Path:
        """Materialize a profile as a directory at ``dest``. Returns ``dest``.

        The inverse of :meth:`commit_directory`, and the way out of any store
        for the capabilities that need a real directory (validation,
        the embedding index).
        """
        ...

    # bytes

    def document_bytes(self, ref: str) -> bytes:
        """The canonical ``profile.jsonld`` bytes, exactly as persisted.

        What a crawler fetching ``/profiles/{slug}/profile.jsonld`` receives has
        to be the published document, byte for byte, or the ``conformsTo`` claim
        is about a file nobody can retrieve.
        """
        ...

    def artifact_bytes(self, ref: str, content_url: str) -> bytes:
        """One manifest artifact's bytes, addressed by its ``contentUrl``.

        Raises :class:`ProfileNotFoundError` when the profile or the artifact is
        absent. Applies no privacy policy: the caller has already decided who
        may read this (see ``api.routers.read.get_profile_artifact``).
        """
        ...

    def content_hash(self, ref: str) -> str:
        """``"sha256:<hex>"`` over the canonical document and the SOUL text.

        Store-maintained derived state, refreshed inside the write unit before
        the pre-commit hooks run, so a hook reading it observes post-write
        content. Identical across backends by construction; see
        :func:`researcher_profiles.store.db.content_hash_for`.
        """
        ...

    # hooks

    def add_pre_commit_hook(self, hook: WriteHook) -> None:
        """Register a callable to run inside every write, before commit.

        Registration belongs here, on the store, and not on an HTTP app: a hook
        registered on an app fires only for writes that went through a route
        that remembered to fire it, so a background writer that bypasses the
        routes would skip it. Applies to profiles handed out from now
        on and to any already handed out, so registration order relative to a
        first :meth:`get` does not matter.
        """
        ...

    def add_post_commit_hook(self, hook: WriteHook) -> None:
        """Register a callable to run after a write commits successfully.

        Same reach as :meth:`add_pre_commit_hook`. The difference is what the
        hook may do: a pre-commit hook can still abort the write by raising; a
        post-commit hook cannot, because the write already landed. Use this for
        fire-and-forget notifications to something outside the store (for
        example, pushing to an external search index). See
        ``researcher_profiles.profile.ResearcherProfile.add_post_commit_hook``.
        """
        ...

    def evict(self, ref: str) -> None:
        """Drop any cached view of ``ref`` so the next :meth:`get` reloads.

        A no-op on a store that does not cache. Cache invalidation only:
        dependent-state maintenance belongs on :meth:`add_pre_commit_hook`,
        where it runs inside the write instead of after it.
        """
        ...


@runtime_checkable
class VectorStore(ProfileStore, Protocol):
    """A store that can also hand out a profile's vectors.

    All three SDK backends satisfy it today, out of three different materials
    (a sqlite file, fetched bytes, SQL rows). It is still a *capability* and
    deliberately not part of :class:`ProfileStore`, for two reasons that do not
    depend on which backends happen to exist:

    1. Not every backend has vectors to give. A store over a bare metadata
       API, or one holding profiles that were never indexed, has none. Folding
       these methods into the base contract would make such a backend one that
       "implements" the store while raising on half of it; failing an
       ``isinstance`` check is the honest answer.
    2. The base contract must stay numpy-free. ``rp list`` over a 300-member
       roster imports ``ProfileStore`` and must not pull the ``vectors`` extra;
       a guardrail asserts exactly that. The array types here are under
       ``TYPE_CHECKING`` for the same reason.

    The check happens once, up front, so a composition root raises
    :class:`~researcher_profiles.errors.CapabilityUnavailableError` with an
    actionable message rather than letting every analytic discover the gap
    separately and degrade into a different 503. It is *not*
    ``isinstance(store, VectorStore)``: the one place that check lives,
    :func:`researcher_profiles.store._analytics.require_vector_store`, is a
    ``hasattr`` sweep over the five vector method names. This class is
    ``@runtime_checkable``, but it also declares the three analytics accessors
    below, and an ``isinstance`` would evaluate the very property that is doing
    the checking.

    The three accessors at the bottom are the cross-profile analytics, reached
    the way ``prof.cite`` / ``prof.topics`` are reached one level down. They
    are supplied by a shared mixin
    (:class:`researcher_profiles.store._analytics._AnalyticsAccessors`) rather
    than reimplemented per backend, and each builds its manager lazily so this
    module and the backends stay numpy-free at import time.
    """

    @property
    def backend_spec(self) -> str | None:
        """The embedding model this store's vectors came out of, or ``None``.

        Asked once at registry construction so a query can be embedded into the
        same space without opening any profile's index. ``None`` when the store
        holds no vectors, or when none of them records a backend.
        """
        ...

    def has_vector_index(self, ref: str) -> bool:
        """Whether ``ref`` has vectors this store can serve.

        Cheap by contract: the ``/match`` dependency scans every profile in the
        store with it to decide whether ranking is possible at all
        (``api.deps.get_match_store``), so it must not fetch or parse a blob.
        """
        ...

    def vector_index(self, ref: str) -> "VectorIndex":
        """One profile's chunk-level index.

        Raises :class:`~researcher_profiles.embeddings.IndexNotBuiltError` when
        the profile has no vectors, and :class:`ProfileNotFoundError` when it
        does not exist.
        """
        ...

    def centroid(self, ref: str) -> "np.ndarray":
        """One profile's L2-normalized centroid vector.

        Separate from :meth:`vector_index` because a store may hold it without
        holding the chunks: a published site carries every profile's centroid
        in one ``collection/embeddings/<backend>.bin``, which is one fetch for
        the whole matrix instead of one per profile.
        """
        ...

    def centroids_matrix(self) -> "tuple[list[str], np.ndarray] | None":
        """``(slugs, matrix)`` when the store holds a precomputed centroid matrix.

        ``None`` when it does not, and the caller stacks per-profile centroids
        instead. Rows are L2-normalized and parallel to ``slugs``; the slugs are
        the store's, which need not be every slug the caller knows about (a
        profile with no vectors has no row), so the caller aligns by name.
        """
        ...

    # analytics accessors

    @property
    def centroids(self) -> "CentroidManager":
        """``store.centroids``: the centroid matrix, its cache, the query backend."""
        ...

    @property
    def match(self) -> "MatchManager":
        """``store.match``: ranking, MMR diversification, topics, clustering."""
        ...

    @property
    def indexes(self) -> "IndexFleetManager":
        """``store.indexes``: building and reporting on every profile's index.

        Narrower than the rest of :class:`VectorStore`: every method here opens
        a profile's ``.cache/`` on disk, so on a store whose profiles have no
        directory (SQL, HTTP) each one raises
        :class:`~researcher_profiles.errors.CapabilityUnavailableError` from
        ``ResearcherProfile.require_directory``. Implementing ``VectorStore`` is
        not enough; export the profiles to a directory first.
        """
        ...
