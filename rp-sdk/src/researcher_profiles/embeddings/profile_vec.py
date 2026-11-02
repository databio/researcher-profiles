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
from ._sqlite import IndexNotBuiltError, read_chunk_matrix_with_meta, read_index_meta_path
from .backends import get_backend

logger = logging.getLogger(__name__)

_KIND = Literal["centroid", "summary", "expertise"]


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n < 1e-12:
        return v.astype(np.float32)
    return (v / n).astype(np.float32)


#: An authored paper counts as this many interest seeds in the centroid blend:
#: an authored paper is evidence of sustained work, a seed is one paste or click.
AUTHORED_PAPER_WEIGHT = 3

#: Whenever the person has any authored paper, seeds never exceed this share of
#: the centroid direction, so authored work always dominates.
SEED_SHARE_CAP = 0.4

#: Bumped whenever the centroid rule changes, so cached centroids recompute.
CENTROID_RULE = "blend-v1"

#: Chunk source types that count as an interest seed in :func:`blend_centroid`.
SEED_SOURCE_TYPES = frozenset({"seed_abstract"})


def seed_share(n_auth: int, n_seed: int, *, has_authored_chunks: bool) -> float:
    """The weight seeds get in the centroid, in ``[0, 1]``.

    ``n_auth`` counts distinct authored papers (``PAPER_CHUNK_TYPES`` source
    ids), ``n_seed`` distinct seeds. ``has_authored_chunks`` is whether any
    non-seed chunk exists at all (papers, or a narrative with no papers).

    Worked examples:

    ===========================  ==========================
    authored, seeds              share
    ===========================  ==========================
    (no authored chunks, 5)      1.0 (pure-seed profile)
    (narrative only, 10)         10 / 13 = 0.77 (no cap)
    (2 papers, 10)               min(10 / 16, 0.4) = 0.4
    (20 papers, 5)               5 / 65 = 0.077
    (100 papers, 10)             10 / 310 = 0.032
    ===========================  ==========================
    """
    if n_seed <= 0:
        return 0.0
    if not has_authored_chunks:
        return 1.0
    share = n_seed / (n_seed + AUTHORED_PAPER_WEIGHT * max(n_auth, 1))
    if n_auth > 0:
        share = min(share, SEED_SHARE_CAP)
    return share


def blend_centroid(vectors: np.ndarray, metas: list[dict]) -> np.ndarray:
    """The one centroid rule: authored chunks dominate, seeds nudge.

    ``metas`` carries ``source_type`` and ``source_id`` per row of ``vectors``.
    ``A`` is the mean of every non-seed chunk vector; ``S`` the mean over seeds
    of each seed's own mean (one vote per seed, however long its abstract).
    With no seed chunks the result is the plain normalized mean, the rule
    before seeds existed. Otherwise it is ``unit((1 - w) * unit(A) + w * unit(S))``
    with ``w`` from :func:`seed_share`. Blending the two unit means stops a
    chunk-heavy side from winning by chunk count alone.
    """
    from .chunking import PAPER_CHUNK_TYPES

    vecs = np.asarray(vectors, dtype=np.float32)
    if vecs.ndim != 2 or vecs.shape[0] == 0:
        raise ValueError("blend_centroid needs at least one vector")
    seed_rows: dict[str, list[int]] = {}
    auth_rows: list[int] = []
    auth_papers: set[str] = set()
    for i, m in enumerate(metas):
        st = m.get("source_type")
        sid = str(m.get("source_id"))
        if st in SEED_SOURCE_TYPES:
            seed_rows.setdefault(sid, []).append(i)
        else:
            auth_rows.append(i)
            if st in PAPER_CHUNK_TYPES:
                auth_papers.add(sid)
    if not seed_rows:
        return _normalize(vecs.mean(axis=0))
    seed_mean = np.stack([vecs[rows].mean(axis=0) for rows in seed_rows.values()]).mean(axis=0)
    share = seed_share(len(auth_papers), len(seed_rows), has_authored_chunks=bool(auth_rows))
    if share >= 1.0:
        return _normalize(seed_mean)
    auth_unit = _normalize(vecs[auth_rows].mean(axis=0))
    return _normalize((1.0 - share) * auth_unit + share * _normalize(seed_mean))


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
    """Whether the cached vectors were built by this backend at this dim and centroid rule."""
    cached_backend = str(_scalar(cache["_backend_name"])) if "_backend_name" in cache else ""
    cached_dim = 0
    if "_dim" in cache:
        try:
            cached_dim = int(_scalar(cache["_dim"]))
        except (TypeError, ValueError):
            cached_dim = 0
    cached_rule = str(_scalar(cache["_centroid_rule"])) if "_centroid_rule" in cache else ""
    return cached_backend == backend_name and cached_dim == dim and cached_rule == CENTROID_RULE


def _fresh_cache(backend_name: str, dim: int) -> dict[str, Any]:
    return {"_backend_name": backend_name, "_dim": dim, "_centroid_rule": CENTROID_RULE}


def _centroid_vec(db_path: Path, dim: int) -> np.ndarray:
    """The index's centroid under :func:`blend_centroid`, over every chunk."""
    mat, metas = read_chunk_matrix_with_meta(db_path, dim)
    if mat.shape[0] == 0:
        raise IndexNotBuiltError(f"Index {db_path} has no vectors")
    return blend_centroid(mat, metas)


def index_centroid(db_path: Path, cache_path: Path) -> np.ndarray:
    """One sqlite index's centroid, read from (or written to) its npz cache.

    The single centroid implementation, shared by the index and the profile.
    """
    meta = read_index_meta_path(db_path)
    backend_name = meta.get("backend_name", "")
    dim = int(meta.get("embedding_dim", "0") or 0)

    cache = _load_cache_at(cache_path)
    if _cache_matches_index(cache, backend_name, dim):
        if "centroid" in cache:
            return np.asarray(cache["centroid"], dtype=np.float32)
    else:
        cache = _fresh_cache(backend_name, dim)

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

    if not backend_match:
        cache = _fresh_cache(backend_name, dim)

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

    Call after rebuilding the index. Recomputing (rather than nudging the old
    vector) keeps the result deterministic.
    """
    cache = _load_cache(profile)
    if "centroid" in cache:
        del cache["centroid"]
        _save_cache(profile, cache)
    return _profile_embedding_impl(profile, "centroid")
