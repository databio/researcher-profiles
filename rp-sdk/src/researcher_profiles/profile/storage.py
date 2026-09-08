"""``ArtifactStorage``: where one profile's artifacts live.

Two interfaces, of different sizes:

* :class:`researcher_profiles.store.ProfileStore` is a set of profiles:
  enumerate them, resolve a reference to one, hand out its bytes.
* :class:`ArtifactStorage`, here, is one profile's backing: the eight
  artifacts, the derived digest, the raw bodies, the manifest, the transaction.

A backend implements this class. It does not subclass ``ResearcherProfile``.
The profile owns identity, caching, validation, stamping and cache
bookkeeping; a storage owns I/O, serialization and the transaction.

Four implementations ship in the SDK:

=================================  =====================================  =========
Storage                            Backing                                Extra
=================================  =====================================  =========
:class:`DirectoryArtifactStorage`  a profile directory                    core
``store.sql.SqlArtifactStorage``   the ``rp_*`` rows for one profile      ``[sql]``
``client.ApiArtifactStorage``      a live ``researcher_profiles.api``     ``[llm]``
``client.StaticArtifactStorage``   a published directory on a dumb host   ``[llm]``
=================================  =====================================  =========

It is an abstract base rather than a ``Protocol`` so it can hold the code two
backends share (the read-only refusal, the no-op transaction methods) and so an
incomplete backend fails at construction instead of at first use.
"""

import abc
import hashlib
import json
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from pydantic import ValidationError

from ..build_state import BuildState
from ..errors import CapabilityUnavailableError, ProfileLoadError, ProfileWriteError
from ..schema import (
    ArtifactRef,
    GrantRecord,
    GrantsDocument,
    PaperRecord,
    PapersDocument,
    ProfileDocument,
)
from ..schema.jsonld import canonical_dumps, read_jsonld
from .write_unit import WriteContext

if TYPE_CHECKING:  # pragma: no cover
    from . import ResearcherProfile

#: The filename suffix a paper summary body carries on disk.
SUMMARY_SUFFIX = ".summary.md"

PAPERS_URL = "sources/papers.jsonld"
GRANTS_URL = "sources/grants.jsonld"
SOUL_URL = "personality/SOUL.md"
EXPERTISE_URL = "personality/expertise.md"
CITATIONS_URL = "sources/citations.json"


class LazySummaries(Mapping[str, str]):
    """Lazy ``Mapping[str, str]`` over a fixed key set with fetch-on-access bodies.

    ``keys`` is snapshotted (sorted) at construction; ``fetch(key)`` is called
    at most once per key, on first access, and the body cached. Unknown keys
    raise :class:`KeyError` without calling ``fetch``. Shared by the
    filesystem, HTTP, and SQL backends, which differ only in ``fetch``.
    """

    def __init__(self, keys: Iterable[str], fetch: Callable[[str], str]):
        self._keys = sorted(keys)
        self._fetch = fetch
        self._cache: dict[str, str] = {}

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    def __getitem__(self, key: str) -> str:
        if key in self._cache:
            return self._cache[key]
        if key not in self._keys:
            raise KeyError(key)
        text = self._fetch(key)
        self._cache[key] = text
        return text


class NoLocalIndex:
    """Every index operation refused with one message.

    The mirror of :class:`ReadOnlyArtifactStorage` for the other half of the
    policy: the storage writers are refused there, the local-index
    capabilities here. Both say the same thing: get a local directory and
    work on that.
    """

    def __init__(self, remedy: str, *, what: str = "the embedding index"):
        self._remedy = remedy
        self._what = what

    def _refuse(self, operation: str) -> NoReturn:
        raise CapabilityUnavailableError(
            f"{operation} needs a local profile directory ({self._what}); {self._remedy}"
        )

    def build(self, *, force: bool = False, backend: Any = None) -> NoReturn:  # noqa: ARG002
        self._refuse("build_index")

    def search(self, query: str, k: int = 5, filter: dict | None = None) -> NoReturn:  # noqa: ARG002
        self._refuse("search")

    def search_similar(
        self,
        source_type: str,  # noqa: ARG002
        source_id: str,  # noqa: ARG002
        chunk_index: int = 0,  # noqa: ARG002
        k: int = 5,  # noqa: ARG002
    ) -> NoReturn:
        self._refuse("search_similar")

    def embedding(self, kind: str = "centroid") -> NoReturn:  # noqa: ARG002
        self._refuse("the profile embedding")


class ArtifactStorage(abc.ABC):
    """Where one profile's artifacts live. The whole persistence surface."""

    # Identity

    @property
    @abc.abstractmethod
    def key(self) -> str:
        """Stable identity for ``__eq__`` / ``__hash__`` / ``__repr__``.

        ``file:/abs/path`` | ``db:<url>#<rid>`` | ``api:<base>/<slug>`` |
        ``static:<base>``. Opaque: compared, never parsed.
        """

    @property
    @abc.abstractmethod
    def slug(self) -> str:
        """The profile's display handle. Not identity; see ``rid``."""

    @property
    @abc.abstractmethod
    def rid_hint(self) -> str | None:
        """The rid when the backend knows it without reading a document.

        SQL knows it (it is the row key), so a write unit can name the profile
        while its document is being replaced. Everyone else returns ``None``
        and the profile falls back to ``metadata.rid``.
        """

    @property
    @abc.abstractmethod
    def directory(self) -> Path | None:
        """The filesystem directory backing this profile, or ``None``.

        This is the one filesystem admission, mirroring ``ProfileStore.root``
        one level up: the serve-time caches under ``.cache/`` (embeddings.sqlite,
        topics.json, calibration.json, profile_vec.npz, coverage.json) are
        not covered by ``ArtifactStorage``. Nothing else may branch on it.
        """

    @abc.abstractmethod
    def locate(self, *parts: str) -> str:
        """Human-readable locator for one artifact, display only."""

    # The 8 artifacts

    @abc.abstractmethod
    def load_document(self) -> ProfileDocument:
        """Read and validate the profile document. Raises on a bad one."""

    @abc.abstractmethod
    def load_persisted_document(self) -> dict[str, Any]:
        """The serialized document currently stored; ``{}`` when absent/unreadable.

        The ``dateModified`` comparison basis. Swallows where
        :meth:`load_document` raises: a predecessor we cannot read is no
        predecessor.
        """

    @abc.abstractmethod
    def save_document(self, data: Mapping[str, Any]) -> None:
        """Persist the already-stamped, canonicalized, re-validated document."""

    @abc.abstractmethod
    def load_expertise(self) -> str: ...

    @abc.abstractmethod
    def save_expertise(self, text: str) -> None: ...

    @abc.abstractmethod
    def load_soul(self) -> str: ...

    @abc.abstractmethod
    def save_soul(self, text: str) -> None: ...

    @abc.abstractmethod
    def load_papers(self) -> list[PaperRecord]: ...

    @abc.abstractmethod
    def save_papers(self, papers: list[PaperRecord]) -> None: ...

    @abc.abstractmethod
    def load_grants(self) -> list[GrantRecord]: ...

    @abc.abstractmethod
    def save_grants(self, grants: list[GrantRecord]) -> None: ...

    @abc.abstractmethod
    def load_citations(self) -> Any: ...

    @abc.abstractmethod
    def save_citations(self, data: Any) -> None:
        """``None`` deletes it; it is not a JSON null."""

    @abc.abstractmethod
    def load_summaries(self) -> Mapping[str, str]: ...

    @abc.abstractmethod
    def save_summary(self, paper_id: str, text: str) -> None: ...

    @abc.abstractmethod
    def delete_summary(self, paper_id: str) -> None: ...

    @abc.abstractmethod
    def load_build_state(self) -> BuildState: ...

    @abc.abstractmethod
    def save_build_state(self, state: BuildState) -> None: ...

    # Derived

    @abc.abstractmethod
    def content_hash(self) -> str:
        """``sha256:<hex>`` over canonical document + NUL + soul.

        Derived, so there is no writer. The filesystem and HTTP backends
        recompute; SQL reads its column, refreshed by :meth:`refresh_derived`.
        """

    # Raw bodies

    @abc.abstractmethod
    def artifact_text(self, content_url: str) -> str | None:
        """One manifest artifact's body as text, verbatim, or ``None``.

        Verbatim matters: ``read_bytes().decode()``, never ``read_text()``.
        Three papers in the reference corpus carry lone ``\\r`` inside
        extracted PDF text, and universal-newline translation silently edits
        them, which a byte-equality check between backends catches.
        """

    @abc.abstractmethod
    def artifact_bytes(self, content_url: str) -> bytes | None: ...

    @abc.abstractmethod
    def collection_envelope(self, content_url: str) -> dict:
        """A collection node (papers/grants .jsonld) minus ``hasPart``."""

    # Manifest

    @abc.abstractmethod
    def build_manifest(self) -> tuple[list[ArtifactRef], list[ArtifactRef]]:
        """Generate ``(hasPart, subjectOf)`` from what the backend holds.

        A directory walk for files; the ``rp_artifacts`` rows for SQL.
        """

    # Transaction

    @abc.abstractmethod
    def new_write_context(self, profile: "ResearcherProfile", kind: str) -> WriteContext:
        """Open whatever transaction this backend has and describe the unit."""

    def refresh_derived(self, ctx: WriteContext) -> None:
        """Refresh store-maintained derived state, inside the write unit.

        Runs after the artifact write and before the pre-commit hooks, so a
        hook reading :meth:`content_hash` observes the new content. A no-op for
        a backend that recomputes the digest on read.
        """

    def commit(self, ctx: WriteContext) -> None:
        """Commit the unit. A no-op for a backend with no transaction."""

    def rollback(self, ctx: WriteContext) -> None:
        """Roll the unit back. A no-op for a backend with no transaction.

        Compensating writes live on
        :class:`researcher_profiles.profile.write_unit.WriteUnit` and run before this.
        """

    # Capability overrides

    def persona(self, profile: "ResearcherProfile") -> Any | None:  # noqa: ARG002
        """A backend-supplied persona manager, or ``None`` for the local one.

        ``ApiArtifactStorage`` returns an HTTP-backed one; everyone else returns
        ``None`` and the profile builds
        ``profile.persona.PersonaManager``.
        """
        return None

    def index(self, profile: "ResearcherProfile") -> Any | None:  # noqa: ARG002
        """A backend-supplied index manager, or ``None`` for the local one."""
        return None


class ReadOnlyArtifactStorage(ArtifactStorage):
    """Every writer refused with one message. Reads stay fully supported.

    A published profile served over HTTP is a read-only view. Without this
    class an HTTP profile would need a synthetic ``self.path`` as an identity
    token, and ``set_soul`` on one of them would try to ``mkdir`` at the
    filesystem root and silently write there. Here it raises instead.

    :meth:`new_write_context` raises rather than returning, so a registered
    pre-commit hook never observes a write that cannot happen.
    """

    def _writes_unsupported(self, what: str) -> NoReturn:
        raise ProfileWriteError(
            self.locate(what),
            f"{type(self).__name__} is a read-only view of a published profile; "
            "pull a local copy with researcher_profiles.client.install_profile() "
            "and edit that",
        )

    # Writers

    def save_document(self, data: Mapping[str, Any]) -> None:  # noqa: ARG002
        self._writes_unsupported("profile.jsonld")

    def save_expertise(self, text: str) -> None:  # noqa: ARG002
        self._writes_unsupported(EXPERTISE_URL)

    def save_soul(self, text: str) -> None:  # noqa: ARG002
        self._writes_unsupported(SOUL_URL)

    def save_papers(self, papers: list[PaperRecord]) -> None:  # noqa: ARG002
        self._writes_unsupported(PAPERS_URL)

    def save_grants(self, grants: list[GrantRecord]) -> None:  # noqa: ARG002
        self._writes_unsupported(GRANTS_URL)

    def save_citations(self, data: Any) -> None:  # noqa: ARG002
        self._writes_unsupported(CITATIONS_URL)

    def save_summary(self, paper_id: str, text: str) -> None:  # noqa: ARG002
        self._writes_unsupported(f"sources/summaries/{paper_id}{SUMMARY_SUFFIX}")

    def delete_summary(self, paper_id: str) -> None:
        self._writes_unsupported(f"sources/summaries/{paper_id}{SUMMARY_SUFFIX}")

    def save_build_state(self, state: BuildState) -> None:  # noqa: ARG002
        self._writes_unsupported("build_state.json")

    def new_write_context(self, profile: "ResearcherProfile", kind: str) -> WriteContext:  # noqa: ARG002
        self._writes_unsupported(kind)

    # Reads with no remote equivalent

    def load_persisted_document(self) -> dict[str, Any]:
        """No stored predecessor to compare against: nothing to stamp."""
        return {}

    def load_build_state(self) -> BuildState:
        """Build state is local-only; a published record never carries it."""
        return BuildState()


class DirectoryArtifactStorage(ArtifactStorage):
    """A profile directory. The reference backend.

    Everything that touches the directory lives here: the ``_p()`` joiner, the
    summaries directory listing, the ``BuildState`` sidecar, and the verbatim artifact-body reads that
    ``SqlProfileStore.put`` needs.
    """

    def __init__(self, path: str | os.PathLike):
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"profile dir does not exist: {p}")
        if not p.is_dir():
            raise NotADirectoryError(f"profile path is not a directory: {p}")
        self._root = p

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DirectoryArtifactStorage({str(self._root)!r})"

    # Identity

    @property
    def key(self) -> str:
        return f"file:{self._root}"

    @property
    def slug(self) -> str:
        return self._root.name

    @property
    def rid_hint(self) -> None:
        return None

    @property
    def directory(self) -> Path:
        return self._root

    def locate(self, *parts: str) -> str:
        return str(self._root.joinpath(*parts))

    def _p(self, *parts: str) -> Path:
        """Join a relative artifact name onto this profile's directory.

        Backend-internal. To name an artifact for a human, use :meth:`locate`.
        """
        return self._root.joinpath(*parts)

    # The profile document

    def load_persisted_document(self) -> dict[str, Any]:
        """The serialized document on disk; ``{}`` when absent or unreadable.

        The comparison basis for ``dateModified``, not a load path. It
        returns ``{}`` rather than raising because an unreadable predecessor
        must be treated as no predecessor: a document we cannot compare
        against is a document we cannot claim is unchanged, so the next write
        is a content change and gets stamped.

        This is why it is not built on :meth:`load_document`, and vice versa:
        the two have opposite error semantics.
        """
        from ..utils.date_modified import read_published_document

        return read_published_document(self._root)

    def load_document(self) -> ProfileDocument:
        """Read and validate ``profile.jsonld``.

        ``profile.jsonld`` is the only document shape read; a missing file
        fails with a message naming it.
        """
        path = self._p("profile.jsonld")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ProfileLoadError(path, f"could not read profile.jsonld: {e}", e) from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProfileLoadError(path, f"JSON parse error: {e}", e) from e
        try:
            return ProfileDocument.model_validate(data or {})
        except ValidationError as e:
            raise ProfileLoadError(path, f"schema error: {e}", e) from e

    def save_document(self, data: Mapping[str, Any]) -> None:
        """Persist the profile document.

        ``data`` is the already stamped and validated serialized dict:
        ``save_profile`` does the dumping, stamping, canonicalizing and
        re-validating before this is ever called, so a document that would
        fail to load never reaches the store.
        """
        path = self._p("profile.jsonld")
        try:
            path.write_text(canonical_dumps(data), encoding="utf-8")
        except OSError as e:
            raise ProfileWriteError(path, f"could not write profile.jsonld: {e}", e) from e

    def content_hash(self) -> str:
        """``"sha256:<hex>"`` over this profile's canonical content.

        Spans two artifacts, NUL-separated: the canonical ``profile.jsonld``
        bytes and the SOUL text. That is exactly the surface a patch can
        touch, which is what makes the digest a correct staleness signal, and
        why a soul-only write must refresh it too.

        Derived, so there is no writer: this backend recomputes on every call.
        A failure to read the SOUL propagates rather than being swallowed: a
        digest that silently covered only half its surface would report a
        changed profile as fresh forever.
        """
        h = hashlib.sha256()
        h.update(canonical_dumps(self.load_persisted_document()).encode("utf-8"))
        h.update(b"\x00")
        h.update((self.load_soul() or "").encode("utf-8"))
        return f"sha256:{h.hexdigest()}"

    # Persona documents

    def load_expertise(self) -> str:
        path = self._p("personality", "expertise.md")
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            raise ProfileLoadError(path, f"could not read expertise.md: {e}", e) from e

    def save_expertise(self, text: str) -> None:
        path = self._p("personality", "expertise.md")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            raise ProfileWriteError(path, f"could not write expertise.md: {e}", e) from e

    def load_soul(self) -> str:
        path = self._p("personality", "SOUL.md")
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            raise ProfileLoadError(path, f"could not read SOUL.md: {e}", e) from e

    def save_soul(self, text: str) -> None:
        path = self._p("personality", "SOUL.md")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            raise ProfileWriteError(path, f"could not write SOUL.md: {e}", e) from e

    # Collections

    def _about(self) -> str | None:
        """The ``@id`` a collection envelope points ``about`` at."""
        return self.load_document().id_

    def load_papers(self) -> list[PaperRecord]:
        """Read ``sources/papers.jsonld``, unwrapping its ``hasPart`` array."""
        path = self._p("sources", "papers.jsonld")
        if not path.is_file():
            return []
        try:
            data = read_jsonld(path)
        except OSError as e:
            raise ProfileLoadError(path, f"could not read papers.jsonld: {e}", e) from e
        except json.JSONDecodeError as e:
            raise ProfileLoadError(path, f"JSON parse error: {e}", e) from e
        try:
            return PapersDocument.model_validate(data or {}).has_part
        except (ValidationError, ValueError) as e:
            raise ProfileLoadError(path, f"schema error: {e}", e) from e

    def save_papers(self, papers: list[PaperRecord]) -> None:
        """Write ``sources/papers.jsonld`` from ``papers``."""
        path = self._p("sources", "papers.jsonld")
        doc = PapersDocument(about=self._about(), has_part=papers)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(canonical_dumps(doc.model_dump(mode="json")), encoding="utf-8")
        except OSError as e:
            raise ProfileWriteError(path, f"could not write papers.jsonld: {e}", e) from e

    def load_grants(self) -> list[GrantRecord]:
        """Read ``sources/grants.jsonld``.

        Returns an empty list when the file is absent. Any level may carry
        one, but only profiles with a configured grants source do.
        """
        path = self._p("sources", "grants.jsonld")
        if not path.is_file():
            return []
        try:
            data = read_jsonld(path)
        except OSError as e:
            raise ProfileLoadError(path, f"could not read grants.jsonld: {e}", e) from e
        except json.JSONDecodeError as e:
            raise ProfileLoadError(path, f"JSON parse error: {e}", e) from e
        try:
            return GrantsDocument.model_validate(data or {}).has_part
        except (ValidationError, ValueError) as e:
            raise ProfileLoadError(path, f"schema error: {e}", e) from e

    def save_grants(self, grants: list[GrantRecord]) -> None:
        """Write ``sources/grants.jsonld`` from ``grants``."""
        path = self._p("sources", "grants.jsonld")
        doc = GrantsDocument(about=self._about(), has_part=grants)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(canonical_dumps(doc.model_dump(mode="json")), encoding="utf-8")
        except OSError as e:
            raise ProfileWriteError(path, f"could not write grants.jsonld: {e}", e) from e

    # Citations and summaries

    def load_citations(self) -> Any:
        path = self._p("sources", "citations.json")
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ProfileLoadError(path, f"could not read citations.json: {e}", e) from e
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProfileLoadError(path, f"JSON parse error: {e}", e) from e

    def save_citations(self, data: Any) -> None:
        """Write ``sources/citations.json``; ``None`` deletes it.

        Citations legitimately absent is a state :meth:`load_citations`
        already models by returning ``None``, so ``None`` here is a delete
        rather than a JSON ``null``.
        """
        path = self._p("sources", "citations.json")
        try:
            if data is None:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as e:
            raise ProfileWriteError(path, f"could not write citations.json: {e}", e) from e

    def load_summaries(self) -> Mapping[str, str]:
        """Return a mapping of summary-id -> markdown body.

        Keys come from listing ``sources/summaries``; an absent directory is
        an empty mapping. Bodies are read on first access.
        """
        summaries_dir = self._p("sources", "summaries")
        keys: list[str] = []
        if summaries_dir.is_dir():
            keys = [
                f.name[: -len(SUMMARY_SUFFIX)]
                for f in summaries_dir.iterdir()
                if f.is_file() and f.name.endswith(SUMMARY_SUFFIX)
            ]

        def fetch(key: str) -> str:
            path = summaries_dir / f"{key}{SUMMARY_SUFFIX}"
            try:
                return path.read_text(encoding="utf-8")
            except OSError as e:
                raise ProfileLoadError(path, f"could not read summary: {e}", e) from e

        return LazySummaries(keys, fetch)

    def _summary_path(self, paper_id: str) -> Path:
        """The file a paper id names, refusing ids that would leave the
        summaries directory. This is the one place an id becomes a filename."""
        if not paper_id or paper_id in {".", ".."} or "/" in paper_id or "\\" in paper_id:
            raise ProfileWriteError(
                self._p("sources", "summaries"),
                f"paper_id {paper_id!r} cannot be used as a summary filename",
            )
        return self._p("sources", "summaries", f"{paper_id}{SUMMARY_SUFFIX}")

    def save_summary(self, paper_id: str, text: str) -> None:
        """Write ``sources/summaries/<paper_id>.summary.md``."""
        path = self._summary_path(paper_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            raise ProfileWriteError(path, f"could not write summary: {e}", e) from e

    def delete_summary(self, paper_id: str) -> None:
        """Remove ``sources/summaries/<paper_id>.summary.md``; absent is fine."""
        path = self._summary_path(paper_id)
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            raise ProfileWriteError(path, f"could not delete summary: {e}", e) from e

    # Build state

    def load_build_state(self) -> BuildState:
        """Read ``.build/<slug>/meta/build_state.json``; empty when absent.

        Absent is normal: a published profile carries no build state.
        """
        return BuildState.load(self._root)

    def save_build_state(self, state: BuildState) -> None:
        """Persist ``.build/<slug>/meta/build_state.json``."""
        try:
            state.save(self._root)
        except OSError as e:
            raise ProfileWriteError(
                self.locate(".build", "meta", "build_state.json"),
                f"could not write build state: {e}",
                e,
            ) from e

    # Raw bodies

    def artifact_text(self, content_url: str) -> str | None:
        """One artifact's body as text, verbatim.

        ``read_bytes().decode()``, not ``read_text()``: the latter opens in
        text mode with universal newlines, which rewrites every lone ``\\r``
        and every ``\\r\\n`` to ``\\n``. Three papers in the reference corpus
        carry lone ``\\r`` inside extracted PDF text, so a text-mode read
        silently edits them. A store that normalizes line endings is not
        holding the document it was given.
        """
        f = self._root / content_url
        if not f.is_file():
            return None
        try:
            return f.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def artifact_bytes(self, content_url: str) -> bytes | None:
        f = self._root / content_url
        return f.read_bytes() if f.is_file() else None

    def collection_envelope(self, content_url: str) -> dict:
        """A collection node minus ``hasPart``, so a re-render is faithful."""
        f = self._root / content_url
        if f.is_file():
            try:
                data = json.loads(f.read_bytes().decode("utf-8"))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k != "hasPart"}
        about = self._about()
        doc = (
            PapersDocument(about=about, has_part=[])
            if content_url == PAPERS_URL
            else GrantsDocument(about=about, has_part=[])
        )
        dumped = doc.model_dump(mode="json")
        return {k: v for k, v in dumped.items() if k != "hasPart"}

    # Manifest

    def build_manifest(self) -> tuple[list[ArtifactRef], list[ArtifactRef]]:
        """Generate ``(hasPart, subjectOf)`` by walking the directory."""
        from ..schema.manifest import build_manifest as _build

        return _build(self._root)

    # Transaction

    def new_write_context(self, profile: "ResearcherProfile", kind: str) -> WriteContext:
        """No session and no transaction: ``atomic=False``."""
        return WriteContext(
            profile=profile,
            slug=self.slug,
            rid=profile.rid_or_empty(),
            kind=kind,
            session=None,
            atomic=False,
        )


__all__ = [
    "DirectoryArtifactStorage",
    "NoLocalIndex",
    "LazySummaries",
    "ArtifactStorage",
    "ReadOnlyArtifactStorage",
    "SUMMARY_SUFFIX",
]
