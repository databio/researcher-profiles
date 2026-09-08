"""Profile-level embedding vectors (centroid / expertise / summary).

Reached as ``prof.index.embedding(kind)``; :class:`IndexManager
<researcher_profiles.profile.index.IndexManager>` imports and calls
:func:`_profile_embedding_impl`. Persistent cache lives at
``<profile>/.cache/profile_vec.npz``.
"""

import logging
import os
import zipfile
from pathlib import Path
from typing import Any, Literal

import numpy as np

from ..utils.paths import cache_dir
from ._sqlite import IndexNotBuiltError, read_chunk_matrix, read_index_meta_path
from .backends import get_backend

logger = logging.getLogger(__name__)

_KIND = Literal["centroid", "summary", "expertise"]


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n < 1e-12:
        return v.astype(np.float32)
    return (v / n).astype(np.float32)


def _resolve_backend(profile) -> Any:
    env = os.environ.get("RESEARCHER_PROFILES_EMBEDDING_BACKEND")
    if env:
        return get_backend(env)
    cfg = getattr(profile, "config", None)
    spec = None
    if cfg is not None:
        spec = getattr(cfg, "embedding_backend", None)
        if spec is None and hasattr(cfg, "model_extra"):
            extra = cfg.model_extra or {}
            spec = extra.get("embedding_backend")
    return get_backend(spec) if spec else get_backend(None)


def _llm_errors() -> tuple[type[BaseException], ...]:
    """The failures of the optional LLM summary step, which is never fatal.

    ``anthropic`` is an optional extra reached only through ``..llm``, so its
    error base joins the tuple only when the package is importable; a missing
    install shows up as the ``ImportError`` that is already covered.
    """
    base: tuple[type[BaseException], ...] = (ImportError, RuntimeError)
    try:
        import anthropic
    except ImportError:
        return base
    return base + (anthropic.AnthropicError,)


def _cache_path(profile) -> Path:
    return cache_dir(profile.require_directory("the profile embedding")) / "profile_vec.npz"


def _load_cache_at(p: Path) -> dict[str, Any]:
    if not p.exists():
        return {}
    try:
        with np.load(str(p), allow_pickle=True) as data:
            return {k: data[k] for k in data.files}
    except (OSError, ValueError, zipfile.BadZipFile):
        return {}


def _load_cache(profile) -> dict[str, Any]:
    return _load_cache_at(_cache_path(profile))


def _save_cache_at(p: Path, cache: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    # numpy.savez can't handle plain strings as 0-d; wrap in arrays
    cleaned: dict[str, Any] = {}
    for k, v in cache.items():
        if isinstance(v, np.ndarray):
            cleaned[k] = v
        else:
            cleaned[k] = np.array(v)
    np.savez(str(p), **cleaned)


def _save_cache(profile, cache: dict[str, Any]) -> None:
    _save_cache_at(_cache_path(profile), cache)


def _scalar(value: Any) -> Any:
    """The Python scalar inside a 0-d array (``np.savez`` wraps every scalar), else ``value``."""
    try:
        return np.asarray(value).reshape(()).item()
    except (TypeError, ValueError):
        return value


def _cache_matches_index(cache: dict[str, Any], backend_name: str, dim: int) -> bool:
    """Whether the cached vectors were built by this backend at this dim."""
    cached_backend = str(_scalar(cache["_backend_name"])) if "_backend_name" in cache else ""
    cached_dim = 0
    if "_dim" in cache:
        try:
            cached_dim = int(_scalar(cache["_dim"]))
        except (TypeError, ValueError):
            cached_dim = 0
    return cached_backend == backend_name and cached_dim == dim


def _centroid_vec(db_path: Path, dim: int) -> np.ndarray:
    """Normalized mean of every chunk vector in the index."""
    mat = read_chunk_matrix(db_path, dim)
    if mat.shape[0] == 0:
        raise IndexNotBuiltError(f"Index {db_path} has no vectors")
    return _normalize(mat.mean(axis=0))


def index_centroid(db_path: Path, cache_path: Path) -> np.ndarray:
    """One sqlite index's centroid, read from (or written to) its npz cache.

    Takes the two paths rather than a profile because the caller that matters
    is :meth:`SqliteEmbeddingIndex.centroid
    <researcher_profiles.embeddings.cache.SqliteEmbeddingIndex.centroid>`,
    which is the profile-free half of the
    :class:`~researcher_profiles.embeddings.protocol.VectorIndex` contract.
    ``_profile_embedding_impl`` routes its ``"centroid"`` kind here, so there
    is one implementation and one cache format, not two that can drift.
    """
    meta = read_index_meta_path(db_path)
    backend_name = meta.get("backend_name", "")
    dim = int(meta.get("embedding_dim", "0") or 0)

    cache = _load_cache_at(cache_path)
    if _cache_matches_index(cache, backend_name, dim):
        if "centroid" in cache:
            return np.asarray(cache["centroid"], dtype=np.float32)
    else:
        cache = {"_backend_name": backend_name, "_dim": dim}

    vec = _centroid_vec(db_path, dim)
    cache["centroid"] = vec
    _save_cache_at(cache_path, cache)
    return vec


def _expertise_vec(profile, dim: int) -> np.ndarray:
    """Embedding of the expertise text; a zero vector when there is none."""
    be = _resolve_backend(profile)
    text = profile.expertise or ""
    if not text.strip():
        return np.zeros((dim,), dtype=np.float32)
    raw = be.embed([text])[0]
    return _normalize(np.array(raw, dtype=np.float32))


def _summary_abstract(profile) -> str | None:
    """The profile summary, or an LLM-synthesized abstract when it has none.

    A failed synthesis falls back to the head of the expertise text.
    """
    text = profile.summary
    if text:
        return text
    # Synthesize via LLM
    from ..generative.llm import LLMClient

    joined = (profile.expertise or "")[:2000]
    prompt = (
        "Write a 1-2 paragraph research abstract summarizing the "
        "following researcher's expertise:\n\n" + joined
    )
    try:
        c = LLMClient()
        resp = c.complete(
            system=[{"type": "text", "text": "You summarize researcher expertise."}],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
        )
        return resp.text.strip()
    except _llm_errors():
        logger.warning(
            "LLM summary synthesis failed; embedding the expertise text instead",
            exc_info=True,
        )
        return (profile.expertise or "")[:500]


def _profile_embedding_impl(profile, kind: str = "centroid") -> np.ndarray:
    if kind not in ("centroid", "summary", "expertise"):
        raise ValueError(f"kind must be centroid|summary|expertise, got {kind!r}")

    index_dir = cache_dir(profile.require_directory("the profile embedding"))
    db_path = index_dir / "embeddings.sqlite"
    if kind == "centroid":
        return index_centroid(db_path, index_dir / "profile_vec.npz")

    meta = read_index_meta_path(db_path)
    backend_name = meta.get("backend_name", "")
    dim = int(meta.get("embedding_dim", "0") or 0)

    cache = _load_cache(profile)
    backend_match = _cache_matches_index(cache, backend_name, dim)
    if backend_match and kind in cache:
        return np.asarray(cache[kind], dtype=np.float32)

    # Recompute
    if not backend_match:
        cache = {"_backend_name": backend_name, "_dim": dim}

    if kind == "expertise":
        vec = _expertise_vec(profile, dim)
    elif kind == "summary":
        be = _resolve_backend(profile)
        abstract = _summary_abstract(profile)
        raw = be.embed([abstract or ""])[0]
        vec = _normalize(np.array(raw, dtype=np.float32))
        cache["_abstract"] = abstract or ""

    cache[kind] = vec
    _save_cache(profile, cache)
    return vec


def recompute_centroid(profile) -> np.ndarray:
    """Drop the cached centroid and recompute it from the current index.

    The feedback path for relevance marks: when an owner marks a paper
    relevant, the caller appends the work, rebuilds the embedding index,
    then calls this.
    Recompute-from-index is preferred over incremental nudging. It is
    deterministic (the same works always yield the same centroid) and needs
    no drift bookkeeping.
    """
    cache = _load_cache(profile)
    if "centroid" in cache:
        del cache["centroid"]
        _save_cache(profile, cache)
    return _profile_embedding_impl(profile, "centroid")
