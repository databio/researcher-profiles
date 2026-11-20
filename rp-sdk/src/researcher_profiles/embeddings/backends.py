"""The embedding backends: the models that turn text into vectors, local or API.

The default, ``st:all-MiniLM-L6-v2`` (dim=384), runs locally with no API key
or network after the first download. ``fastembed:`` runs the same weights
through ONNX instead of torch. ``tei:`` sends the text to a Hugging Face Text
Embeddings Inference server (such as Embed API) that runs fastembed, so its
vectors are stored under the ``fastembed:`` label (:func:`stored_name_of`).
OpenAI and Voyage need keys, network and per-call cost, so a profile indexed
with them cannot be searched offline. The label of the backend used at build
time is recorded in the index, and search uses an encoder in the same space
(:func:`same_space`, :func:`encoder_for`).
"""

import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..env import check_retired_env_vars

if TYPE_CHECKING:  # pragma: no cover - typing only
    import httpx

logger = logging.getLogger(__name__)


class MissingEmbeddingBackendError(RuntimeError):
    """Raised when the chosen embedding backend is not importable / usable."""


class EmbeddingBackendConfigError(MissingEmbeddingBackendError):
    """The backend is set up wrong: a missing or bad setting, a key the server
    rejects, or a URL with no such model. Retrying does not help; fix the settings."""


class EmbeddingModelMismatchError(EmbeddingBackendConfigError):
    """The server serves another model, or vectors unlike the ones the index
    holds (other size, dtype or truncation)."""


class EmbeddingBackendUnavailableError(MissingEmbeddingBackendError):
    """The remote encoder cannot answer now: down, slow, busy, out of quota,
    or it returned garbage. Retrying later may help."""


@runtime_checkable
class EmbeddingBackend(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


#: Ways to run the same float32 sentence-transformers weights: in process with
#: torch (st), in process with ONNX (fastembed), or on a TEI server that runs
#: fastembed (tei; TEIBackend.check() refuses a server whose dtype or truncation differs).
_SAME_WEIGHTS_PREFIXES = ("st", "fastembed", "tei")


def same_space(encoder_name: str | None, index_spec: str | None) -> bool:
    """Whether a query from ``encoder_name`` may be compared with ``index_spec`` vectors.

    True when the names are equal, or when both are the same model under the
    ``st:``, ``fastembed:`` and ``tei:`` prefixes (the same weights; ``/match``
    already treats them as one space). A ``tei:`` encoder is compared with
    the ``fastembed:`` labels its vectors are stored under. Case and any
    ``org/`` before the model name are ignored.
    """
    if not encoder_name or not index_spec:
        return False
    if encoder_name == index_spec:
        return True

    def split(spec: str) -> tuple[str, str]:
        prefix, _, model = spec.partition(":")
        return prefix.lower(), model.split("/")[-1].lower()

    (pa, ma), (pb, mb) = split(encoder_name), split(index_spec)
    return pa in _SAME_WEIGHTS_PREFIXES and pb in _SAME_WEIGHTS_PREFIXES and ma == mb and bool(ma)


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


#: onnxruntime's CPU memory arena for fastembed models. ``None`` keeps
#: onnxruntime's default (on). Set with :func:`set_fastembed_mem_arena`.
_FASTEMBED_MEM_ARENA: bool | None = None


def set_fastembed_mem_arena(enabled: bool | None) -> None:
    """Turn onnxruntime's CPU memory arena on or off for fastembed models.

    The arena is a private pool that grows to fit the largest batch the model
    has run and never shrinks, so a long-running process that embeds one big
    batch keeps that memory for life. Off returns it after each batch, at a
    small speed cost. ``None`` restores onnxruntime's default (on), which
    suits bulk index builds.

    Set this before the first embed. A model is loaded once per model name and
    shared by the whole process, so a model already loaded keeps the setting
    it was built with.
    """
    global _FASTEMBED_MEM_ARENA
    _FASTEMBED_MEM_ARENA = enabled


class FastEmbedBackend:
    """Local ONNX backend via fastembed. Same weights as ``st:``, no torch.

    About 175 MB installed instead of about 6 GB, so a local encoder fits in a
    container. Vectors agree with ``st:`` at cosine >= 0.999999, but the index
    is not interchangeable with ``st:``: ``name`` differs, and ``build_index``
    refuses a mismatch (switch with ``rp index <profile> --force``). ``tei:``
    writes the same ``fastembed:`` label, because Embed API runs fastembed.

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
                    kw = {}
                    if _FASTEMBED_MEM_ARENA is not None:
                        kw["enable_cpu_mem_arena"] = _FASTEMBED_MEM_ARENA
                    cached = TextEmbedding(model_name=full_name, **kw)
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


# ---------------------------------------------------------------------------
# tei: a remote encoder over the Hugging Face TEI API
# ---------------------------------------------------------------------------

#: Most texts one ``POST /embed`` carries: Embed API's per-key
#: ``max_batch_size`` default and ceiling. ``check()`` lowers it to the key's own.
TEI_MAX_BATCH = 64
#: Seconds one batch request (at most :data:`TEI_MAX_BATCH` texts) may take,
#: retry waits included. ``RESEARCHER_PROFILES_EMBEDDING_TIMEOUT`` overrides.
DEFAULT_TEI_TIMEOUT_S = 60.0
#: Seconds a one-text call (a query) may take, retry waits included; never
#: more than the batch timeout.
DEFAULT_TEI_QUERY_TIMEOUT_S = 5.0
_TEI_CONNECT_TIMEOUT_S = 5.0
_TEI_CHECK_TIMEOUT_S = 10.0
#: A ``Retry-After`` longer than this is the daily quota, not a busy model:
#: fail at once instead of sleeping.
_TEI_MAX_RETRY_WAIT_S = 10.0
_TEI_DEFAULT_RETRY_WAIT_S = 1.0
_TEI_MAX_ATTEMPTS = 8

_TEI_URL_ENV = "RESEARCHER_PROFILES_EMBEDDING_DENSE_URL"
_TEI_KEY_ENV = "RESEARCHER_PROFILES_EMBEDDING_API_KEY"
_TEI_TIMEOUT_ENV = "RESEARCHER_PROFILES_EMBEDDING_TIMEOUT"
_TEI_URL_EXAMPLE = "https://embed.databio.org/tei/all-MiniLM-L6-v2"

# Test seams: the retry loop's clock and sleep, and an ``httpx.BaseTransport``
# every shared client uses when set.
_sleep = time.sleep
_monotonic = time.monotonic
_TEI_TRANSPORT = None


def _positive_seconds(value, what: str) -> float:
    """``value`` as a positive, finite number of seconds, else a config error naming ``what``."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        raise EmbeddingBackendConfigError(
            f"{what} must be a positive number of seconds, got {value!r}"
        )
    return seconds


def _tei_env() -> tuple[str | None, str | None, float]:
    """``(url, api_key, timeout)`` from the environment; an empty value counts as unset."""
    check_retired_env_vars()
    url = (os.environ.get(_TEI_URL_ENV) or "").strip() or None
    key = (os.environ.get(_TEI_KEY_ENV) or "").strip() or None
    raw = (os.environ.get(_TEI_TIMEOUT_ENV) or "").strip()
    timeout = _positive_seconds(raw, _TEI_TIMEOUT_ENV) if raw else DEFAULT_TEI_TIMEOUT_S
    return url, key, timeout


@dataclass(frozen=True)
class TEIServerInfo:
    """What :meth:`TEIBackend.check` learned about the server."""

    url: str
    model_id: str | None
    served_model_name: str | None
    dtype: str | None
    max_input_length: int | None
    #: Texts per request this backend sends: the server's limit, capped at 64.
    max_batch: int
    #: Vector size of the probe; ``None`` until a vector was seen.
    dim: int | None


@dataclass
class _TeiConnection:
    """One HTTP client per process and ``(url, api_key)``, with what it has learned."""

    url: str
    http: "httpx.Client"
    pid: int
    has_key: bool
    #: Set by 401/403 or a mismatch: every later call raises it, sending nothing.
    fatal: EmbeddingBackendConfigError | None = None
    #: Set once ``/info`` passed the model check.
    checked: TEIServerInfo | None = None
    #: Vector size of the first vector this server returned.
    dim: int | None = None
    check_lock: threading.Lock = field(default_factory=threading.Lock)


_TEI_LOCK = threading.Lock()
_TEI_CONNECTIONS: dict[tuple[str, str | None], _TeiConnection] = {}


def _import_httpx():
    try:
        import httpx
    except ImportError as e:
        raise EmbeddingBackendConfigError(
            "httpx is required for the 'tei' backend. Requires the 'tei' extra; "
            "see the install instructions in the README."
        ) from e
    return httpx


def _new_tei_client(httpx, url: str, api_key: str | None, transport) -> "httpx.Client":
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # The server's proxy may cap open connections per client IP, shared by
    # every service on the calling host; a new TLS connection is slow.
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=60.0)
    # httpx ignores ``limits`` once a transport is given, so the default
    # transport carries them. retries=1 retries only connect failures, where
    # nothing was sent.
    transport = transport or _TEI_TRANSPORT or httpx.HTTPTransport(retries=1, limits=limits)
    client = httpx.Client(base_url=url, headers=headers, limits=limits, transport=transport)
    logger.info("tei backend: new HTTP client for %s", url)
    return client


def _tei_connection(url: str, api_key: str | None) -> _TeiConnection:
    """The shared connection for ``(url, api_key)``, rebuilt in a forked child."""
    httpx = _import_httpx()
    pid = os.getpid()
    with _TEI_LOCK:
        conn = _TEI_CONNECTIONS.get((url, api_key))
        if conn is not None and conn.pid == pid:
            return conn
        # A forked worker must never share its parent's sockets. What the
        # parent learned (a rejected key, a passed check) still holds.
        fresh = _TeiConnection(
            url=url,
            http=_new_tei_client(httpx, url, api_key, None),
            pid=pid,
            has_key=bool(api_key),
        )
        if conn is not None:
            fresh.fatal, fresh.checked, fresh.dim = conn.fatal, conn.checked, conn.dim
        _TEI_CONNECTIONS[(url, api_key)] = fresh
        return fresh


def reset_tei_connections() -> None:
    """Close and forget every shared TEI client, and with them any remembered error.

    For tests and shutdown; a host normally never needs it.
    """
    with _TEI_LOCK:
        conns = list(_TEI_CONNECTIONS.values())
        _TEI_CONNECTIONS.clear()
    for conn in conns:
        if conn.pid != os.getpid():
            continue  # a parent's client: closing it here would touch its sockets
        try:
            conn.http.close()
        # Best effort at shutdown; a client that fails to close is dropped anyway.
        except Exception:
            logger.debug("could not close the tei client for %s", conn.url, exc_info=True)


def _server_error(resp) -> str:
    """The server's ``{"error", "error_type"}`` as text, else the start of the body."""
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and ("error" in payload or "error_type" in payload):
        kind = payload.get("error_type")
        text = str(payload.get("error") or "")
        return f"{kind}: {text}" if kind else text
    return (resp.text or "")[:300]


def _retry_after(resp) -> float:
    """Seconds the server asked to wait; 1.0 when missing or unparseable."""
    try:
        wait = float(resp.headers.get("Retry-After", ""))
    except ValueError:
        return _TEI_DEFAULT_RETRY_WAIT_S
    return wait if math.isfinite(wait) and wait > 0 else _TEI_DEFAULT_RETRY_WAIT_S


def _fatal(conn: _TeiConnection, error: EmbeddingBackendConfigError):
    """Remember ``error`` on ``conn`` so later calls send nothing, and return it to raise."""
    conn.fatal = error
    return error


def _raise_fatal(conn: _TeiConnection) -> None:
    if conn.fatal is not None:
        # A fresh instance, so a remembered error does not grow one traceback forever.
        raise type(conn.fatal)(*conn.fatal.args)


def _raise_for_status(conn: _TeiConnection, resp, path: str) -> None:
    """Map a non-200 answer to the error table. Always raises."""
    status = resp.status_code
    where = f"{conn.url}{path}"
    if status in (401, 403):
        if not conn.has_key:
            why = f"no API key is set ({_TEI_KEY_ENV})"
        elif status == 401:
            why = f"it rejected the API key ({_TEI_KEY_ENV})"
        else:
            why = f"the API key ({_TEI_KEY_ENV}) may not use this model: {_server_error(resp)}"
        raise _fatal(
            conn, EmbeddingBackendConfigError(f"Embed API at {conn.url} answered {status}: {why}")
        )
    if status == 404:
        raise EmbeddingBackendConfigError(
            f"no TEI model at {where} (404); {_TEI_URL_ENV} should look like {_TEI_URL_EXAMPLE}"
        )
    if status in (400, 413, 422):
        raise EmbeddingBackendConfigError(
            f"{where} refused the request ({status} {_server_error(resp)})"
        )
    raise EmbeddingBackendUnavailableError(f"{where} answered {status} {_server_error(resp)}")


def _parse_vectors(conn: _TeiConnection, resp, n: int) -> list[list[float]]:
    """``n`` vectors from a ``/embed`` answer, or the error the table says."""
    if resp.status_code != 200:
        _raise_for_status(conn, resp, "/embed")
    try:
        data = resp.json()
    except ValueError:
        data = None
    ok = (
        isinstance(data, list)
        and len(data) == n
        and all(
            isinstance(v, list)
            and v
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)
            for v in data
        )
        and len({len(v) for v in data}) == 1
    )
    if not ok:
        raise EmbeddingBackendUnavailableError(f"unexpected response shape from {conn.url}/embed")
    return [list(map(float, v)) for v in data]


def _human_seconds(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}h{rest // 60:02d}m" if hours else f"{max(1, rest // 60)}m"


class TEIBackend:
    """Remote encoder over the Hugging Face Text Embeddings Inference (TEI) API.

    One base URL per model (``POST /embed``, ``GET /info``). Embed API runs
    fastembed's TextEmbedding with the same model files, so vectors are stored
    under ``stored_name`` (``fastembed:<model>``) while ``name`` (``tei:<model>``)
    is what config and logs show. Imports nothing heavier than httpx; never
    falls back to a local model. The constructor does no network I/O.

    The URL and key come from the arguments or from
    ``RESEARCHER_PROFILES_EMBEDDING_DENSE_URL`` /
    ``RESEARCHER_PROFILES_EMBEDDING_API_KEY``, never from a spec dict (a spec
    can come from a profile document). One HTTP client per process and
    ``(url, key)`` is shared by every instance. A one-text call is a query and
    gets ``query_timeout`` (5 s); longer calls go out in requests of at most
    ``batch_size`` texts, each with ``timeout`` (60 s), retry waits included.
    A 401/403 or a model mismatch is remembered: every later call in the
    process raises it at once without sending anything.
    """

    _DEFAULT_DIMS = {
        "all-MiniLM-L6-v2": 384,
        "all-MiniLM-L12-v2": 384,
        "all-mpnet-base-v2": 768,
    }
    #: Tokens fastembed truncates at; check() refuses a server that differs.
    _MAX_INPUT_TOKENS = {"all-MiniLM-L6-v2": 256}

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        *,
        url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        query_timeout: float | None = None,
        batch_size: int | None = None,
        transport: "httpx.BaseTransport | None" = None,
    ):
        env_url, env_key, env_timeout = _tei_env()
        self.model_name = model_name
        self.name = f"tei:{model_name}"
        #: The label written next to this backend's vectors: what
        #: ``FastEmbedBackend(model_name)`` writes, since the server runs fastembed.
        self.stored_name = f"fastembed:{model_name}"
        self._short = model_name.split("/")[-1]
        self.dim = self._DEFAULT_DIMS.get(self._short, 384)
        self._initial_dim = self.dim
        self.url = (url or env_url or "").strip().rstrip("/")
        if not self.url:
            raise EmbeddingBackendConfigError(
                f"the 'tei' backend needs a TEI base URL: set {_TEI_URL_ENV} "
                f"(for example {_TEI_URL_EXAMPLE})"
            )
        if not self.url.startswith(("http://", "https://")):
            raise EmbeddingBackendConfigError(
                f"{_TEI_URL_ENV} must start with http:// or https://, got {self.url!r} "
                f"(for example {_TEI_URL_EXAMPLE})"
            )
        self._api_key = api_key if api_key is not None else env_key  # never in repr or logs
        self.timeout = (
            env_timeout if timeout is None else _positive_seconds(timeout, "the tei timeout")
        )
        self.query_timeout = min(
            DEFAULT_TEI_QUERY_TIMEOUT_S
            if query_timeout is None
            else _positive_seconds(query_timeout, "the tei query_timeout"),
            self.timeout,
        )
        if batch_size is not None and not (
            isinstance(batch_size, int) and 1 <= batch_size <= TEI_MAX_BATCH
        ):
            raise ValueError(
                f"tei batch_size must be an integer from 1 to {TEI_MAX_BATCH}, got {batch_size!r}"
            )
        self.batch_size = batch_size or TEI_MAX_BATCH
        self._transport = transport
        self._own_conn: _TeiConnection | None = None

    def __repr__(self) -> str:
        return f"TEIBackend({self.name!r}, url={self.url!r})"

    # --- the connection ----------------------------------------------------

    def _connection(self) -> _TeiConnection:
        if self._transport is None:
            return _tei_connection(self.url, self._api_key)
        # A transport of its own (tests): an unshared client.
        if self._own_conn is None or self._own_conn.pid != os.getpid():
            self._own_conn = _TeiConnection(
                url=self.url,
                http=_new_tei_client(_import_httpx(), self.url, self._api_key, self._transport),
                pid=os.getpid(),
                has_key=bool(self._api_key),
            )
        return self._own_conn

    # --- embedding ---------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        texts = list(texts)
        conn = self._connection()
        _raise_fatal(conn)
        info = self._ensure_checked(conn)
        if len(texts) == 1:
            out = self._post(conn, texts, self.query_timeout)
        else:
            size = max(1, min(self.batch_size, info.max_batch))
            out = []
            for i in range(0, len(texts), size):
                out.extend(self._post(conn, texts[i : i + size], self.timeout))
        self._check_dim(conn, len(out[0]))
        self.dim = len(out[0])
        return out

    def _post(self, conn: _TeiConnection, texts: list[str], budget: float) -> list[list[float]]:
        """One ``POST /embed``, retried on 429/503 within ``budget`` seconds."""
        httpx = _import_httpx()
        deadline = _monotonic() + budget
        for _attempt in range(_TEI_MAX_ATTEMPTS):
            remaining = deadline - _monotonic()
            if remaining <= 0:
                break
            try:
                resp = conn.http.post(
                    "/embed",
                    json={"inputs": texts, "truncate": True},
                    timeout=httpx.Timeout(
                        remaining, connect=min(_TEI_CONNECT_TIMEOUT_S, remaining)
                    ),
                )
            except httpx.TimeoutException as e:
                raise EmbeddingBackendUnavailableError(
                    f"Embed API at {conn.url} timed out after {budget:.0f}s"
                ) from e
            except httpx.HTTPError as e:
                raise EmbeddingBackendUnavailableError(
                    f"Embed API at {conn.url} is unreachable: {e!r}"
                ) from e
            if resp.status_code in (429, 503):
                wait = _retry_after(resp)
                if wait > _TEI_MAX_RETRY_WAIT_S:
                    raise EmbeddingBackendUnavailableError(
                        f"Embed API at {conn.url} asked to wait {wait:.0f}s "
                        f"({resp.status_code} {_server_error(resp)}): the daily quota for this "
                        f"key is likely used up; it resets in about {_human_seconds(wait)}"
                    )
                if wait >= deadline - _monotonic():
                    break
                logger.info(
                    "Embed API at %s busy (%s); retrying in %.1fs",
                    conn.url,
                    resp.status_code,
                    wait,
                )
                _sleep(wait)
                continue
            return _parse_vectors(conn, resp, len(texts))
        raise EmbeddingBackendUnavailableError(
            f"Embed API at {conn.url} stayed busy for {budget:.0f}s; gave up"
        )

    # --- the model check ---------------------------------------------------

    def _expected_dims(self) -> set[int]:
        """The vector sizes this spec requires: the model table's and an explicit ``dim``."""
        out: set[int] = set()
        table = self._DEFAULT_DIMS.get(self._short)
        if table is not None:
            out.add(table)
        if table is not None or self.dim != self._initial_dim:
            out.add(self.dim)
        return out

    def _check_dim(self, conn: _TeiConnection, got: int) -> None:
        """Refuse (and remember) vectors whose size is not the one the index needs."""
        wanted = self._expected_dims()
        if conn.dim is not None:
            wanted.add(conn.dim)
        if any(got != w for w in wanted):
            raise _fatal(
                conn,
                EmbeddingModelMismatchError(
                    f"Embed API at {conn.url} returns {got}-d vectors, but "
                    f"{self.name!r} needs {'/'.join(str(w) for w in sorted(wanted))}-d"
                ),
            )
        conn.dim = got

    def _mismatch(self, conn: _TeiConnection, what: str) -> EmbeddingModelMismatchError:
        return _fatal(
            conn,
            EmbeddingModelMismatchError(
                f"Embed API at {conn.url} {what}, but RESEARCHER_PROFILES_EMBEDDING_BACKEND "
                f"asks for {self.name!r}"
            ),
        )

    def _model_matches(self, info: TEIServerInfo) -> bool:
        names = {info.served_model_name, info.model_id}
        if info.model_id:
            names.add(info.model_id.split("/")[-1])
        return self.model_name in names

    def _read_info(self, conn: _TeiConnection) -> TEIServerInfo:
        """``GET /info``, checked against this spec. Raises Unavailable when the server is down."""
        httpx = _import_httpx()
        try:
            resp = conn.http.get(
                "/info",
                timeout=httpx.Timeout(_TEI_CHECK_TIMEOUT_S, connect=_TEI_CONNECT_TIMEOUT_S),
            )
        except httpx.TimeoutException as e:
            raise EmbeddingBackendUnavailableError(
                f"Embed API at {conn.url} timed out after {_TEI_CHECK_TIMEOUT_S:.0f}s on /info"
            ) from e
        except httpx.HTTPError as e:
            raise EmbeddingBackendUnavailableError(
                f"Embed API at {conn.url} is unreachable: {e!r}"
            ) from e
        if resp.status_code != 200:
            _raise_for_status(conn, resp, "/info")
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise EmbeddingBackendUnavailableError(
                f"unexpected response shape from {conn.url}/info"
            )

        model_type = body.get("model_type")
        embedding = model_type.get("embedding") if isinstance(model_type, dict) else None
        if not isinstance(embedding, dict):
            raise self._mismatch(conn, f"serves no embedding model (model_type: {model_type!r})")
        if str(embedding.get("pooling") or "").lower() == "splade":
            raise self._mismatch(conn, "serves a sparse (SPLADE) model")
        model_id = body.get("model_id")
        served = body.get("served_model_name")
        max_batch = body.get("max_client_batch_size")
        max_input = body.get("max_input_length")
        info = TEIServerInfo(
            url=conn.url,
            model_id=str(model_id) if model_id else None,
            served_model_name=str(served) if served else None,
            dtype=body.get("model_dtype"),
            max_input_length=max_input if isinstance(max_input, int) else None,
            max_batch=(
                min(TEI_MAX_BATCH, max_batch)
                if isinstance(max_batch, int) and max_batch > 0
                else TEI_MAX_BATCH
            ),
            dim=None,
        )
        if not self._model_matches(info):
            raise self._mismatch(conn, f"serves {served!r} ({model_id})")
        if info.dtype is not None and info.dtype != "float32":
            raise self._mismatch(
                conn,
                f"runs {model_id} as {info.dtype}, not the float32 weights the stored vectors "
                "came from",
            )
        tokens = self._MAX_INPUT_TOKENS.get(self._short)
        if tokens is not None and max_input is not None and max_input != tokens:
            raise self._mismatch(
                conn,
                f"truncates {model_id} at {max_input} tokens, not the {tokens} the stored "
                "vectors were made with",
            )
        return info

    def _ensure_checked(self, conn: _TeiConnection) -> TEIServerInfo:
        """The ``/info`` model check, once per connection, before any vector is used.

        Unlike :meth:`check`, a server that is down raises
        :class:`EmbeddingBackendUnavailableError`, and no probe is embedded:
        the first answer's vector size is checked instead.
        """
        info = conn.checked
        if info is None:
            with conn.check_lock:
                _raise_fatal(conn)
                if conn.checked is None:
                    conn.checked = self._read_info(conn)
                info = conn.checked
        if not self._model_matches(info):
            raise EmbeddingModelMismatchError(
                f"Embed API at {conn.url} serves {info.served_model_name!r} "
                f"({info.model_id}), but this backend is {self.name!r}"
            )
        return info

    def check(self) -> TEIServerInfo | None:
        """The startup check: ``/info``, the model, dtype and truncation, and a probe's size.

        Returns ``None`` (after a WARNING) when the server cannot be reached,
        is busy or fails. Raises :class:`EmbeddingBackendConfigError` on a
        rejected key or a URL with no model, and :class:`EmbeddingModelMismatchError`
        when the server answers with another model or vectors unlike the
        index's; both are remembered. Lowers ``batch_size`` to the key's limit.
        """
        from ..utils.const import EMBEDDING_PROBE_TEXT

        conn = self._connection()
        _raise_fatal(conn)
        with conn.check_lock:
            _raise_fatal(conn)
            try:
                info = self._read_info(conn)
                probe = self._post(conn, [EMBEDDING_PROBE_TEXT], _TEI_CHECK_TIMEOUT_S)
            except EmbeddingBackendUnavailableError as e:
                logger.warning(
                    "Embed API at %s is unreachable at startup (%s); embedding calls will "
                    "fail until it is back",
                    conn.url,
                    e,
                )
                return None
            self._check_dim(conn, len(probe[0]))
            info = replace(info, dim=conn.dim)
            conn.checked = info
        self.dim = int(conn.dim)
        self.batch_size = min(self.batch_size, info.max_batch)
        logger.info(
            "tei backend ready: %s serves %s (%s, %d-d, max_input_length %s, batch %d)",
            conn.url,
            info.model_id,
            info.dtype or "dtype unknown",
            self.dim,
            info.max_input_length,
            self.batch_size,
        )
        return info


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
    - ``"tei:all-MiniLM-L6-v2"`` -> TEIBackend (URL and key from the environment)
    - ``"openai:text-embedding-3-small"`` -> OpenAIBackend
    - ``"voyage:voyage-3-lite"`` -> VoyageBackend
    - ``{"backend": "st", "model": "...", "dim": 384}`` -> full override
    - ``{"backend": "tei", "model": "...", "dim": ..., "batch_size": ...,
      "timeout": ..., "query_timeout": ...}`` -> TEIBackend; a tei dict may not
      carry ``url`` or ``api_key`` (``ValueError``), because a spec can come
      from a profile document
    - ``None`` -> package default (``st:all-MiniLM-L6-v2``)

    ``batch_size`` (or a dict spec's ``"batch_size"``) applies only to
    fastembed and tei (1 to 64); setting it for any other backend raises
    ``ValueError``.
    """
    from ..utils.const import DEFAULT_BACKEND_SPEC

    if spec is None:
        spec = DEFAULT_BACKEND_SPEC
    if isinstance(spec, dict) and batch_size is None:
        batch_size = spec.get("batch_size")
    kind = spec.get("backend") if isinstance(spec, dict) else str(spec).partition(":")[0]
    if batch_size is not None and kind not in ("fastembed", "tei"):
        raise ValueError(
            f"batch_size is only supported by the fastembed and tei backends, not {kind!r}"
        )

    if isinstance(spec, dict):
        kind = spec.get("backend")
        model = spec.get("model")
        dim = spec.get("dim")
        if kind == "st":
            backend = SentenceTransformerBackend(model or "all-MiniLM-L6-v2")
        elif kind == "fastembed":
            backend = FastEmbedBackend(model or "all-MiniLM-L6-v2", batch_size=batch_size)
        elif kind == "tei":
            if "url" in spec or "api_key" in spec:
                raise ValueError(
                    "a tei spec may not carry url or api_key: specs can come from profile "
                    f"documents; set {_TEI_URL_ENV} / {_TEI_KEY_ENV}"
                )
            backend = TEIBackend(
                model or "all-MiniLM-L6-v2",
                batch_size=batch_size,
                timeout=spec.get("timeout"),
                query_timeout=spec.get("query_timeout"),
            )
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
    if kind == "tei":
        return TEIBackend(model, batch_size=batch_size)
    if kind == "openai":
        return OpenAIBackend(model)
    if kind == "voyage":
        return VoyageBackend(model)
    raise ValueError(f"unknown backend kind: {kind!r}")


def stored_name_of(backend) -> str:
    """The label written next to vectors this backend makes.

    ``backend.stored_name`` when it has one (TEIBackend: ``fastembed:<model>``,
    since Embed API runs fastembed), else ``backend.name``. Every writer of a
    backend label goes through this; reads compare labels with same_space().
    """
    return getattr(backend, "stored_name", None) or backend.name


def encoder_for(index_spec: str) -> EmbeddingBackend:
    """The encoder this process uses for vectors stored under ``index_spec``.

    ``RESEARCHER_PROFILES_EMBEDDING_BACKEND`` names the encoder this process
    runs; when it is in the same space as ``index_spec`` it wins, so a ``tei:``
    process searches a ``fastembed:`` index without loading fastembed.
    Otherwise the index's own backend is built.
    """
    env = os.environ.get("RESEARCHER_PROFILES_EMBEDDING_BACKEND")
    if env and same_space(env, index_spec):
        return get_backend(env)
    return get_backend(index_spec)


def check_backend(backend) -> bool:
    """Run ``backend.check()`` when it has one (remote backends). False when the
    server could not be reached (a WARNING was logged); True otherwise. Raises
    EmbeddingBackendConfigError / EmbeddingModelMismatchError when it answers but is wrong."""
    check = getattr(backend, "check", None)
    return True if check is None else check() is not None
