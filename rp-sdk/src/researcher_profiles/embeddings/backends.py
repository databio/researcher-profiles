"""The embedding backends: the models that turn text into vectors, local or API.

The default, ``st:all-MiniLM-L6-v2`` (dim=384), runs locally with no API key
or network after the first download. OpenAI and Voyage need keys, network and
per-call cost, so a profile indexed with them cannot be searched offline. The
backend used at build time is recorded in the index, and search uses the same
one.
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

    ``torch.cuda.is_available()`` is true for a GPU the installed torch has no
    kernels for (e.g. sm_61 under a build shipping sm_75 and up), which then
    fails on the first forward pass with "no kernel image is available". So
    return ``"cpu"`` for such a card. ``RESEARCHER_PROFILES_EMBEDDING_DEVICE``
    overrides. ``None`` means "let sentence-transformers decide".
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
        # torch's binary compatibility rule: sm_XY runs on the same major
        # with an equal-or-higher minor.
        for arch in torch.cuda.get_arch_list():
            if not arch.startswith("sm_"):
                continue
            digits = arch[3:]
            if digits[:-1].isdigit() and digits[-1:].isdigit():
                if int(digits[:-1]) == major and int(digits[-1]) <= minor:
                    return None
        return "cpu"
    except (ImportError, RuntimeError, AttributeError, ValueError):
        logger.debug("could not probe the torch device; leaving it unset", exc_info=True)
        return None


class SentenceTransformerBackend:
    """Local sentence-transformers backend. Default. dim depends on model.

    Thread safety: model construction is not thread-safe (parallel calls race
    and raise "Cannot copy out of meta tensor"), so it runs under a class
    lock, and one model per ``model_name`` is shared process-wide.
    ``encode()`` on a loaded model is safe to share across threads.
    """

    _DEFAULT_DIMS = {
        "all-MiniLM-L6-v2": 384,
        "all-MiniLM-L12-v2": 384,
        "all-mpnet-base-v2": 768,
    }

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
        # The loaded model's dim wins over the table.
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


#: Batch size for fastembed backends given none. ``None`` keeps fastembed's
#: own (256), which peaks near 4.7 GB on abstract-length texts. Set with
#: :func:`set_fastembed_batch_size`; it reaches every backend the SDK builds.
_FASTEMBED_BATCH_SIZE: int | None = None


def set_fastembed_batch_size(batch_size: int | None) -> None:
    """Set the process-wide fastembed batch size; ``None`` restores fastembed's."""
    global _FASTEMBED_BATCH_SIZE
    if batch_size is not None and batch_size < 1:
        raise ValueError(f"batch_size must be a positive integer, got {batch_size!r}")
    _FASTEMBED_BATCH_SIZE = batch_size


class FastEmbedBackend:
    """Local ONNX backend via fastembed. Same weights as ``st:``, no torch.

    About 175 MB installed instead of about 6 GB, so a local encoder fits in a
    container. Vectors agree with ``st:`` at cosine >= 0.999999, but the index
    is not interchangeable: ``name`` differs, and ``build_index`` refuses a
    mismatch (switch with ``rp index <profile> --force``).

    Thread safety matches :class:`SentenceTransformerBackend`.
    """

    # Short name -> fastembed's HuggingFace path. Unmapped names pass through.
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

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", batch_size: int | None = None):
        self.model_name = model_name
        self.name = f"fastembed:{model_name}"
        self.dim = self._DEFAULT_DIMS.get(model_name, 384)
        #: Texts per ONNX batch. ``None`` falls back to the process-wide
        #: :func:`set_fastembed_batch_size`, then to fastembed's own (256).
        self.batch_size = batch_size
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        cached = FastEmbedBackend._MODEL_CACHE.get(self.model_name)
        if cached is None:
            with FastEmbedBackend._MODEL_LOCK:
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
        size = self.batch_size or _FASTEMBED_BATCH_SIZE
        kw = {"batch_size": size} if size else {}
        vectors = [list(map(float, v)) for v in model.embed(texts, **kw)]
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


def get_backend(spec: str | dict | None, *, batch_size: int | None = None) -> EmbeddingBackend:
    """Instantiate a backend from a spec.

    Accepted forms:

    - ``"st:all-MiniLM-L6-v2"`` -> SentenceTransformerBackend
    - ``"fastembed:all-MiniLM-L6-v2"`` -> FastEmbedBackend
    - ``"openai:text-embedding-3-small"`` -> OpenAIBackend
    - ``"voyage:voyage-3-lite"`` -> VoyageBackend
    - ``{"backend": "st", "model": "...", "dim": 384}`` -> full override
    - ``None`` -> package default (``st:all-MiniLM-L6-v2``)

    ``batch_size`` (or a dict spec's ``"batch_size"``) applies only to
    fastembed; setting it for any other backend raises ``ValueError``.
    """
    from ..utils.const import DEFAULT_BACKEND_SPEC

    if spec is None:
        spec = DEFAULT_BACKEND_SPEC
    if isinstance(spec, dict) and batch_size is None:
        batch_size = spec.get("batch_size")
    kind = spec.get("backend") if isinstance(spec, dict) else str(spec).partition(":")[0]
    if batch_size is not None and kind != "fastembed":
        raise ValueError(f"batch_size is only supported by the fastembed backend, not {kind!r}")

    if isinstance(spec, dict):
        kind = spec.get("backend")
        model = spec.get("model")
        dim = spec.get("dim")
        if kind == "st":
            backend = SentenceTransformerBackend(model or "all-MiniLM-L6-v2")
        elif kind == "fastembed":
            backend = FastEmbedBackend(model or "all-MiniLM-L6-v2", batch_size=batch_size)
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
        return FastEmbedBackend(model, batch_size=batch_size)
    if kind == "openai":
        return OpenAIBackend(model)
    if kind == "voyage":
        return VoyageBackend(model)
    raise ValueError(f"unknown backend kind: {kind!r}")
