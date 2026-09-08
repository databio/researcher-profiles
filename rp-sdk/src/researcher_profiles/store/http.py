"""``HttpProfileStore``: a published profile site, read over HTTP.

The third store backend, and the one that makes :class:`VectorStore
<researcher_profiles.store.protocol.VectorStore>` worth having: it serves a
registry's vectors without a filesystem anywhere in the path.

What it reads is exactly what ``rp publish`` writes (``publish/_site.py``,
``publish/_centroids.py``)::

    index.json                                   ["profiles/<slug>/", ...]
    by-rid.json                                  {rid: "profiles/<slug>/profile.jsonld"}
    profiles/<slug>/profile.jsonld               the canonical document
    profiles/<slug>/embeddings/index.json        one profile's flat index
    profiles/<slug>/embeddings/<backend>.bin     its chunk vectors
    profiles/<slug>/embeddings/<backend>.chunks.json
    collection/embeddings/index.json               the stacked centroid index
    collection/embeddings/<backend>.bin            every profile's centroid

Two things follow from the published form, and both are by design rather than
by omission:

* **Public subset only.** ``.cache/embeddings.sqlite`` never leaves the build
  machine, so a chunk whose source is ``restricted`` has no row in any ``.bin``
  here (spec section 6). Ranking through this store is ranking over what the
  site publishes, which is the correct answer for a consumer that only ever had
  the site.
* **Read-only.** Every writer raises
  :class:`~researcher_profiles.errors.ProfileWriteError`. A static host has no
  write endpoint, and pretending otherwise would put the failure at the end of
  a long ingest instead of at its first call.

Embedding a *query* is a compute concern, not a storage one: this store hands
back an index that knows its ``backend_spec``, and the caller supplies (or
resolves) a backend in that space. ``index.json``'s ``probe`` is what verifies
the two agree.

Requires the ``client`` extra (``httpx``), imported inside the methods that
open a connection, exactly as :mod:`researcher_profiles.client` does.
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
    one profile's directory and not an API prefix. For a live
    ``researcher_profiles.api`` server use
    :func:`researcher_profiles.client.list_registry` and
    :meth:`ResearcherProfile.from_api` instead: that server speaks a different
    contract and owns its own ranking.

    Hooks register and never fire, which is the honest outcome: they run inside
    a write, and there are no writes here.
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
        """``None``: there is no directory. See :mod:`researcher_profiles.store`."""
        return None

    # --- fetching -----------------------------------------------------------

    def _get(self, relpath: str) -> Optional[bytes]:
        """GET ``<base_url>/<relpath>``; ``None`` on 404.

        Bytes, not text: half of what this store fetches is a float32 blob, and
        a reader that decoded it as UTF-8 would corrupt it silently.
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

        The file lists directory prefixes (``"profiles/jane-doe/"``), which is
        what a static consumer needs to join to; the slug is the last segment.
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

        Slug first, like the filesystem backend: it is what humans type, and a
        directory name can never be mistaken for a rid.
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
        """Map a slug or a rid to the rid, off the published ``by-rid.json``.

        A bare ORCID resolves without any extra indexing: an ORCID rid *is* its
        ORCID, so it is already a key of that file.
        """
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

    def write_lookup_index(self) -> None:
        """``None``: there is nowhere to write, and the site already publishes one.

        ``by-rid.json`` is the published form of exactly this mapping, written
        by whoever built the site. This store reads it; it never writes back.
        """
        return None

    def get(self, ref: str) -> ResearcherProfile:
        """The profile as a lazy view over the published files.

        Built on :class:`~researcher_profiles.client.StaticArtifactStorage`, so
        each artifact costs one GET the first time it is touched and nothing
        after. Shares this store's HTTP client.
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

    def artifact_bytes(self, ref: str, content_url: str) -> bytes:
        """One artifact's bytes, addressed by its ``contentUrl``.

        The traversal guard is the same rule the filesystem backend enforces
        with ``resolve()``, stated for a URL: a ``contentUrl`` that climbs out
        of the profile prefix would let a manifest point this store at an
        unrelated part of the host.
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

        Recomputed per call from the same two artifacts every backend hashes,
        so a published profile's hash is comparable to the source directory's.
        """
        return self.get(ref).content_hash()

    # --- vectors (the ``VectorStore`` capability) ----------------------------

    def has_vector_index(self, ref: str) -> bool:
        """Whether the site publishes a centroid row for this profile.

        The collection centroid index is one fetch for the whole roster, so this
        answers for every profile at the cost of the first call. A profile with
        a centroid row always has a flat index too: ``_collect_centroid`` only
        looks at profiles that ship ``embeddings/index.json``.
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

        Three GETs, then pure numpy. The blob is verified against the declared
        length and sha256 by :meth:`FlatEmbeddingIndex.from_bytes`, which is
        the check that matters most here: these bytes crossed a network.
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
                "it was built without a searchable index, or every chunk was restricted"
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

        The stacked blob is the whole point of publishing it: one fetch answers
        for every profile, so ranking a roster of 300 costs one request rather
        than 900. The per-profile flat index is the fallback for a site
        published before the collection blob existed, or one whose profile uses a
        minority backend the blob dropped.
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
        """The embedding space this site's centroid blob lives in, or ``None``.

        What a caller resolves a query backend against, so it does not have to
        open a profile's index to learn which model the site was built with.
        """
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

    # The signatures mirror ``ProfileStore`` exactly, arguments and all, so a
    # caller that type-checks against the contract still type-checks here and
    # learns at the call that this backend cannot write. Hence the unused
    # arguments.
    def create(self, document: "ProfileDocument", *, slug: str) -> NoReturn:  # noqa: ARG002
        self._read_only("create")

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

        A read, so it is supported: the document, every artifact its manifest
        names, and the flat embedding form. What comes back is the published
        record, which is the same thing
        :meth:`FilesystemProfileStore.export_directory` produces and less than
        the build directory held (no ``.cache/``, no restricted sources).
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
