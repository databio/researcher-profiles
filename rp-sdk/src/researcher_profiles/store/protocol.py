"""The store contract (:class:`ProfileStore`, :class:`VectorStore`) and the
value and error types that cross it."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

from ..errors import ProfileError, ProfileWriteError
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
    """No profile in the store answers to a given ref (a slug or a ``profile_id``).

    Both a ``ProfileError`` and a ``KeyError``, so callers can catch it either
    way.
    """

    def __str__(self) -> str:
        # ``KeyError.__str__`` reprs its argument, so a message would arrive
        # wrapped in quotes. These carry a sentence, not a key.
        return str(self.args[0]) if self.args else ""


class RetiredRidError(ProfileWriteError):
    """A write targets a rid a merge retired, or a slug a rename or merge retired.

    A retired rid or slug keeps resolving to its successor through an alias,
    so reusing it would silently hijack (or resurrect) that identity. A store
    that keeps aliases refuses such a write with this error. It is a
    ``ProfileWriteError`` so routes map it to 409. ``successor_rid`` is who
    the record the alias now leads to is about.
    """

    def __init__(
        self,
        ref: str,
        successor_rid: str,
        *,
        rid: Optional[str] = None,
        slug: Optional[str] = None,
        message: Optional[str] = None,
    ):
        self.rid = rid
        self.slug = slug
        self.successor_rid = successor_rid
        self.message = message or (
            f"{ref!r} was retired by a merge into {successor_rid!r}; "
            "a retired rid or slug cannot be reused"
        )
        super().__init__(ref, self.message)

    def __str__(self) -> str:
        return self.message


class UploadError(ValueError):
    """A staged profile directory is malformed, unsafe, or does not load.

    Defined here, not in ``api.upload``, so backends need not import the HTTP
    layer to raise it.
    """


class DuplicateIdentityError(ValueError):
    """Two profiles in one directory store claim the same ``rid``.

    Only the filesystem backend raises it: a directory corpus holds one record
    per person. SQL allows several records about one person (one per owner),
    and a site's ``by-rid.json`` cannot hold two entries under one key.
    """


@dataclass(frozen=True)
class Ownership:
    """Who owns a profile row. The host defines what a principal is.

    The SDK checks one rule only: a self profile (``is_self``) is a person's
    own, so its owner must be a ``'user'``.
    """

    #: ``'user'`` or ``'team'``, in the host's vocabulary.
    kind: str
    #: The owning principal's id, in the host's principal table.
    principal_id: int
    #: Whether the profile is about its owner.
    is_self: bool = False

    def __post_init__(self) -> None:
        if self.is_self and self.kind != "user":
            raise ValueError(f"a self profile must be owned by a user, not a {self.kind!r}")


@dataclass(frozen=True)
class ProfileRef:
    """One record about a person, as :meth:`ProfileStore.profiles_about` lists it."""

    #: The record key, or ``None`` on a backend that has none (a directory).
    profile_id: int | None
    #: Who the record is about.
    rid: str
    #: Its display handle; ``store.get(slug)`` loads it.
    slug: str
    #: Who owns it, or ``None`` on a backend with no owners.
    owner: Ownership | None = None

    @property
    def ref(self) -> "int | str":
        """What to pass to ``store.get``: the ``profile_id`` when there is one."""
        return self.profile_id if self.profile_id is not None else self.slug


@dataclass
class IngestResult:
    """Outcome of committing one staged profile directory into a store."""

    slug: str
    rid: str | None
    name: str
    level: str
    #: Whether a built embedding index is present afterwards. Always ``False``
    #: on a store with no filesystem.
    indexed: bool
    #: Live files carried over rather than deleted, by class
    #: (``{"fulltext": 53, "index": 1}``); see ``upload.WITHHELD_CLASSES``.
    #: Filled in by ``ingest_archive``, not by ``commit_directory``.
    kept: dict[str, int] = field(default_factory=dict)
    #: Manifest entries the server added back for kept files the incoming
    #: manifest did not list. Filled in by ``ingest_archive``; see
    #: ``upload._splice_kept_into_manifest``.
    spliced: int = 0
    #: ``{role: count}`` over the manifest held after the commit. Filled in by
    #: ``ingest_archive``.
    manifest_counts: dict[str, int] = field(default_factory=dict)
    #: Which ``upload.PushMode`` the ingest ran under, and so what happened to
    #: the live files the archive did not carry.
    mode: str = "replace"
    #: The record written, on a backend that has record keys (SQL).
    profile_id: int | None = None


@runtime_checkable
class ProfileStore(Protocol):
    """A set of profiles, whatever they are stored in.

    Every method that takes a ``ref`` takes a **slug** (any backend, a retired
    slug included where the backend keeps aliases) or an integer
    **``profile_id``** (``#<id>`` as a string) on a backend that has one. A rid
    is never a ref: a store may hold several records about one person, so "the
    profile for this rid" is a question with an explicit answer
    (:meth:`profiles_about`, :meth:`self_profile_for`, :meth:`profile_owned_by`).
    """

    # identity of the store itself

    @property
    def location(self) -> str:
        """A human-readable locator for this store. Display only.

        A directory path, a database URL. Never parse it or join to it.
        """
        ...

    @property
    def root(self) -> Optional[Path]:
        """The filesystem root when this store is a directory, else ``None``.

        The one filesystem admission. This is the whole sanctioned set of
        readers that branch on ``root``, and nothing else may:

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
          (``api.service.Service.invalidate``). They sit outside ``ArtifactStorage``, so
          nothing else can drop them; a ``None`` root means there are none.

        ``tests/test_guardrails.py`` holds the allowlist to match, so a new
        reader fails the suite until it is added here too.
        """
        ...

    # enumeration and lookup

    def list_slugs(self) -> list[str]:
        """Every profile's display handle, sorted."""
        ...

    def resolve_slug(self, ref: "str | int") -> str:
        """Map a ref to the live slug. Raises :class:`ProfileNotFoundError`."""
        ...

    def profiles_about(self, rid: str) -> list[ProfileRef]:
        """Every record about the person ``rid``, the self profile first.

        A retired rid resolves through :meth:`successor_rid` first. A directory
        store returns zero or one entry (it refuses two directories with one
        rid, :class:`DuplicateIdentityError`); a published site returns what its
        listing says.
        """
        ...

    def self_profile_for(self, rid: str) -> Optional[ResearcherProfile]:
        """The person's own profile (``is_self``), or ``None``.

        A directory corpus has no owners: its one profile for ``rid`` IS the
        person's record, so the filesystem and HTTP stores return that.
        """
        ...

    def profile_owned_by(self, rid: str, owner: Ownership) -> Optional[ResearcherProfile]:
        """The record about ``rid`` that ``owner`` owns, or ``None``.

        SQL only; the other backends have no owners and raise
        ``NotImplementedError``.
        """
        ...

    def write_lookup_index(self) -> Optional[Path]:
        """Persist ``rid <-> slug`` so shell callers resolve without importing.

        Nothing writes it implicitly. Returns ``None`` on a store with no
        directory; such a store answers through :meth:`profiles_about` instead.
        """
        ...

    def exists(self, ref: "str | int") -> bool:
        """Whether ``ref`` names a profile in this store."""
        ...

    def get(self, ref: "str | int") -> ResearcherProfile:
        """Load a profile. Raises :class:`ProfileNotFoundError`.

        The returned profile carries every hook registered through
        :meth:`add_pre_commit_hook`, so any write through it fires them.
        """
        ...

    def rids_with_email(self, email: str) -> list[str]:
        """rids of every profile whose top-level document ``email`` equals ``email``.

        Ignoring case and outer whitespace, sorted. Reads stored documents only:
        never a host's overlay or lens of someone else's profile. A backend that
        cannot enumerate documents (HTTP) raises ``NotImplementedError``.
        """
        ...

    def successor_rid(self, rid: str) -> Optional[str]:
        """The live rid a retired rid now resolves to, or ``None``.

        ``None`` for a rid nothing retired, and on every backend that cannot
        merge. :meth:`profiles_about` already follows it; this says whether it
        had to.
        """
        ...

    def alias_slugs(self) -> set[str]:
        """Slugs of retired profiles. They stay reserved: allocate around them."""
        ...

    # mutation

    def merge_into(
        self,
        retired_ref: "str | int",
        staging: Path,
        *,
        survivor_ref: "str | int | None",
        survivor_rid: str,
        survivor_slug: str,
        build_missing_index: bool = True,
    ) -> IngestResult:
        """Retire one record into another in ONE write unit.

        ``survivor_ref`` names the live record that receives the staged
        content; the retired record is deleted. ``None`` is a **convert**: the
        retired record itself survives, under ``survivor_rid``. Either way the
        staged directory's rid must be ``survivor_rid``. When the rid changes,
        every other record about the retired rid moves to ``survivor_rid`` and
        an alias keeps the retired rid resolving (:meth:`successor_rid`); a
        retired slug keeps resolving to the survivor. Pre-commit hooks see
        ``ctx.kind == "merge"``, ``ctx.profile_id`` (the survivor),
        ``ctx.retired_profile_id`` (``None`` for a convert), ``ctx.retired_rid``
        (``None`` when the rid did not change) and ``ctx.retired_slug``. Only
        SQL implements it; the others raise ``NotImplementedError``. Raises
        :class:`RetiredRidError` when ``survivor_rid`` is itself retired.
        """
        ...

    def create(
        self, document: "ProfileDocument", *, slug: str, owner: Optional[Ownership] = None
    ) -> ResearcherProfile:
        """Create a new profile from a validated document. Returns it.

        Runs in one write unit of kind ``"create"``. ``owner`` lands on the row
        in the same transaction; when it is ``None`` a SQL store asks its
        ``owner_resolver``. Backends with no owners ignore it.

        Raises ``ProfileWriteError`` if ``slug`` is taken (and, on a directory
        store or for a SQL record with no owner, if the rid already has a
        profile), and :class:`RetiredRidError` for a retired rid or slug.
        """
        ...

    def create_bundle(
        self,
        document: "ProfileDocument",
        *,
        slug: str,
        expertise: Optional[str] = None,
        soul: Optional[str] = None,
        artifacts: Optional[dict[str, str]] = None,
        owner: Optional[Ownership] = None,
    ) -> ResearcherProfile:
        """Create a profile and all of its authored artifacts in one write unit.

        The document, persona documents, every artifact, the owner and the
        derived digest land together, so no half-built profile is ever readable.

        ``artifacts`` maps a manifest ``contentUrl`` to its text body; each key
        must appear in the document's ``hasPart`` or ``subjectOf``.

        Whether the unit is genuinely atomic is the backend's to say
        (``WriteContext.atomic``): SQL commits or rolls back, the filesystem
        cleans up after itself instead.

        Raises as :meth:`create` does.
        """
        ...

    def put_document(
        self, slug: str, document: "ProfileDocument", *, owner: Optional[Ownership] = None
    ) -> "ResearcherProfile":
        """Create-or-replace a profile's canonical profile.jsonld.

        Runs in one write unit (kind ``"create"`` if new, ``"edit"`` if it
        exists), so hooks fire and ``dateModified`` is stamped. Derived
        artifacts (``sources/``, ``.cache/``) are left untouched: this writes
        the document, not the bundle. ``owner`` applies to a create only; a
        replace keeps the row's owner and refuses a document with another rid.
        Where slugs have aliases (SQL), a former slug replaces the record it
        leads to when that record is about the document's rid.

        Returns the profile. Raises :class:`RetiredRidError` for a retired rid
        or slug.
        """
        ...

    def delete(self, ref: "str | int") -> str:
        """Remove a profile and everything belonging to it. Returns its rid."""
        ...

    def commit_directory(
        self,
        slug: str,
        staging: Path,
        *,
        owner: Optional[Ownership] = None,
        build_missing_index: bool = False,
    ) -> IngestResult:
        """Make a fully-staged profile directory live in this store.

        ``staging`` is a complete profile directory (``profile.jsonld`` at its
        root): the interchange format, not the storage format. "Live" is an
        atomic rename for the filesystem, a transaction for SQL. A replace (the
        slug is live) keeps the record, its ``profile_id`` and its owner, and
        refuses a staged rid that differs; ``owner`` applies to a create only.
        Where slugs have aliases (SQL), a former slug is a replace of the
        record it leads to when that record is about the staged rid; the
        record keeps its live slug, which the result names.

        ``build_missing_index`` asks for a best-effort embedding-index build
        afterwards; a backend with no filesystem ignores it.

        Raises :class:`UploadError` if the staged directory does not load, and
        :class:`RetiredRidError` for a retired rid or slug.
        """
        ...

    def export_directory(self, ref: str, dest: Path) -> Path:
        """Materialize a profile as a directory at ``dest``. Returns ``dest``.

        The inverse of :meth:`commit_directory`.
        """
        ...

    # bytes

    def document_bytes(self, ref: str) -> bytes:
        """The canonical ``profile.jsonld`` bytes, exactly as persisted.

        Served byte for byte, so the ``conformsTo`` claim holds for the file a
        crawler retrieves.
        """
        ...

    def held_artifacts(self, ref: str) -> dict[str, int | None]:
        """``{contentUrl: stored size in bytes}`` for every body this store holds.

        A manifest row can outlive its body (a push withholds full text by
        default), so "listed" is not "fetchable". Reads no body. A size is
        ``None`` when the store cannot say cheaply. Raises
        :class:`ProfileNotFoundError` when the profile is absent.
        """
        ...

    def artifact_bytes(self, ref: str, content_url: str) -> bytes:
        """One manifest artifact's bytes, addressed by its ``contentUrl``.

        Raises :class:`ProfileNotFoundError` when the profile or the artifact is
        absent. Applies no privacy policy; the caller must.
        """
        ...

    def content_hash(self, ref: str) -> str:
        """``"sha256:<hex>"`` over the canonical document and the SOUL text.

        Refreshed inside the write unit before the pre-commit hooks run, so a
        hook sees post-write content. Identical across backends; see
        :func:`researcher_profiles.store.db.content_hash_for`.
        """
        ...

    # hooks

    def add_pre_commit_hook(self, hook: WriteHook) -> None:
        """Register a callable to run inside every write, before commit.

        Registered on the store, not an HTTP app, so writes that bypass the
        routes still fire it. Applies to profiles already handed out too, so
        order relative to a first :meth:`get` does not matter.
        """
        ...

    def add_post_commit_hook(self, hook: WriteHook) -> None:
        """Register a callable to run after a write commits successfully.

        Same reach as :meth:`add_pre_commit_hook`, but it cannot abort the
        write, which already landed. For fire-and-forget notifications.
        """
        ...

    def evict(self, ref: str) -> None:
        """Drop any cached view of ``ref`` so the next :meth:`get` reloads.

        A no-op on a store that does not cache. Cache invalidation only;
        dependent state belongs in a pre-commit hook.
        """
        ...


@runtime_checkable
class VectorStore(ProfileStore, Protocol):
    """A store that can also hand out a profile's vectors.

    A capability, not part of :class:`ProfileStore`, for two reasons:

    1. Not every backend has vectors to give; such a backend should fail a
       capability check rather than raise on half its contract.
    2. The base contract must stay numpy-free (a guardrail asserts it), which
       is also why the array types are under ``TYPE_CHECKING``.

    Check for it with
    :func:`researcher_profiles.store._analytics.require_vector_store`, a
    ``hasattr`` sweep over the five vector methods, not ``isinstance``: an
    ``isinstance`` would evaluate the analytics accessors below.

    The accessors come from
    :class:`researcher_profiles.store._analytics._AnalyticsAccessors` and build
    their managers lazily to keep imports numpy-free.
    """

    @property
    def backend_spec(self) -> str | None:
        """The embedding model this store's vectors came out of, or ``None``.

        ``None`` when the store holds no vectors, or none of them records a
        backend.
        """
        ...

    def has_vector_index(self, ref: str) -> bool:
        """Whether ``ref`` has vectors this store can serve.

        Cheap by contract (it is called for every profile in the store): it
        must not fetch or parse a blob.
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
        the chunks (a published site's ``collection/embeddings/<backend>.bin``).
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

        Every method opens a profile's ``.cache/`` on disk, so on SQL or HTTP
        each raises :class:`~researcher_profiles.errors.CapabilityUnavailableError`.
        """
        ...
