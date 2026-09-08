"""Cross-profile ranking: query -> ordered profiles, with evidence.

The pipeline is centroid prefilter, per-profile chunk re-rank, calibration,
then optional MMR diversification. Topic indexing and clustering live here too
because both are ways of asking "which profiles go together", and both read the
same centroid matrix ranking does.

Reached as ``store.match``.
"""

import json
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

from ..embeddings._sqlite import IndexNotBuiltError
from ..embeddings.backends import MissingEmbeddingBackendError
from ..embeddings.chunking import PAPER_CHUNK_TYPES
from ..errors import CapabilityUnavailableError
from ..generative.calibration import ensure_calibration, normalize_score
from ..models.results import Match, MatchEvidence
from ..profile import ResearcherProfile
from ..store._analytics import require_vector_store
from ..utils.clock import now_iso

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..store.protocol import VectorStore
    from .roster import _Roster, _RosterCache

logger = logging.getLogger(__name__)


class MatchManager:
    """``store.match``: ranking, diversification, topics, clustering."""

    def __init__(self, store: "VectorStore", rostered: "_RosterCache") -> None:
        self._store = store
        self._rostered = rostered

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"MatchManager(store={self._store.location!r})"

    @property
    def topics_cache_path(self) -> Optional[Path]:
        """``<root>/.cache/topics.json``, or ``None`` without a root.

        A rendered view of :meth:`topics`, written for shell callers. The
        in-memory index is built fresh either way, so a store with no directory
        loses only the file.
        """
        from ..utils.paths import store_cache_dir

        d = store_cache_dir(self._store.root)
        return None if d is None else d / "topics.json"

    def invalidate(self) -> None:
        """Unlink the topics view."""
        tp = self.topics_cache_path
        if tp is not None and tp.exists():
            try:
                tp.unlink()
            except OSError:
                logger.debug("could not unlink topics cache at %s", tp, exc_info=True)

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    def rank(
        self,
        text: str,
        k: int = 5,
        *,
        prefilter: int = 10,
        normalize: bool = True,
        require_topics: list[str] | None = None,
        diversify: bool = True,
        lambda_: float = 0.5,
        topk_chunks: int = 5,
    ) -> list[Match]:
        """Rank every profile in the store against a free-text query."""
        # Up front, before any work: ranking is the capability, and a store
        # that cannot serve vectors should say so in one error rather than
        # produce an empty ranking that reads like "nobody matched".
        require_vector_store(self._store)
        if not self._rostered().profiles:
            return []
        candidate_slugs = set(self._rostered().slugs)
        if require_topics:
            candidate_slugs &= self._topic_allowed_slugs(require_topics)
            if not candidate_slugs:
                return []

        vectors = self._store.centroids
        qvec = vectors.embed_query(text)
        # The roster and the matrix together, so row i is still slugs[i] even
        # if a write lands between the two reads.
        roster, matrix = vectors.snapshot()
        sims = matrix @ qvec  # cosine to all, (n,)
        slugs = roster.slugs

        # One memo per rank() call. Topic labels repeat heavily across
        # profiles, so embedding each one once turns prefilter*topics query
        # embeddings into the handful of distinct labels the survivors share.
        label_vecs: dict[str, "np.ndarray"] = {}

        matches = [
            self._match_for(
                roster.by_slug[slugs[i]],
                text,
                qvec,
                float(sims[i]),
                label_vecs=label_vecs,
                topk_chunks=topk_chunks,
                normalize=normalize,
            )
            for i in self._survivors(sims, roster, candidate_slugs, prefilter=prefilter, k=k)
        ]

        matches.sort(key=lambda m: -m.score)
        if diversify and len(matches) > k:
            matches = self.diversify(matches, k=k, lambda_=lambda_)
        return matches[:k]

    def _topic_allowed_slugs(self, require_topics: list[str]) -> set[str]:
        """The slugs claiming at least one of ``require_topics``."""
        idx = self.topics()
        allowed: set[str] = set()
        for t in require_topics:
            for p in idx.get(t.lower(), []):
                allowed.add(p.slug)
        return allowed

    def _survivors(
        self,
        sims,
        roster: "_Roster",
        candidate_slugs: set[str],
        *,
        prefilter: int,
        k: int,
    ) -> list[int]:
        """Matrix row indices that pass the filter, best-scoring first, capped.

        A set membership test, not a scan per slug: this is the one step in
        rank() that would otherwise grow quadratically with the corpus.
        """
        survivor_idx = [i for i, s in enumerate(roster.slugs) if s in candidate_slugs]
        survivor_idx.sort(key=lambda i: -float(sims[i]))
        return survivor_idx[: max(prefilter, k)]

    def _overlapping_topics(
        self,
        prof: ResearcherProfile,
        qvec,
        label_vecs: dict[str, "np.ndarray"],
        *,
        threshold: float = 0.5,
    ) -> list[str]:
        """The profile's cached topic labels close enough to the query to be evidence.

        ``label_vecs`` is the caller's memo of label -> unit vector, shared
        across every profile in one rank() call. It is passed in rather than
        held on the manager so it lives exactly as long as the query does.
        """
        try:
            topics = prof.topics.get(method="cached")
            overlap = []
            for t in topics:
                if not t.label:
                    continue
                vec = label_vecs.get(t.label)
                if vec is None:
                    vec = self._store.centroids.embed_query(t.label)
                    label_vecs[t.label] = vec
                if float(vec @ qvec) >= threshold:
                    overlap.append(t.label)
            return overlap
        except (
            IndexNotBuiltError,
            OSError,
            json.JSONDecodeError,
            MissingEmbeddingBackendError,
            CapabilityUnavailableError,
        ):
            # CapabilityUnavailableError: ``prof.topics`` reads the profile's
            # ``.cache/topics.json``, which a remote profile does not have.
            # Topic overlap is only supporting evidence for the score.
            return []

    def _match_for(
        self,
        prof: ResearcherProfile,
        text: str,
        qvec,
        centroid_score: float,
        *,
        label_vecs: dict[str, "np.ndarray"],
        topk_chunks: int,
        normalize: bool,
    ) -> Match:
        """Score one surviving profile and gather the evidence behind that score."""
        try:
            hits = self._store.vector_index(prof.slug).search(text, k=topk_chunks)
        except (IndexNotBuiltError, MissingEmbeddingBackendError):
            # A profile with no vectors contributes no chunk evidence and keeps
            # its centroid score. Not a capability failure: the store proved it
            # serves vectors before ranking started (``require_vector_store``).
            hits = []
        mean_chunk_score = float(np.mean([h.score for h in hits])) if hits else 0.0
        raw = 0.5 * centroid_score + 0.5 * mean_chunk_score

        top_papers = []
        seen = set()
        for h in hits:
            if h.source_type in PAPER_CHUNK_TYPES and h.source_id not in seen:
                seen.add(h.source_id)
                top_papers.append(h.source_id)

        evidence = MatchEvidence(
            top_chunks=hits,
            top_papers=top_papers,
            overlapping_topics=self._overlapping_topics(prof, qvec, label_vecs),
            centroid_score=centroid_score,
        )
        score = self._normalized_score(prof, raw) if normalize else raw
        return Match(profile=prof, score=score, evidence=evidence)

    def _normalized_score(self, prof: ResearcherProfile, raw: float) -> float:
        """``raw`` mapped through the profile's calibration, or ``raw`` if that fails."""
        try:
            return normalize_score(raw, ensure_calibration(prof))
        # Boundary: every failure mode of an on-disk calibration artifact, from
        # any of the readers behind ensure_calibration.
        except Exception:
            # Fail soft (a broken calibration must not 500 /match) but never
            # silently: an uncalibrated raw score ranked against normalized
            # peers is a corrupt ordering, not a degraded one.
            logger.warning(
                "calibration failed for %s; falling back to an UNCALIBRATED "
                "raw score (this profile is not comparable to normalized peers)",
                prof.slug,
                exc_info=True,
            )
            return raw

    # ------------------------------------------------------------------
    # Diversification
    # ------------------------------------------------------------------

    def diversify(
        self,
        matches: list[Match],
        k: int = 5,
        lambda_: float = 0.5,
    ) -> list[Match]:
        """Re-order by maximal marginal relevance: relevant but not redundant."""
        if not matches:
            return []
        # Lazy: the MMR helper pulls numpy-adjacent machinery the plain
        # ranking path must not pay for. See
        # tests/test_guardrails.py::test_match_path_does_not_pull_sklearn.
        from ..embeddings.rank import mmr_indices

        roster, centroids = self._store.centroids.snapshot()
        slug_to_idx = {s: i for i, s in enumerate(roster.slugs)}
        vectors = [
            centroids[slug_to_idx[m.profile.slug]] if m.profile.slug in slug_to_idx else None
            for m in matches
        ]
        chosen = mmr_indices([m.score for m in matches], vectors, k=k, lambda_=lambda_)
        return [matches[i] for i in chosen]

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------

    def topics(self) -> dict[str, list[ResearcherProfile]]:
        """Topic label -> the profiles claiming it. Also written to disk."""
        cache_path = self.topics_cache_path
        # Build fresh in-memory; we still write a JSON view to disk.
        out: dict[str, list[ResearcherProfile]] = {}
        for p in self._rostered().profiles:
            try:
                topics = p.topics.get(method="cached")
            except (
                IndexNotBuiltError,
                OSError,
                json.JSONDecodeError,
                CapabilityUnavailableError,
            ):
                topics = []
            for t in topics:
                key = (t.label or "").lower().strip()
                if not key:
                    continue
                out.setdefault(key, []).append(p)
        if cache_path is None:
            return out
        try:
            payload = {
                "version": 1,
                "computed_at": now_iso(),
                "merge_threshold": 0.85,
                "index": {k: [p.slug for p in v] for k, v in out.items()},
            }
            cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass
        return out

    # ------------------------------------------------------------------
    # Cluster
    # ------------------------------------------------------------------

    def cluster(self, k: int | None = None) -> dict[str, list[ResearcherProfile]]:
        """Group profiles by centroid; each group is named for its exemplar."""
        if not self._rostered().profiles:
            return {}
        roster, centroids = self._store.centroids.snapshot()
        profiles = roster.profiles
        if not profiles:
            return {}
        if k is None:
            k = max(2, int(math.sqrt(len(profiles))))
        k = max(1, min(k, len(profiles)))
        # Lazy: scikit-learn is a heavy optional extra and the ranking path
        # must stay free of it.
        try:
            from sklearn.cluster import KMeans
        except ImportError as e:
            raise RuntimeError(
                "clustering requires scikit-learn; requires the 'topics' extra, "
                "see the install instructions in the README"
            ) from e
        if len(centroids) == 0:
            return {}
        km = KMeans(n_clusters=k, n_init=10, random_state=0)
        labels = km.fit_predict(centroids)
        groups: dict[int, list[int]] = {}
        for i, lbl in enumerate(labels):
            groups.setdefault(int(lbl), []).append(i)
        out: dict[str, list[ResearcherProfile]] = {}
        cluster_centers = km.cluster_centers_
        for lbl, members in groups.items():
            center = cluster_centers[lbl]
            center_n = max(float(np.linalg.norm(center)), 1e-12)
            center_unit = center / center_n
            sims = [float(centroids[i] @ center_unit) for i in members]
            best_local = int(np.argmax(sims))
            name = f"like-{profiles[members[best_local]].slug}"
            out[name] = [profiles[i] for i in members]
        return out


__all__ = ["MatchManager"]
