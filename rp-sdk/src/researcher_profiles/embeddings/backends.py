"""The embedding backends: the models that turn text into vectors, local or API.

Backends produce dense vectors for arbitrary text. The default backend is
the local sentence-transformers ``all-MiniLM-L6-v2`` model, which runs
locally with no API key or network access (dim=384).

Tradeoffs
---------

- Local sentence-transformers (default): portable, no keys, free,
  offline. ~400MB model download on first use. Lower semantic quality
  than frontier API models. dim=384 (small, fast index).
- fastembed: the same weights as ``st:`` (default ``all-MiniLM-L6-v2``,
  dim=384) run through ONNX Runtime instead of PyTorch. No API key, no
  network after the first model download, and ~175MB installed instead
  of ~6GB, the difference between a local encoder fitting in a
  container and not. Its vectors are numerically near-identical to
  ``st:``'s (see ``FastEmbedBackend``), but its index is not
  interchangeable with an ``st:``-built one: the recorded backend name
  differs, and ``build_index`` refuses a mismatch.
- OpenAI / Voyage: higher quality, no model download, but require
  API keys, network, and per-call cost. A profile indexed with one of
  these cannot be searched offline. Larger vectors mean a
  larger on-disk index.

Default is ``st:all-MiniLM-L6-v2`` for portability. The backend used at
build time is persisted in the per-profile index; search instantiates
the same backend automatically.
"""

import logging
import os
import threading
from typing import Protocol, runtime_checkable

from ..env import check_retired_env_vars

logger = logging.getLogger(__name__)


class MissingEmbeddingBackendError(RuntimeError):
    """Raised when the chosen embedding backend is not importable / usable."""


@runtime_checkable
class EmbeddingBackend(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _usable_device() -> str | None:
    """Pick the device sentence-transformers should load the model on.

    ``torch.cuda.is_available()`` answers "is there a driver and a card",
    not "can this torch build run on that card". A GPU whose compute
    capability the installed torch has no compiled kernels for (e.g. a
    Pascal sm_61 card under a build shipping sm_75 and up) passes that
    check and then raises ``CUDA error: no kernel image is available for
    execution on the device`` on the first forward pass, deep inside the
    model where the cause is unrecognizable.

    So check up front and return ``"cpu"`` when the card cannot run this
    build; the embedding model is small enough that CPU is a real
    fallback, not a degraded one. ``RESEARCHER_PROFILES_EMBEDDING_DEVICE``
    overrides. Returns ``None`` to mean "let sentence-transformers decide",
    which is what happens on any machine whose GPU this torch does support.
    """
    check_retired_env_vars()
    override = os.environ.get("RESEARCHER_PROFILES_EMBEDDING_DEVICE")
    if override:
        return override
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        major, minor = torch.cuda.get_device_capability()
        # A kernel compiled for sm_XY runs on any device of the same major
        # version with an equal-or-higher minor, torch's own binary
        # compatibility rule.
        for arch in torch.cuda.get_arch_list():
            if not arch.startswith("sm_"):
                continue
            digits = arch[3:]
            if digits[:-1].isdigit() and digits[-1:].isdigit():
                if int(digits[:-1]) == major and int(digits[-1]) <= minor:
                    return None
        return "cpu"
    except (ImportError, RuntimeError, AttributeError, ValueError):
        # An unexpected torch shape is not our problem to diagnose; leave
        # device selection where it was.
        logger.debug("could not probe the torch device; leaving it unset", exc_info=True)
        return None


class SentenceTransformerBackend:
    """Local sentence-transformers backend. Default. dim depends on model.

    Concurrency notes
    -----------------

    SentenceTransformer / PyTorch model construction is not thread-safe:
    parallel calls to ``SentenceTransformer(model_name)`` can race during
    meta-tensor materialization and raise
    ``NotImplementedError: Cannot copy out of meta tensor; no data!``.

    We defend against this two ways:

    1. A class-level lock serializes the actual construction.
    2. A class-level cache shares one underlying model across all
       backend instances using the same ``model_name``, so N profiles
       all using ``st:all-MiniLM-L6-v2`` share a single ~80MB model
       in memory instead of loading it once per profile.

    ``model.encode()`` itself is internally thread-safe in
    sentence-transformers (it uses a stateless forward pass under
    ``torch.no_grad()``), so sharing the loaded model across threads is
    safe.
    """

    _DEFAULT_DIMS = {
        "all-MiniLM-L6-v2": 384,
        "all-MiniLM-L12-v2": 384,
        "all-mpnet-base-v2": 768,
    }

    # Process-wide model cache keyed by model_name. Multiple
    # SqliteEmbeddingIndex instances sharing the same model don't each pay the
    # download + RAM cost, and we avoid the concurrent-init race.
    _MODEL_CACHE: dict[str, object] = {}
    _MODEL_LOCK = threading.Lock()

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.name = f"st:{model_name}"
        self.dim = self._DEFAULT_DIMS.get(model_name, 384)
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        cached = SentenceTransformerBackend._MODEL_CACHE.get(self.model_name)
        if cached is None:
            with SentenceTransformerBackend._MODEL_LOCK:
                # Re-check under the lock: another thread may have
                # finished loading while we were waiting.
                cached = SentenceTransformerBackend._MODEL_CACHE.get(self.model_name)
                if cached is None:
                    try:
                        from sentence_transformers import SentenceTransformer
                    except ImportError as e:
                        raise MissingEmbeddingBackendError(
                            "sentence-transformers is required for the default backend. "
                            "Requires the 'st' extra; see the install instructions in "
                            "the README. (or use a remote backend: [openai] / [voyage])"
                        ) from e
                    device = _usable_device()
                    cached = (
                        SentenceTransformer(self.model_name, device=device)
                        if device
                        else SentenceTransformer(self.model_name)
                    )
                    SentenceTransformerBackend._MODEL_CACHE[self.model_name] = cached
        self._model = cached
        # Refine dim from the actual model in case our table is stale.
        try:
            self.dim = int(self._model.get_sentence_embedding_dimension())
        except (AttributeError, TypeError, ValueError):
            logger.debug(
                "model %r did not report a dimension; keeping dim=%d",
                self.model_name,
                self.dim,
                exc_info=True,
            )
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        arr = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return [list(map(float, v)) for v in arr]


class FastEmbedBackend:
    """Local ONNX backend via fastembed. Same weights as ``st:``, no torch.

    ``st:all-MiniLM-L6-v2`` and ``fastembed:all-MiniLM-L6-v2`` run the same
    published model. fastembed executes it under ONNX Runtime rather than
    PyTorch, which is why it costs ~175 MB installed instead of ~6 GB, and is
    what makes a local encoder viable in a container.

    The two are not interchangeable against an already-built index. ``name``
    differs, and ``build_index`` refuses a backend whose name does not match
    the one recorded in the index. Switching an existing index between them
    needs ``rp index <profile> --force``. Measured on the probe text plus a
    handful of realistic profile sentences, the two backends' vectors agree
    at cosine >= 0.999999, effectively identical, since it is the same
    trained weights and the ONNX export is a numerical conversion, not a
    retrain, so a value computed under one backend is a safe drop-in
    estimate under the other even though the index itself is not, and
    `EMBEDDING_PROBE_TEXT`-based consistency checks hold across the switch.

    Concurrency notes
    -----------------

    ``TextEmbedding(...)`` downloads and initializes an ONNX session. As with
    sentence-transformers we serialize construction behind a class-level lock
    and share one session per model name process-wide, so N profiles on the
    same model pay the download and the RAM once. ``embed()`` on a constructed
    session is a stateless forward pass and is safe to share across threads.
    """

    # fastembed addresses models by their full HuggingFace path. Accept the
    # short name the rest of the SDK uses and map it, so a spec string reads
    # the same for `st:` and `fastembed:`. An unmapped name is passed through
    # untouched, which lets a caller name any model fastembed supports.
    _MODEL_MAP = {
        "all-MiniLM-L6-v2": "sentence-transformers/all-MiniLM-L6-v2",
        "all-MiniLM-L12-v2": "sentence-transformers/all-MiniLM-L12-v2",
        "all-mpnet-base-v2": "sentence-transformers/all-mpnet-base-v2",
    }

    _DEFAULT_DIMS = {
        "all-MiniLM-L6-v2": 384,
        "all-MiniLM-L12-v2": 384,
        "all-mpnet-base-v2": 768,
    }

    _MODEL_CACHE: dict[str, object] = {}
    _MODEL_LOCK = threading.Lock()

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self.name = f"fastembed:{model_name}"
        self.dim = self._DEFAULT_DIMS.get(model_name, 384)
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        cached = FastEmbedBackend._MODEL_CACHE.get(self.model_name)
        if cached is None:
            with FastEmbedBackend._MODEL_LOCK:
                # Re-check under the lock: another thread may have
                # finished loading while we were waiting.
                cached = FastEmbedBackend._MODEL_CACHE.get(self.model_name)
                if cached is None:
                    try:
                        from fastembed import TextEmbedding
                    except ImportError as e:
                        raise MissingEmbeddingBackendError(
                            "fastembed is required for the 'fastembed' backend. "
                            "Requires the 'fastembed' extra; see the install "
                            "instructions in the README. (a local encoder with no "
                            "torch; see also [st])"
                        ) from e
                    full_name = self._MODEL_MAP.get(self.model_name, self.model_name)
                    cached = TextEmbedding(model_name=full_name)
                    FastEmbedBackend._MODEL_CACHE[self.model_name] = cached
        self._model = cached
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        # fastembed's embed() returns a generator of numpy arrays.
        vectors = [list(map(float, v)) for v in model.embed(texts)]
        if vectors:
            self.dim = len(vectors[0])
        return vectors


class OpenAIBackend:
    """OpenAI embeddings backend. Requires OPENAI_API_KEY and network."""

    _DEFAULT_DIMS = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(self, model: str = "text-embedding-3-small"):
        self.model = model
        self.name = f"openai:{model}"
        self.dim = self._DEFAULT_DIMS.get(model, 1536)
        self._client = None

    def _client_or_raise(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:
                raise MissingEmbeddingBackendError(
                    "openai is required for the OpenAI backend. "
                    "Requires the 'openai' extra; see the install instructions "
                    "in the README."
                ) from e
            if not os.environ.get("OPENAI_API_KEY"):
                raise MissingEmbeddingBackendError(
                    "OPENAI_API_KEY is not set; cannot use OpenAI embeddings."
                )
            self._client = OpenAI()
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._client_or_raise()
        out: list[list[float]] = []
        # Batch up to 100 inputs per request.
        for i in range(0, len(texts), 100):
            batch = texts[i : i + 100]
            resp = client.embeddings.create(model=self.model, input=batch)
            out.extend([list(map(float, d.embedding)) for d in resp.data])
        return out


class VoyageBackend:
    """Voyage AI embeddings backend. Requires VOYAGE_API_KEY."""

    _DEFAULT_DIMS = {
        "voyage-3-lite": 512,
        "voyage-3": 1024,
        "voyage-large-2": 1536,
    }

    def __init__(self, model: str = "voyage-3-lite"):
        self.model = model
        self.name = f"voyage:{model}"
        self.dim = self._DEFAULT_DIMS.get(model, 512)
        self._client = None

    def _client_or_raise(self):
        if self._client is None:
            try:
                import voyageai
            except ImportError as e:
                raise MissingEmbeddingBackendError(
                    "voyageai is required for the Voyage backend. "
                    "Requires the 'voyage' extra; see the install instructions "
                    "in the README."
                ) from e
            if not os.environ.get("VOYAGE_API_KEY"):
                raise MissingEmbeddingBackendError(
                    "VOYAGE_API_KEY is not set; cannot use Voyage embeddings."
                )
            self._client = voyageai.Client()
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        client = self._client_or_raise()
        out: list[list[float]] = []
        for i in range(0, len(texts), 100):
            batch = texts[i : i + 100]
            resp = client.embed(batch, model=self.model)
            out.extend([list(map(float, v)) for v in resp.embeddings])
        return out


def get_backend(spec: str | dict | None) -> EmbeddingBackend:
    """Instantiate a backend from a spec.

    Accepted forms:

    - ``"st:all-MiniLM-L6-v2"`` -> SentenceTransformerBackend
    - ``"fastembed:all-MiniLM-L6-v2"`` -> FastEmbedBackend
    - ``"openai:text-embedding-3-small"`` -> OpenAIBackend
    - ``"voyage:voyage-3-lite"`` -> VoyageBackend
    - ``{"backend": "st", "model": "...", "dim": 384}`` -> full override
    - ``None`` -> package default (``st:all-MiniLM-L6-v2``)
    """
    from ..utils.const import DEFAULT_BACKEND_SPEC

    if spec is None:
        spec = DEFAULT_BACKEND_SPEC

    if isinstance(spec, dict):
        kind = spec.get("backend")
        model = spec.get("model")
        dim = spec.get("dim")
        if kind == "st":
            backend = SentenceTransformerBackend(model or "all-MiniLM-L6-v2")
        elif kind == "fastembed":
            backend = FastEmbedBackend(model or "all-MiniLM-L6-v2")
        elif kind == "openai":
            backend = OpenAIBackend(model or "text-embedding-3-small")
        elif kind == "voyage":
            backend = VoyageBackend(model or "voyage-3-lite")
        else:
            raise ValueError(f"unknown backend kind: {kind!r}")
        if dim is not None:
            backend.dim = int(dim)
        return backend

    if not isinstance(spec, str) or ":" not in spec:
        raise ValueError(f"backend spec must look like 'kind:model', got {spec!r}")
    kind, _, model = spec.partition(":")
    if kind == "st":
        return SentenceTransformerBackend(model)
    if kind == "fastembed":
        return FastEmbedBackend(model)
    if kind == "openai":
        return OpenAIBackend(model)
    if kind == "voyage":
        return VoyageBackend(model)
    raise ValueError(f"unknown backend kind: {kind!r}")
