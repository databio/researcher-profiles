"""The two read-only HTTP storage backends and their remote capability managers.

:class:`ApiArtifactStorage`
    One profile served by a live ``researcher_profiles.api`` server.
    ``ResearcherProfile.from_api(...)`` builds one. The server owns the model,
    the corpus and the embedding index, so ``prof.persona`` and ``prof.index``
    are HTTP-backed here, through :class:`_RemotePersona` and
    :class:`_RemoteIndex`.

:class:`StaticArtifactStorage`
    One profile published as a directory on a dumb static host (Cloudflare
    Pages, S3, Apache). ``ResearcherProfile.from_url(...)`` builds one. One
    lazy GET per artifact, and the manifest in ``profile.jsonld`` stands in for
    the directory listing a static host does not have.

Both are read-only: every writer is refused by
:class:`~researcher_profiles.profile.storage.ReadOnlyArtifactStorage` with one
message naming :func:`install_profile`.
"""

import hashlib
import json
from typing import Any, Mapping, NoReturn, Optional
from urllib.parse import urlparse

from pydantic import ValidationError

from ..embeddings.cache import SearchHit
from ..errors import CapabilityUnavailableError, ProfileError, ProfileLoadError
from ..models.results import CitationRef, Idea, PersonaResponse, Riff
from ..profile import ResearcherProfile
from ..profile.storage import (
    CITATIONS_URL,
    EXPERTISE_URL,
    GRANTS_URL,
    PAPERS_URL,
    SOUL_URL,
    SUMMARY_SUFFIX,
    LazySummaries,
    ReadOnlyArtifactStorage,
)
from ..schema import (
    ArtifactRef,
    GrantRecord,
    GrantsDocument,
    PaperRecord,
    PapersDocument,
    ProfileDocument,
)
from ..schema.jsonld import canonical_dumps
from ._http import _compact, auth_headers

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary_id_of(content_url: str) -> Optional[str]:
    """The summary id a ``sources/summaries/<id>.summary.md`` address names."""
    if content_url.endswith(SUMMARY_SUFFIX):
        return content_url.rsplit("/", 1)[-1][: -len(SUMMARY_SUFFIX)]
    return None


def _empty_collection_envelope(content_url: str, about: Optional[str]) -> dict:
    """A works/grants collection node with no ``hasPart``, from nothing."""
    doc = (
        PapersDocument(about=about, has_part=[])
        if content_url == PAPERS_URL
        else GrantsDocument(about=about, has_part=[])
    )
    return {k: v for k, v in doc.model_dump(mode="json").items() if k != "hasPart"}


# ---------------------------------------------------------------------------
# Capability managers for a profile served over HTTP
# ---------------------------------------------------------------------------


class _RemoteIndex:
    """``prof.index`` for a profile served by a live API server.

    ``search`` goes over ``POST /search``, which the server answers from the
    index IT built. The rest need the sqlite handle itself and are refused.
    """

    def __init__(self, storage: "ApiArtifactStorage"):
        self._storage = storage

    def search(self, query: str, k: int = 5, filter: Optional[dict] = None) -> list[SearchHit]:
        body: dict[str, Any] = {"query": query, "k": k}
        if filter is not None:
            body["filter"] = filter
        data = self._storage._request("POST", self._storage._api_path("search"), json=body)
        return [
            SearchHit(
                text=h.get("text", ""),
                source_type=h.get("source_type", ""),
                source_id=h.get("source_id", ""),
                chunk_index=int(h.get("chunk_index", 0)),
                section=h.get("section"),
                cosine=float(h.get("cosine", 0.0)),
                score=float(h.get("score", 0.0)),
                meta=h.get("meta", {}) or {},
            )
            for h in data.get("hits", [])
        ]

    def _not_served(self, operation: str) -> NoReturn:
        raise CapabilityUnavailableError(
            f"{operation} is not exposed over the API in v1; the server at "
            f"{self._storage.base_url} owns the index. Install a local copy "
            "(client.install_profile) if you need it locally"
        )

    def search_similar(
        self,
        source_type: str,  # noqa: ARG002
        source_id: str,  # noqa: ARG002
        chunk_index: int = 0,  # noqa: ARG002
        k: int = 5,  # noqa: ARG002
    ) -> NoReturn:  # pragma: no cover - not exposed in v1
        self._not_served("search_similar")

    def build(
        self,
        *,
        force: bool = False,  # noqa: ARG002
        backend: Any = None,  # noqa: ARG002
    ) -> NoReturn:  # pragma: no cover  # noqa: ARG002
        self._not_served("build_index")

    def embedding(self, kind: str = "centroid") -> NoReturn:  # pragma: no cover  # noqa: ARG002
        self._not_served("the profile embedding")


class _RemotePersona:
    """``prof.persona`` for a profile served by a live API server.

    Each verb POSTs one request; the server owns the model, the corpus and the
    refusal policy. Signatures are a strict subset of the local ones:
    ``thinking`` and ``max_tokens`` have no field on ``AskRequest`` /
    ``ReviewRequest`` / ``InnovateRequest`` / ``RiffRequest``, and forwarding
    them would only move the lie one hop, since ``_APIModel`` sets
    ``extra="allow"``.
    """

    def __init__(self, profile: ResearcherProfile, storage: "ApiArtifactStorage"):
        self._profile = profile
        self._storage = storage

    def _post(self, verb: str, body: dict[str, Any]) -> Any:
        """POST one persona request and return the decoded JSON body."""
        return self._storage._request("POST", self._storage._api_path(verb), json=body)

    @staticmethod
    def _parse_citations(raw_cits: Any) -> list[CitationRef]:
        out: list[CitationRef] = []
        for c in raw_cits or []:
            if isinstance(c, dict):
                out.append(
                    CitationRef(
                        paper_id=c.get("paper_id", ""),
                        relevance=c.get("relevance"),
                        span=c.get("span"),
                    )
                )
            elif isinstance(c, str):
                out.append(CitationRef(paper_id=c))
            else:
                out.append(
                    CitationRef(
                        paper_id=getattr(c, "paper_id", ""),
                        relevance=getattr(c, "relevance", None),
                        span=getattr(c, "span", None),
                    )
                )
        return out

    def _text_response(self, data: Any) -> PersonaResponse:
        """Build the shared nine-field text response from a wire payload."""
        return PersonaResponse(
            text=data.get("text", ""),
            citations=self._parse_citations(data.get("citations")),
            model=data.get("model", ""),
            usage=dict(data.get("usage") or {}),
            raw=None,
            request_id=data.get("request_id"),
            refused=bool(data.get("refused", False)),
            refusal_reason=data.get("refusal_reason"),
            grounded=bool(data.get("grounded", True)),
        )

    def ask(
        self,
        question: str,
        k: int = 5,
        *,
        model: Optional[str] = None,
        strict_corpus: bool = False,
        refusal_threshold: Optional[float] = None,
        history: Optional[list] = None,
    ) -> PersonaResponse:
        body = _compact(
            question=question,
            k=k,
            model=model,
            strict_corpus=strict_corpus,
            refusal_threshold=refusal_threshold,
            history=history,
        )
        return self._text_response(self._post("ask", body))

    def review(
        self,
        material: str,
        focus: Optional[str] = None,
        k: int = 5,
        *,
        model: Optional[str] = None,
        strict_corpus: bool = False,
        refusal_threshold: Optional[float] = None,
    ) -> PersonaResponse:
        body = _compact(
            material=material,
            focus=focus,
            k=k,
            model=model,
            strict_corpus=strict_corpus,
            refusal_threshold=refusal_threshold,
        )
        return self._text_response(self._post("review", body))

    def innovate(
        self,
        topic: str,
        n: int = 3,
        *,
        k: int = 12,
        model: Optional[str] = None,
        temperature: float = 0.7,
    ) -> list[Idea]:
        body = _compact(topic=topic, n=n, k=k, temperature=temperature, model=model)
        data = self._post("innovate", body)
        return [
            Idea(
                hypothesis=i.get("hypothesis", ""),
                approach=i.get("approach", ""),
                rationale=i.get("rationale", ""),
                related_works=list(i.get("related_works") or []),
            )
            for i in (data.get("items") or [])
        ]

    def riff(
        self,
        seed: str,
        n: int = 5,
        *,
        k: int = 4,
        model: Optional[str] = None,
        temperature: float = 1.0,
    ) -> list[Riff]:
        body = _compact(seed=seed, n=n, k=k, temperature=temperature, model=model)
        data = self._post("riff", body)
        return [
            Riff(
                angle=r.get("angle", ""),
                text=r.get("text", ""),
                related_work=r.get("related_work"),
            )
            for r in (data.get("items") or [])
        ]

    def chat(self, **kwargs: Any):
        from ..generative.chat import Chat

        return Chat(self._profile, **kwargs)


# ---------------------------------------------------------------------------
# ApiArtifactStorage
# ---------------------------------------------------------------------------


class ApiArtifactStorage(ReadOnlyArtifactStorage):
    """A profile served by a live ``researcher_profiles.api`` server.

    Read-only: every writer is refused by :class:`ReadOnlyArtifactStorage`.
    Reads come off the combined detail payload where the API has one, and off
    the dedicated routes otherwise.
    """

    def __init__(
        self,
        slug: str,
        base_url: str,
        token: Optional[str] = None,
        *,
        timeout: float = 60.0,
        client: Any = None,
    ):
        import httpx

        self._slug = slug
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._http = (
            client
            if client is not None
            else httpx.Client(
                base_url=self.base_url,
                headers=auth_headers(token),
                timeout=timeout,
            )
        )
        self._owns_http = client is None
        # Caches for the combined endpoint.
        self._detail_cache: Optional[dict] = None
        self._paper_index_cache: Optional[list[dict]] = None

    def __repr__(self) -> str:
        return f"ApiArtifactStorage(slug={self._slug!r}, base_url={self.base_url!r})"

    # Identity

    @property
    def key(self) -> str:
        return f"api:{self.base_url}/{self._slug}"

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def rid_hint(self) -> None:
        return None

    @property
    def directory(self) -> None:
        return None

    def locate(self, *parts: str) -> str:
        """Display-only locator: the API URL naming this artifact."""
        tail = "/".join(parts)
        base = f"{self.base_url}/api/v1/profiles/{self._slug}"
        return f"{base}/{tail}" if tail else base

    def _api_path(self, *parts: str) -> str:
        return "/".join(("/api/v1/profiles", self._slug, *parts))

    # Lifecycle

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def refresh(self) -> None:
        """Drop cached immutable resources so the next access refetches."""
        self._detail_cache = None
        self._paper_index_cache = None

    # HTTP helpers + error mapping

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        import httpx

        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise RuntimeError(f"HTTP request failed: {e}") from e
        if resp.status_code == 401:
            raise PermissionError(f"unauthorized: {resp.text}")
        if resp.status_code == 404:
            raise KeyError(self._slug)
        if resp.status_code >= 500:
            raise RuntimeError(f"server error {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise RuntimeError(f"client error {resp.status_code}: {resp.text[:300]}")
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    def _detail(self) -> dict:
        if self._detail_cache is None:
            self._detail_cache = self._request("GET", self._api_path())
        return self._detail_cache

    def _fetch_summary(self, paper_id: str) -> str:
        data = self._request("GET", self._api_path("summary", paper_id))
        return data.get("summary", "")

    # Reads

    def load_document(self) -> ProfileDocument:
        d = self._detail()
        # Server emits a tolerant dict; round-trip through ProfileDocument.
        return ProfileDocument.model_validate(d["metadata"])

    def content_hash(self) -> str:
        """``"sha256:<hex>"`` over the served document + soul.

        A read, so it stays supported on a read-only view. Computed from the
        combined detail payload rather than raw bytes, because the API serves a
        document, not a file.
        """
        d = self._detail()
        h = hashlib.sha256()
        h.update(
            canonical_dumps(
                ProfileDocument.model_validate(d["metadata"]).model_dump(mode="json")
            ).encode("utf-8")
        )
        h.update(b"\x00")
        h.update((d.get("soul") or "").encode("utf-8"))
        return f"sha256:{h.hexdigest()}"

    def load_expertise(self) -> str:
        return self._detail().get("expertise", "")

    def load_soul(self) -> str:
        return self._detail().get("soul", "")

    def load_papers(self) -> list[PaperRecord]:
        entries = self._request("GET", self._api_path("papers"))
        self._paper_index_cache = list(entries)
        return [
            PaperRecord.model_validate(
                {
                    "paper_id": e.get("paper_id"),
                    "title": e.get("title", ""),
                    "year": e.get("year"),
                    "journal": e.get("journal"),
                    "first_author": e.get("first_author"),
                    "authors": e.get("authors"),
                    "doi": e.get("doi"),
                    "pmid": e.get("pmid"),
                    "openalex_id": e.get("openalex_id"),
                    "full_text_link": e.get("full_text_link"),
                }
            )
            for e in entries
        ]

    def load_grants(self) -> list[GrantRecord]:
        # Not exposed in v1: the API serves works, not awards.
        return []

    def load_citations(self) -> Any:
        # Not exposed in v1.
        return None

    def load_summaries(self) -> Mapping[str, str]:
        if self._paper_index_cache is None:
            # Triggers a load + caches the paper index.
            self.load_papers()
        ids = [
            e["paper_id"]
            for e in (self._paper_index_cache or [])
            if e.get("paper_id") and e.get("summary_available")
        ]
        return LazySummaries(ids, self._fetch_summary)

    # Raw bodies

    def artifact_text(self, content_url: str) -> Optional[str]:
        """Only what v1 serves: soul, expertise, and paper summaries.

        Returns ``None`` otherwise. An invented empty body is worse than an
        empty column, and the v1 API has no route that hands back an arbitrary
        artifact's bytes.
        """
        if content_url == SOUL_URL:
            return self.load_soul() or None
        if content_url == EXPERTISE_URL:
            return self.load_expertise() or None
        summary_id = _summary_id_of(content_url)
        if summary_id is not None:
            summaries = self.load_summaries()
            if summary_id in summaries:
                return summaries[summary_id]
        return None

    def artifact_bytes(self, content_url: str) -> Optional[bytes]:
        text = self.artifact_text(content_url)
        return None if text is None else text.encode("utf-8")

    def collection_envelope(self, content_url: str) -> dict:
        return _empty_collection_envelope(content_url, self._about())

    def _about(self) -> Optional[str]:
        try:
            return self.load_document().id_
        except (ProfileError, KeyError, RuntimeError):  # pragma: no cover - unreachable server
            return None

    def build_manifest(self) -> tuple[list[ArtifactRef], list[ArtifactRef]]:
        """The manifest the served document records; there is nothing to walk."""
        try:
            doc = self.load_document()
        except (ProfileError, KeyError, RuntimeError):  # pragma: no cover
            return [], []
        return list(doc.has_part), list(doc.subject_of)

    # Capabilities

    def persona(self, profile: ResearcherProfile) -> _RemotePersona:
        """HTTP-backed persona: the server owns model, corpus and refusals."""
        return _RemotePersona(profile, self)

    def index(self, profile: ResearcherProfile) -> _RemoteIndex:  # noqa: ARG002
        """``search`` over HTTP; everything else needs the local sqlite handle."""
        return _RemoteIndex(self)


# ---------------------------------------------------------------------------
# StaticArtifactStorage
# ---------------------------------------------------------------------------


def _s3_to_https(url: str) -> str:
    """Translate ``s3://bucket/prefix`` to the public virtual-hosted HTTPS
    endpoint (``https://bucket.s3.amazonaws.com/prefix``).

    Published profiles are public static directories (deployed with
    ``aws s3 sync``), so plain HTTPS reads suffice. A private bucket needs
    a presigned or otherwise reachable ``https://`` URL instead.
    """
    parsed = urlparse(url)
    return f"https://{parsed.netloc}.s3.amazonaws.com{parsed.path}"


class StaticArtifactStorage(ReadOnlyArtifactStorage):
    """A published profile directory on a dumb static host (Pages, S3, ...).

    Unlike :class:`ApiArtifactStorage` (which talks to a live
    ``researcher_profiles.api`` server), this fetches the published files
    themselves (``profile.jsonld``, ``personality/*.md``, ``sources/*.jsonld``)
    with one lazy GET per artifact. Summaries are enumerated from the manifest
    in ``profile.jsonld``, since a static host has no directory listing.

    ``persona`` and ``index`` are not overridden: the local managers are built,
    and the index's ``require_directory`` refuses with the
    ``install_profile()`` message.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 30.0,
        client: Any = None,
    ):
        import httpx

        if url.startswith("s3://"):
            url = _s3_to_https(url)
        self.base_url = url.rstrip("/")
        parsed = urlparse(self.base_url)
        self._slug = parsed.path.rstrip("/").rsplit("/", 1)[-1] or parsed.netloc
        self._timeout = timeout
        self._http = (
            client if client is not None else httpx.Client(timeout=timeout, follow_redirects=True)
        )
        self._owns_http = client is None

    def __repr__(self) -> str:
        return f"StaticArtifactStorage(slug={self._slug!r}, base_url={self.base_url!r})"

    # Identity

    @property
    def key(self) -> str:
        return f"static:{self.base_url}"

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def rid_hint(self) -> None:
        return None

    @property
    def directory(self) -> None:
        return None

    def locate(self, *parts: str) -> str:
        """Display-only locator: the published URL naming this artifact."""
        tail = "/".join(parts)
        return f"{self.base_url}/{tail}" if tail else self.base_url

    # Lifecycle

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # Fetch helper

    def _fetch(self, relpath: str) -> Optional[str]:
        """GET ``<base_url>/<relpath>``; ``None`` on 404."""
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
        return resp.text

    def _fetch_summary(self, paper_id: str) -> str:
        body = self._fetch(f"sources/summaries/{paper_id}{SUMMARY_SUFFIX}")
        if body is None:
            raise KeyError(paper_id)
        return body

    # Reads

    def content_hash(self) -> str:
        """``"sha256:<hex>"`` over the fetched document + soul.

        A read, so it stays supported on a read-only view.
        """
        h = hashlib.sha256()
        raw = self._fetch("profile.jsonld")
        data = json.loads(raw) if raw is not None else {}
        h.update(canonical_dumps(data if isinstance(data, dict) else {}).encode("utf-8"))
        h.update(b"\x00")
        h.update((self.load_soul() or "").encode("utf-8"))
        return f"sha256:{h.hexdigest()}"

    def load_document(self) -> ProfileDocument:
        url = f"{self.base_url}/profile.jsonld"
        raw = self._fetch("profile.jsonld")
        if raw is None:
            raise ProfileLoadError(url, "no profile.jsonld at base URL")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProfileLoadError(url, f"JSON parse error: {e}", e) from e
        try:
            return ProfileDocument.model_validate(data or {})
        except ValidationError as e:
            raise ProfileLoadError(url, f"schema error: {e}", e) from e

    def load_expertise(self) -> str:
        return self._fetch(EXPERTISE_URL) or ""

    def load_soul(self) -> str:
        return self._fetch(SOUL_URL) or ""

    def load_papers(self) -> list[PaperRecord]:
        url = f"{self.base_url}/{PAPERS_URL}"
        raw = self._fetch(PAPERS_URL)
        if raw is None:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProfileLoadError(url, f"JSON parse error: {e}", e) from e
        try:
            return PapersDocument.model_validate(data or {}).has_part
        except (ValidationError, ValueError) as e:
            raise ProfileLoadError(url, f"schema error: {e}", e) from e

    def load_grants(self) -> list[GrantRecord]:
        url = f"{self.base_url}/{GRANTS_URL}"
        raw = self._fetch(GRANTS_URL)
        if raw is None:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProfileLoadError(url, f"JSON parse error: {e}", e) from e
        try:
            return GrantsDocument.model_validate(data or {}).has_part
        except (ValidationError, ValueError) as e:
            raise ProfileLoadError(url, f"schema error: {e}", e) from e

    def load_citations(self) -> Any:
        url = f"{self.base_url}/{CITATIONS_URL}"
        raw = self._fetch(CITATIONS_URL)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProfileLoadError(url, f"JSON parse error: {e}", e) from e

    def load_summaries(self) -> Mapping[str, str]:
        # A static host has no directory listing, so enumerate from the
        # manifest, which exists precisely for this.
        ids = sorted(
            p.paper_id
            for p in self.load_document().has_part
            if p.role == "paper_summary" and p.paper_id
        )
        return LazySummaries(ids, self._fetch_summary)

    # Raw bodies

    def artifact_text(self, content_url: str) -> Optional[str]:
        """Whatever the host serves at that relative path.

        A published static directory is the manifest's address space, so any
        ``contentUrl`` is fetchable, subject to the host's own access rules,
        which surface as ``PermissionError``.
        """
        return self._fetch(content_url)

    def artifact_bytes(self, content_url: str) -> Optional[bytes]:
        text = self._fetch(content_url)
        return None if text is None else text.encode("utf-8")

    def collection_envelope(self, content_url: str) -> dict:
        raw = self._fetch(content_url)
        if raw is not None:
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k != "hasPart"}
        about = None
        try:
            about = self.load_document().id_
        except ProfileError:  # pragma: no cover - an unreachable host
            pass
        return _empty_collection_envelope(content_url, about)

    def build_manifest(self) -> tuple[list[ArtifactRef], list[ArtifactRef]]:
        """The recorded manifest: a static host has no directory to walk."""
        doc = self.load_document()
        return list(doc.has_part), list(doc.subject_of)
