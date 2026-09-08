"""Per-profile score calibration against a fixed background query set.

Used by :class:`~researcher_profiles.analytics.match.MatchManager`
(``store.match``) to z-score raw match scores so that absolute rankings are
comparable across profiles that have very different vector-space
neighborhoods.
"""

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

from ..embeddings import IndexNotBuiltError, MissingEmbeddingBackendError
from ..embeddings.profile_vec import _resolve_backend
from ..errors import CapabilityUnavailableError
from ..utils.clock import now_iso
from ..utils.paths import cache_dir

logger = logging.getLogger(__name__)

BACKGROUND_QUERIES: tuple[str, ...] = (
    "machine learning for biology",
    "single-cell genomics and transcriptomics",
    "regulatory networks and gene expression",
    "computational tools for proteomics",
    "epigenetics and chromatin biology",
    "natural language processing for science",
    "education, mentorship and outreach",
    "image analysis and computer vision",
    "clinical informatics and electronic health records",
    "structural biology and protein modeling",
    "metadata standards and data interoperability",
    "statistical genetics and population genomics",
)

SCORER_VERSION = 1


def _cal_path(profile) -> Path:
    return cache_dir(profile.require_directory("calibration")) / "calibration.json"


def _is_finite(x: float) -> bool:
    import math

    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _score_for_query(profile, text: str) -> float:
    """Same scoring function the registry uses: centroid + mean top-3 chunks."""
    be = _resolve_backend(profile)
    qvec = np.array(be.embed([text])[0], dtype=np.float32)
    n = float(np.linalg.norm(qvec))
    if n < 1e-12:
        return 0.0
    qvec = qvec / n
    centroid = profile.index.embedding("centroid")
    cn = float(np.linalg.norm(centroid))
    if cn < 1e-12:
        centroid_score = 0.0
    else:
        centroid_score = float(qvec @ (centroid / cn))
    # Top-3 chunks via profile.search
    chunk_score = 0.0
    try:
        hits = profile.index.search(text, k=3)
        if hits:
            chunk_score = float(np.mean([h.score for h in hits]))
    except (IndexNotBuiltError, CapabilityUnavailableError):
        chunk_score = 0.0
    raw = 0.5 * centroid_score + 0.5 * chunk_score
    return raw if _is_finite(raw) else 0.0


def compute_calibration(profile) -> dict[str, Any]:
    import sqlite3

    scores = []
    for q in BACKGROUND_QUERIES:
        try:
            scores.append(_score_for_query(profile, q))
        except IndexNotBuiltError:
            raise
        except (MissingEmbeddingBackendError, ValueError) as e:
            # A zero here is not neutral: it drags the mean and the standard
            # deviation every /match score is normalized against.
            logger.warning("calibration query %r scored 0.0: %s", q, e)
            scores.append(0.0)
    arr = np.array(scores, dtype=np.float64)
    backend_name = ""
    try:
        meta_path = cache_dir(profile.require_directory("calibration")) / "embeddings.sqlite"
        conn = sqlite3.connect(str(meta_path))
        try:
            rows = conn.execute("SELECT value FROM index_meta WHERE key='backend_name'").fetchall()
            if rows:
                backend_name = rows[0][0]
        finally:
            conn.close()
    except sqlite3.Error:
        pass

    payload = {
        "mean": float(arr.mean()),
        "std": float(arr.std()),
        "queries": list(BACKGROUND_QUERIES),
        "scores": [float(s) for s in scores],
        "computed_at": now_iso(),
        "backend_name": backend_name,
        "scorer_version": SCORER_VERSION,
    }
    p = _cal_path(profile)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_calibration(profile) -> dict[str, Any] | None:
    p = _cal_path(profile)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("scorer_version") != SCORER_VERSION:
            return None
        return data
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def ensure_calibration(profile) -> dict[str, Any]:
    cal = load_calibration(profile)
    if cal is not None:
        return cal
    return compute_calibration(profile)


def normalize_score(raw: float, cal: dict[str, Any]) -> float:
    """Convert raw score to a bounded normalized score using cal stats."""
    import math

    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return 0.0
    mean = float(cal.get("mean", 0.0))
    std = float(cal.get("std", 0.0))
    if math.isnan(mean) or math.isnan(std):
        return 0.5
    z = (raw - mean) / max(std, 1e-6)
    if math.isnan(z) or math.isinf(z):
        return 0.5 if z != z else (1.0 if z > 0 else 0.0)
    return 0.5 * (1.0 + math.tanh(z / 2.0))
