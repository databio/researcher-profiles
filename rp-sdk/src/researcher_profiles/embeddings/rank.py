"""Candidate works ranked against one profile, the mirror of ``match.rank``.

``store.match.rank`` ranks profiles against a free-text query;
:func:`rank_works_against_profile` ranks candidate works (e.g. new OpenAlex
results from ``openalex.fetch_new_works``) against one profile's embedding
vector. Both diversify through the shared :func:`mmr_indices` helper.
"""

import json
import logging
import math

import numpy as np

from ..models.results import RankedWork
from ._sqlite import IndexNotBuiltError
from .backends import MissingEmbeddingBackendError
from .profile_vec import _resolve_backend

logger = logging.getLogger(__name__)

#: Cosine floor for a profile topic label to count as evidence for a work.
_EVIDENCE_SIM = 0.5


def mmr_indices(
    scores: list[float],
    vectors: np.ndarray | list | None,
    k: int,
    lambda_: float = 0.5,
) -> list[int]:
    """Greedy maximal-marginal-relevance selection over candidate indices.

    ``scores`` are the relevance scores; ``vectors`` the candidates' unit
    vectors, aligned by index (a ``None`` entry, or ``vectors=None``, makes
    that candidate contribute zero similarity, i.e. pure relevance). Returns
    the chosen indices, best-first, at most ``k`` of them.
    """
    n = len(scores)
    if n == 0:
        return []

    def _vec(i: int):
        if vectors is None:
            return None
        v = vectors[i]
        return None if v is None else np.asarray(v, dtype=np.float32)

    chosen: list[int] = []
    remaining = list(range(n))
    first = max(remaining, key=lambda i: scores[i])
    chosen.append(first)
    remaining.remove(first)
    while remaining and len(chosen) < k:
        best_i = None
        best_v = -math.inf
        for i in remaining:
            vi = _vec(i)
            if vi is None:
                sim_max = 0.0
            else:
                sims = [float(vi @ _vec(c)) for c in chosen if _vec(c) is not None]
                sim_max = max(sims) if sims else 0.0
            mmr = lambda_ * scores[i] - (1 - lambda_) * sim_max
            if mmr > best_v:
                best_v = mmr
                best_i = i
        if best_i is None:  # pragma: no cover - defensive
            break
        chosen.append(best_i)
        remaining.remove(best_i)
    return chosen


def _work_text(work) -> str:
    title = getattr(work, "title", "") or ""
    abstract = getattr(work, "abstract", "") or ""
    return f"{title}\n\n{abstract}".strip()


def _topic_evidence(profile, backend, work_vecs: np.ndarray) -> list[list[str]]:
    """Overlapping profile topic labels per work; fail-soft to empty lists."""
    empty: list[list[str]] = [[] for _ in range(len(work_vecs))]
    try:
        topics = profile.topics.get(method="cached")
    except (IndexNotBuiltError, OSError, json.JSONDecodeError):
        return empty
    labels = [t.label for t in topics if t.label]
    if not labels:
        return empty
    try:
        lvecs = np.array(backend.embed(labels), dtype=np.float32)
    except (MissingEmbeddingBackendError, RuntimeError):
        return empty
    norms = np.linalg.norm(lvecs, axis=1, keepdims=True)
    norms[norms < 1e-12] = 1.0
    lvecs = lvecs / norms
    sims = work_vecs @ lvecs.T  # (n_works, n_labels)
    return [
        [labels[j] for j in range(len(labels)) if float(sims[i, j]) >= _EVIDENCE_SIM]
        for i in range(len(work_vecs))
    ]


def rank_works_against_profile(
    profile,
    works: list,
    *,
    k: int = 10,
    backend=None,
    kind: str = "centroid",
    diversify: bool = True,
    lambda_: float = 0.5,
    threshold: float | None = None,
) -> list[RankedWork]:
    """Rank candidate works against one profile's embedding vector.

    ``works`` are ``PaperRecord``-like objects (title + optional abstract).
    The profile side is ``profile.index.embedding(kind)``: ``centroid`` by
    default, ``summary``/``expertise`` as alternatives. Each candidate's
    title+abstract is embedded with the same backend that built the profile's
    index (or an explicit ``backend``), cosine-scored, optionally filtered by
    ``threshold``, MMR-diversified, and returned best-first as
    :class:`~researcher_profiles.models.results.RankedWork` with overlapping-topic
    evidence.
    """
    if not works:
        return []
    pvec = np.asarray(profile.index.embedding(kind), dtype=np.float32)
    pn = float(np.linalg.norm(pvec))
    if pn < 1e-12:
        return []
    pvec = pvec / pn

    be = backend if backend is not None else _resolve_backend(profile)
    mat = np.array(be.embed([_work_text(w) for w in works]), dtype=np.float32)
    mat = np.nan_to_num(mat, nan=0.0, posinf=0.0, neginf=0.0)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms < 1e-12] = 1.0
    mat = mat / norms
    scores = mat @ pvec  # (n,)

    evidence = _topic_evidence(profile, be, mat)

    keep = list(range(len(works)))
    if threshold is not None:
        keep = [i for i in keep if float(scores[i]) >= threshold]
    keep.sort(key=lambda i: -float(scores[i]))

    if diversify and len(keep) > k:
        kept_scores = [float(scores[i]) for i in keep]
        kept_vecs = mat[keep]
        chosen = mmr_indices(kept_scores, kept_vecs, k=k, lambda_=lambda_)
        keep = [keep[i] for i in chosen]
    else:
        keep = keep[:k]

    return [RankedWork(work=works[i], score=float(scores[i]), evidence=evidence[i]) for i in keep]


__all__ = ["mmr_indices", "rank_works_against_profile"]
