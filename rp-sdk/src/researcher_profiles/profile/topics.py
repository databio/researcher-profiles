"""``prof.topics``: this profile's research topics.

Defines :class:`TopicManager`, the object ``ResearcherProfile.topics`` hands
back (``prof.topics.get(...)`` / ``prof.topics.relevance(...)``), together with
the functions behind it: clustering the embedding index into labeled topics
(cached, cluster, or LLM methods) and scoring a topic string's relevance
against a profile's centroid. Importing this module has no side effect on the
profile class.

Cluster-based topic extraction uses HDBSCAN if installed, else KMeans
from scikit-learn. Both are soft-deps; missing deps raise a helpful
error only when the caller actually requests clustering.
"""

import json
import logging
import re
import sqlite3
from pathlib import Path

import numpy as np

from ..embeddings._sqlite import read_chunk_matrix_with_meta, read_index_meta_path
from ..embeddings.profile_vec import _resolve_backend
from ..models.results import Topic, _llm_call_errors
from ..utils.clock import now_iso
from ..utils.paths import cache_dir

logger = logging.getLogger(__name__)


def _topics_cache_path(profile) -> Path:
    """The cluster-method memo: a precomputation, safe to delete."""
    return cache_dir(profile.require_directory("topics")) / "topics.json"


def _topics_content_path(profile) -> Path:
    """LLM-labeled topics: generated content, the same kind of thing as
    ``personality/expertise.md``. Rerunning gives different topics, so deleting
    this loses information."""
    return profile.require_directory("topics") / "personality" / "topics.json"


def _load_all_chunk_vectors(profile) -> tuple[np.ndarray, list[dict]]:
    db_path = cache_dir(profile.require_directory("topics")) / "embeddings.sqlite"
    meta = read_index_meta_path(db_path)
    dim = int(meta.get("embedding_dim", "0") or 0)
    return read_chunk_matrix_with_meta(db_path, dim)


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def _label_from_chunk(text: str) -> str:
    """Pull a short label out of a chunk's text (section or first sentence)."""
    text = (text or "").strip()
    if not text:
        return "topic"
    # Prefer a leading markdown heading
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and lines[0].startswith("#"):
        return lines[0].lstrip("#").strip()[:80]
    # First sentence
    m = re.split(r"(?<=[.!?])\s", text)
    return (m[0] if m else text)[:80]


def _topics_cluster(profile, n: int) -> list[Topic]:
    mat, metas = _load_all_chunk_vectors(profile)
    if len(metas) == 0:
        return []
    n_chunks = len(metas)
    n = max(1, min(n, n_chunks))

    labels: np.ndarray | None = None
    use_hdbscan = False
    if n_chunks >= 6:
        try:
            import hdbscan  # type: ignore

            use_hdbscan = True
        except ImportError:
            use_hdbscan = False

    if use_hdbscan:
        min_cluster_size = max(2, n_chunks // max(3 * n, 1))
        clusterer = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size, metric="euclidean")
        labels = clusterer.fit_predict(mat)
        # If everything came out -1 (noise), fall back to KMeans
        if (labels == -1).all():
            labels = None
            use_hdbscan = False

    if labels is None:
        try:
            from sklearn.cluster import KMeans
        except ImportError as e:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "topic clustering requires scikit-learn or hdbscan; "
                "requires the 'topics' extra, see the install instructions in the README"
            ) from e
        k = max(1, min(n, n_chunks))
        km = KMeans(n_clusters=k, n_init=10, random_state=0)
        labels = km.fit_predict(mat)

    topics: list[Topic] = []
    unique = sorted(set(int(lbl) for lbl in labels if lbl != -1))
    for cluster_id in unique:
        mask = labels == cluster_id
        members_idx = np.where(mask)[0]
        if len(members_idx) == 0:
            continue
        # Compute centroid and find closest member
        cluster_vecs = mat[members_idx]
        centroid = cluster_vecs.mean(axis=0)
        centroid /= max(float(np.linalg.norm(centroid)), 1e-12)
        sims = cluster_vecs @ centroid
        best_local = int(np.argmax(sims))
        best_idx = int(members_idx[best_local])
        label = _label_from_chunk(metas[best_idx]["text"])
        paper_ids = sorted(
            {
                metas[i]["source_id"]
                for i in members_idx
                if metas[i]["source_type"] == "paper_summary"
            }
        )
        chunk_ids = [int(metas[i]["id"]) for i in members_idx]
        weight = float(len(members_idx)) / float(n_chunks)
        topics.append(
            Topic(
                label=label,
                weight=round(weight, 4),
                paper_ids=paper_ids,
                chunk_ids=chunk_ids,
            )
        )
    topics.sort(key=lambda t: t.weight, reverse=True)
    return topics[:n]


def _persist_topics(profile, method: str, topics: list[Topic]) -> None:
    db_path = cache_dir(profile.require_directory("topics")) / "embeddings.sqlite"
    backend_name = ""
    try:
        meta = read_index_meta_path(db_path)
        backend_name = meta.get("backend_name", "")
    except (OSError, sqlite3.Error):
        pass
    payload = {
        "version": 1,
        "method": method,
        "computed_at": now_iso(),
        "backend_name": backend_name,
        "topics": [t.to_dict() for t in topics],
    }
    p = _topics_content_path(profile) if method == "llm" else _topics_cache_path(profile)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_topics(profile) -> list[Topic] | None:
    """Whatever topics are on disk: content first, then the cluster memo."""
    for p in (_topics_content_path(profile), _topics_cache_path(profile)):
        if p.exists():
            break
    else:
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if data.get("version") != 1:
        return None
    out: list[Topic] = []
    for d in data.get("topics", []) or []:
        out.append(
            Topic(
                label=d.get("label", ""),
                weight=float(d.get("weight", 0.0)),
                paper_ids=list(d.get("paper_ids", []) or []),
                chunk_ids=list(d.get("chunk_ids", []) or []),
            )
        )
    return out


def _topics_llm(profile, n: int) -> tuple[str, list[Topic]]:
    """LLM-based topic generation, as ``(method, topics)``.

    Falls back to clustering when the LLM is unavailable or unusable, and says
    so in the returned method, because the two results are persisted to
    different places: LLM topics are content, cluster topics are a cache.
    """
    try:
        from ..generative.llm import LLMClient
    except ImportError:  # pragma: no cover
        return "cluster", _topics_cluster(profile, n)
    mat, metas = _load_all_chunk_vectors(profile)
    if not metas:
        return "llm", []
    import random

    random.seed(0)
    sample = random.sample(metas, min(10, len(metas)))
    joined = "\n\n---\n\n".join((m["text"] or "")[:400] for m in sample)
    prompt = (
        f"Read these chunks of a researcher's writing and identify the top {n} "
        "research topics. Respond with strict JSON: a list of objects with keys "
        "'label' (short topic name) and 'weight' (float in [0,1]).\n\n"
        f"{joined}"
    )
    try:
        c = LLMClient()
        resp = c.complete(
            system=[{"type": "text", "text": "You extract concise research topics."}],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
        )
        text = resp.text.strip()
        # Try to find JSON list in text
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return "cluster", _topics_cluster(profile, n)
        data = json.loads(m.group(0))
    except (*_llm_call_errors(), json.JSONDecodeError) as e:
        # method="llm" quietly becomes method="cluster" here, and the caller is
        # told neither, so the downgrade has to be visible somewhere.
        logger.warning("LLM topic extraction failed, falling back to clustering: %s", e)
        return "cluster", _topics_cluster(profile, n)

    # Map back to chunks via cosine of label embedding vs chunk vectors
    be = _resolve_backend(profile)
    labels = [d.get("label", "") for d in data if d.get("label")]
    if not labels:
        return "llm", []
    label_vecs = np.array(be.embed(labels), dtype=np.float32)
    norms = np.linalg.norm(label_vecs, axis=1, keepdims=True)
    norms[norms < 1e-12] = 1.0
    label_vecs = label_vecs / norms
    sims = label_vecs @ mat.T  # (n_topics, n_chunks)
    out: list[Topic] = []
    for i, d in enumerate(data[: len(labels)]):
        # take top 3 chunks
        top_idx = np.argsort(-sims[i])[:5]
        paper_ids = sorted(
            {
                metas[int(j)]["source_id"]
                for j in top_idx
                if metas[int(j)]["source_type"] == "paper_summary"
            }
        )
        chunk_ids = [int(metas[int(j)]["id"]) for j in top_idx]
        out.append(
            Topic(
                label=str(d.get("label", "")),
                weight=float(d.get("weight", 0.0)),
                paper_ids=paper_ids,
                chunk_ids=chunk_ids,
            )
        )
    return "llm", out[:n]


def _topics_impl(profile, n: int = 10, method: str = "cached") -> list[Topic]:
    if method == "cached":
        cached = _load_topics(profile)
        if cached:
            return cached[:n]
        topics = _topics_cluster(profile, n)
        _persist_topics(profile, "cluster", topics)
        return topics
    if method == "cluster":
        topics = _topics_cluster(profile, n)
        _persist_topics(profile, "cluster", topics)
        return topics
    if method == "llm":
        used, topics = _topics_llm(profile, n)
        _persist_topics(profile, used, topics)
        return topics
    raise ValueError(f"unknown method: {method!r}")


def _relevance_impl(profile, topic: str) -> float:
    be = _resolve_backend(profile)
    vec = np.array(be.embed([topic])[0], dtype=np.float32)
    n = float(np.linalg.norm(vec))
    if n < 1e-12:
        return 0.0
    vec = vec / n
    # centroid
    cvec = profile.index.embedding("centroid")
    cn = float(np.linalg.norm(cvec))
    if cn < 1e-12:
        return 0.0
    cvec = cvec / cn
    return float(vec @ cvec)


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class TopicManager:
    """``prof.topics``: this profile's research topics.

    ``get()`` is the primary read, matching the other four managers; topic
    extraction clusters the embedding index, so it needs the same directory
    :class:`~researcher_profiles.profile.index.IndexManager` does.
    """

    def __init__(self, profile):
        self._profile = profile

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TopicManager(slug={self._profile.slug!r})"

    def get(self, n: int = 10, method: str = "cached") -> list[Topic]:
        """The top ``n`` topics; ``method`` is ``cached`` / ``cluster`` / ``llm``."""
        return _topics_impl(self._profile, n=n, method=method)

    def relevance(self, topic: str) -> float:
        """Cosine similarity between ``topic`` and this profile's centroid."""
        return _relevance_impl(self._profile, topic)


__all__ = ["TopicManager"]
