"""``HttpProfileStore``: a published profile site, read over HTTP.

Reads exactly what ``rp publish`` writes::

    index.json                                   ["profiles/<slug>/", ...]
    by-rid.json                                  {rid: "profiles/<slug>/profile.jsonld"}
    profiles/<slug>/profile.jsonld               the canonical document
    profiles/<slug>/embeddings/index.json        one profile's flat index
    profiles/<slug>/embeddings/<backend>.bin     its chunk vectors
    profiles/<slug>/embeddings/<backend>.chunks.json
    collection/embeddings/index.json               the stacked centroid index
    collection/embeddings/<backend>.bin            every profile's centroid

By design:

* **Public subset only.** A chunk whose source is ``private`` has no row in any
  ``.bin`` (spec section 6).
* **Read-only.** Every writer raises
  :class:`~researcher_profiles.errors.ProfileWriteError` at its first call.

Requires the ``client`` extra (``httpx``), imported inside the methods that
open a connection.
"""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Optional

from ..errors import ProfileWriteError
from ..profile import ResearcherProfile
from ._analytics import _AnalyticsAccessors
from .hooks import _HookedStore
from .protocol import IngestResult, ProfileNotFoundError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

    from ..embeddings.protocol import VectorIndex
    from ..schema import ProfileDocument

logger = logging.getLogger(__name__)

__all__ = ["HttpProfileStore"]


class HttpProfileStore(_HookedStore, _AnalyticsAccessors):
    """A published profile site as a :class:`VectorStore`, over HTTP.

    ``base_url`` is the site root (the directory holding ``index.json``), not
    an API prefix. For a live API server use
    :func:`researcher_profiles.client.list_registry` and
    :meth:`ResearcherProfile.from_api` instead.

    Hooks register and never fire: there are no writes here.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        client: Any = None,
    ):
        import httpx

        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._http = (
            client if client is not None else httpx.Client(timeout=timeout, follow_redirects=True)
        )
        self._owns_http = client is None
        self._slugs: list[str] | None = None
        self._rid_map: dict[str, str] | None = None
        self._profiles: dict[str, ResearcherProfile] = {}
        self._vector_indexes: dict[str, "VectorIndex"] = {}
        self._centroids: tuple[list[str], "np.ndarray"] | None = None
        self._centroid_index: dict[str, Any] | None = None
        self._init_hooks()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"HttpProfileStore(base_url={self.base_url!r})"

    def close(self) -> None:
        """Close the HTTP client when this store owns it."""
        if self._owns_http:
            self._http.close()

    # --- hooks --------------------------------------------------------------

    def _live_profiles(self):
        """Every profile handed out so far. They can carry hooks; none will fire."""
        return self._profiles.values()

    # --- identity of the store ---------------------------------------------

    @property
    def location(self) -> str:
        return self.base_url

    @property
    def root(self) -> None:
        """``None``: there is no directory."""
        return None

    # --- fetching -----------------------------------------------------------

    def _get(self, relpath: str) -> Optional[bytes]:
        """GET ``<base_url>/<relpath>``; ``None`` on 404.

        Bytes, not text: some responses are float32 blobs.
        """
        import httpx

        url = f"{self.base_url}/{relpath}"
        try:
            resp = self._http.get(url)
        except httpx.HTTPError as e:
            raise RuntimeError(f"HTTP request failed: {e}") from e
        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise PermissionError(f"{url}: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(f"{url}: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.content

    def _get_json(self, relpath: str) -> Any:
        raw = self._get(relpath)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{self.base_url}/{relpath}: JSON parse error: {e}") from e

    # --- enumeration and lookup ---------------------------------------------

    def list_slugs(self) -> list[str]:
        """Every published slug, sorted. One fetch of ``index.json``, memoized.

        The file lists directory prefixes; the slug is the last segment.
        """
        if self._slugs is not None:
            return self._slugs
        data = self._get_json("index.json")
        if not isinstance(data, list):
            raise RuntimeError(
                f"{self.base_url}/index.json is missing or is not a list of "
                "profile prefixes; this does not look like a published profile site"
            )
        slugs = sorted(str(entry).rstrip("/").rsplit("/", 1)[-1] for entry in data)
        self._slugs = slugs
        return slugs

    def resolve_slug(self, ref: str) -> str:
        """Map a slug or a rid to the slug.

        Slug first; a directory name can never be mistaken for a rid.
        """
        if ref in self.list_slugs():
            return ref
        mapped = self._rid_lookup().get(ref)
        if mapped is None:
            raise ProfileNotFoundError(
                f"nothing at {self.base_url} matches {ref!r}: no profile by that "
                f"slug, and no profile whose rid is {ref!r}"
            )
        return mapped

    def _rid_lookup(self) -> dict[str, str]:
        """``rid -> slug`` from ``by-rid.json``, memoized.

        The published file maps a rid to the *document* path
        (``profiles/<slug>/profile.jsonld``), so the slug is the second-to-last
        segment.
        """
        if self._rid_map is not None:
            return self._rid_map
        data = self._get_json("by-rid.json")
        mapping: dict[str, str] = {}
        if isinstance(data, dict):
            for rid, href in data.items():
                parts = str(href).rstrip("/").split("/")
                if len(parts) >= 2:
                    mapping[str(rid)] = parts[-2]
        self._rid_map = mapping
        return mapping

    def rid_for(self, ref: str) -> str:
        """Map a slug or a rid to the rid, off the published ``by-rid.json``."""
        slug = self.resolve_slug(ref)
        for rid, mapped in self._rid_lookup().items():
            if mapped == slug:
                return rid
        raise ProfileNotFoundError(
            f"{self.base_url} publishes no rid for profile {slug!r} in by-rid.json"
        )

    def exists(self, ref: str) -> bool:
        try:
            self.resolve_slug(ref)
        except ProfileNotFoundError:
            return False
        return True

    def rids_with_email(self, email: str) -> list[str]:  # noqa: ARG002
        raise NotImplementedError("rids_with_email is not available over HTTP")

    def successor_of(self, ref: str) -> Optional[str]:  # noqa: ARG002
        """Always ``None``: a published site carries no alias table."""
        return None

    def alias_slugs(self) -> set[str]:
        """Always empty: a published site carries no alias table."""
        return set()

    def merge_into(self, retired_ref: str, staging: Path, **kwargs) -> IngestResult:  # noqa: ARG002
        raise NotImplementedError("merge needs the SQL store")

    def write_lookup_index(self) -> None:
        """``None``: nowhere to write. The site's ``by-rid.json`` is this mapping."""
        return None

    def get(self, ref: str) -> ResearcherProfile:
        """The profile as a lazy view over the published files.

        Each artifact costs one GET on first touch. Shares this store's HTTP
        client.
        """
        slug = self.resolve_slug(ref)
        cached = self._profiles.get(slug)
        if cached is not None:
            return cached
        prof = ResearcherProfile.from_url(
            f"{self.base_url}/profiles/{slug}",
            timeout=self._timeout,
            client=self._http,
        )
        self._thread_hooks(prof)
        self._profiles[slug] = prof
        return prof

    def evict(self, ref: str) -> None:
        """Drop the cached view of ``ref`` and its vectors."""
        for key in (ref, self._rid_lookup().get(ref, ref)):
            self._profiles.pop(key, None)
            self._vector_indexes.pop(key, None)

    # --- bytes ---------------------------------------------------------------

    def document_bytes(self, ref: str) -> bytes:
        slug = self.resolve_slug(ref)
        raw = self._get(f"profiles/{slug}/profile.jsonld")
        if raw is None:
            raise ProfileNotFoundError(
                f"{self.base_url}/profiles/{slug}/profile.jsonld is not published"
            )
        return raw

    def held_artifacts(self, ref: str) -> dict[str, int | None]:
        """Every manifest entry: a static host publishes what its manifest lists.

        Not checked per body: a static site that lists a file and 404s on it
        is broken, not withholding.
        """
        return {part.content_url: part.bytes for part in self.get(ref).manifest()}

    def artifact_bytes(self, ref: str, content_url: str) -> bytes:
        """One artifact's bytes, addressed by its ``contentUrl``.

        Refuses a ``contentUrl`` that climbs out of the profile prefix, so a
        manifest cannot point this store at an unrelated part of the host.
        """
        slug = self.resolve_slug(ref)
        rel = content_url.lstrip("/")
        if ".." in Path(rel).parts or rel.startswith(("http://", "https://")):
            raise ProfileNotFoundError(f"artifact {content_url!r} is not inside profile {slug!r}")
        raw = self._get(f"profiles/{slug}/{rel}")
        if raw is None:
            raise ProfileNotFoundError(
                f"artifact {content_url!r} of profile {slug!r} is not published"
            )
        return raw

    def content_hash(self, ref: str) -> str:
        """``"sha256:<hex>"`` over the fetched document and soul.

        Recomputed per call; comparable to the source directory's hash.
        """
        return self.get(ref).content_hash()

    # --- vectors (the ``VectorStore`` capability) ----------------------------

    def has_vector_index(self, ref: str) -> bool:
        """Whether the site publishes a centroid row for this profile.

        One fetch answers for the whole roster. A profile with a centroid row
        always has a flat index too.
        """
        try:
            slug = self.resolve_slug(ref)
        except ProfileNotFoundError:
            return False
        matrix = self.centroids_matrix()
        if matrix is not None:
            return slug in matrix[0]
        return self._get(f"profiles/{slug}/embeddings/index.json") is not None

    def vector_index(self, ref: str) -> "VectorIndex":
        """The profile's flat index, built from the three fetched files.

        Three GETs. The blob is verified against the declared length and sha256
        by :meth:`FlatEmbeddingIndex.from_bytes`, since it crossed a network.
        """
        slug = self.resolve_slug(ref)
        cached = self._vector_indexes.get(slug)
        if cached is not None:
            return cached

        from ..embeddings._sqlite import IndexNotBuiltError
        from ..embeddings.flat import FlatEmbeddingIndex

        base = f"profiles/{slug}/embeddings"
        index_json = self._get(f"{base}/index.json")
        if index_json is None:
            raise IndexNotBuiltError(
                f"profile {slug!r} at {self.base_url} publishes no embeddings/index.json; "
                "it was built without a searchable index, or every chunk was private"
            )
        index = json.loads(index_json)
        blob = self._get(f"{base}/{index['file']}")
        chunks_name = Path(str(index["file"])).stem + ".chunks.json"
        chunks_json = self._get(f"{base}/{chunks_name}")
        if blob is None or chunks_json is None:
            raise IndexNotBuiltError(
                f"profile {slug!r} at {self.base_url} publishes an embeddings/index.json "
                f"naming {index['file']!r}, but the blob or its chunks file is missing"
            )
        idx = FlatEmbeddingIndex.from_bytes(index_json, blob, chunks_json)
        self._vector_indexes[slug] = idx
        return idx

    def centroid(self, ref: str) -> "np.ndarray":
        """The profile's centroid, from the stacked collection blob when there is one.

        One fetch answers for every profile. Falls back to the per-profile flat
        index when the blob is absent or dropped this profile's backend.
        """
        slug = self.resolve_slug(ref)
        matrix = self.centroids_matrix()
        if matrix is not None:
            slugs, vectors = matrix
            try:
                return vectors[slugs.index(slug)]
            except ValueError:
                pass
        return self.vector_index(slug).centroid()

    def centroids_matrix(self) -> "tuple[list[str], np.ndarray] | None":
        """``(slugs, matrix)`` from ``collection/embeddings/``, or ``None``.

        Rows are already L2-normalized on the wire (``normalized: true``) and
        ordered by the index's ``rows``, which for this blob are slugs rather
        than chunk indices (spec section 8).
        """
        if self._centroids is not None:
            return self._centroids

        import numpy as np

        index_json = self._get("collection/embeddings/index.json")
        if index_json is None:
            return None
        index = json.loads(index_json)
        blob = self._get(f"collection/embeddings/{index['file']}")
        if blob is None:
            logger.warning(
                "%s publishes collection/embeddings/index.json but not %s; "
                "falling back to per-profile centroids",
                self.base_url,
                index["file"],
            )
            return None

        dim = int(index["dim"])
        count = int(index["count"])
        expected = count * dim * 4
        if len(blob) != expected:
            raise ValueError(
                f"collection/embeddings/{index['file']}: byte length {len(blob)} != "
                f"count*dim*4 ({expected}); corrupt or truncated blob"
            )
        declared_sha = index.get("sha256")
        if declared_sha:
            import hashlib

            actual = hashlib.sha256(blob).hexdigest()
            if actual != declared_sha:
                raise ValueError(
                    f"collection/embeddings/{index['file']}: sha256 mismatch (index says "
                    f"{declared_sha}, blob is {actual}); tampered or corrupt blob"
                )
        vectors = np.frombuffer(blob, dtype=np.dtype("<f4")).reshape(count, dim)
        slugs = [str(s) for s in index["rows"]]
        self._centroid_index = index
        self._centroids = (slugs, vectors)
        return self._centroids

    @property
    def backend_spec(self) -> str | None:
        """The embedding space this site's centroid blob lives in, or ``None``."""
        if self.centroids_matrix() is None:
            return None
        assert self._centroid_index is not None
        spec = self._centroid_index.get("backend_spec")
        return str(spec) if spec else None

    # --- mutation: refused ---------------------------------------------------

    def _read_only(self, operation: str) -> NoReturn:
        raise ProfileWriteError(
            self.base_url,
            f"{operation} is not available on a remote profile site: this store "
            f"reads published files over HTTP and has nothing to write to. Install "
            f"the profiles locally (researcher_profiles.client.install_profile) and "
            f"use a FilesystemProfileStore to change them",
        )

    # Signatures mirror ``ProfileStore`` exactly so callers still type-check;
    # hence the unused arguments.
    def create(self, document: "ProfileDocument", *, slug: str) -> NoReturn:  # noqa: ARG002
        self._read_only("create")

    def create_bundle(
        self,
        document: "ProfileDocument",  # noqa: ARG002
        *,
        slug: str,  # noqa: ARG002
        expertise: "str | None" = None,  # noqa: ARG002
        soul: "str | None" = None,  # noqa: ARG002
        artifacts: "dict[str, str] | None" = None,  # noqa: ARG002
    ) -> NoReturn:
        self._read_only("create_bundle")

    def put_document(self, slug: str, document: "ProfileDocument") -> NoReturn:  # noqa: ARG002
        self._read_only("put_document")

    def delete(self, ref: str) -> NoReturn:  # noqa: ARG002
        self._read_only("delete")

    def commit_directory(
        self,
        slug: str,  # noqa: ARG002
        staging: Path,  # noqa: ARG002
        *,
        build_missing_index: bool = False,  # noqa: ARG002
    ) -> IngestResult:
        self._read_only("commit_directory")

    def export_directory(self, ref: str, dest: Path) -> Path:
        """Download a published profile into ``dest``. Returns ``dest``.

        The document, every artifact its manifest names, and the flat embedding
        form: the published record (no ``.cache/``, no private sources).
        """
        slug = self.resolve_slug(ref)
        out = Path(dest).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        (out / "profile.jsonld").write_bytes(self.document_bytes(slug))

        prof = self.get(slug)
        doc = prof.metadata
        for part in list(doc.has_part) + list(doc.subject_of):
            content_url = getattr(part, "content_url", None)
            if not content_url:
                continue
            try:
                raw = self.artifact_bytes(slug, content_url)
            except ProfileNotFoundError:
                logger.debug("artifact %s of %s is not published", content_url, slug)
                continue
            target = out / content_url
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)

        index_json = self._get(f"profiles/{slug}/embeddings/index.json")
        if index_json is not None:
            index = json.loads(index_json)
            emb = out / "embeddings"
            emb.mkdir(parents=True, exist_ok=True)
            (emb / "index.json").write_bytes(index_json)
            chunks_name = Path(str(index["file"])).stem + ".chunks.json"
            for name in (str(index["file"]), chunks_name):
                raw = self._get(f"profiles/{slug}/embeddings/{name}")
                if raw is not None:
                    (emb / name).write_bytes(raw)
        return out
