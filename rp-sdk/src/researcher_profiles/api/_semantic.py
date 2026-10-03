"""Search inside one profile: stored vectors, a query encoder, BM25, and RRF.

The shared code behind three routes: paper search (``GET /profiles/{slug}/papers?q=``),
passages (``POST .../passages``), and ``POST /profiles/{slug}/search``. Every
function takes one profile and never touches another, ``/match``, or the
centroid matrix.

**Where the vectors live.** A store that serves vectors (the ``VectorStore``
capability) hands out one profile's chunk index. On the SQL store those rows
are the *public* subset and carry no text; on a directory store the
build-local sqlite carries every chunk. Either way a hit is a key
``(source_type, source_id, chunk_index)``: text is recovered by re-running the
deterministic chunkers over the sources this caller may read
(:func:`chunk_texts`), and a chunk whose length moved since indexing is stale
and dropped.

**Hybrid.** Semantic and keyword rankings are merged with reciprocal-rank
fusion. Whenever semantic search cannot run (no vectors, no encoder, a
timeout, a different embedding model), callers fall back to keyword search
and say so in a fixed-text note. A note never carries source or user text.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import weakref
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Iterable, Mapping, Optional

from ..privacy import ViewerTier, chunk_source_tiers, explain_tiers, tier_allows
from ._projection import artifact_visible

logger = logging.getLogger(__name__)

#: A semantic hit below this cosine is not kept: MiniLM puts unrelated
#: scientific text around 0.1-0.25. Tune with the eval set.
MIN_COSINE = 0.30
#: The reciprocal-rank-fusion constant.
RRF_K = 60
#: How long one query embedding may take before the caller falls back.
EMBED_TIMEOUT_S = 3.0

_PAPER_TYPES = frozenset({"paper_summary", "paper_abstract"})

# Fixed note texts. Never interpolate source or user text into these.
NOTE_NO_VECTORS = "Semantic search unavailable (no stored vectors for this profile); keyword only."
NOTE_NO_ENCODER = "Semantic search unavailable (query encoder not available); keyword only."
NOTE_TIMEOUT = "Semantic search unavailable (query encoder timed out); keyword only."
NOTE_MISMATCH = (
    "Semantic search unavailable (stored vectors come from a different embedding model);"
    " keyword only."
)


# ---------------------------------------------------------------------------
# Keyword ranking
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"\w+")


def tokens(text: str) -> list[str]:
    """Lowercase ``\\w+`` tokens. No stemming, so gene symbols match exactly."""
    return _TOKEN.findall((text or "").lower())


class BM25:
    """Okapi BM25 over a small in-memory corpus (``k1=1.2, b=0.75``)."""

    def __init__(self, docs: Iterable[str], *, k1: float = 1.2, b: float = 0.75):
        self._docs = [Counter(tokens(d)) for d in docs]
        self._lens = [sum(c.values()) for c in self._docs]
        self._avg = (sum(self._lens) / len(self._lens)) if self._lens else 0.0
        df: Counter = Counter()
        for c in self._docs:
            df.update(c.keys())
        n = len(self._docs)
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query: str) -> list[float]:
        """One score per document; 0.0 means no query term matched it."""
        terms = set(tokens(query))
        out: list[float] = []
        for c, n in zip(self._docs, self._lens):
            s = 0.0
            for t in terms:
                f = c.get(t)
                if not f:
                    continue
                norm = 1 - self.b + self.b * (n / self._avg if self._avg else 0.0)
                s += self._idf[t] * f * (self.k1 + 1) / (f + self.k1 * norm)
            out.append(s)
        return out

    def ranking(self, query: str, ids: list) -> list:
        """``ids`` of the documents any query term matched, best first."""
        scored = [(s, i) for i, s in enumerate(self.scores(query)) if s > 0]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [ids[i] for _, i in scored]


def rrf(*rankings: list) -> dict:
    """Reciprocal-rank fusion: ``score(id) = sum over rankings of 1 / (RRF_K + rank)``.

    Ranks start at 1. An id missing from a ranking contributes nothing there.
    """
    out: dict = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, 1):
            out[key] = out.get(key, 0.0) + 1.0 / (RRF_K + rank)
    return out


def fuse(semantic: list, keyword: list) -> list[tuple[Any, float, list[str]]]:
    """``[(id, rrf_score, matched_by)]`` best first, over ids either side kept."""
    scores = rrf(semantic, keyword)
    sem, kw = set(semantic), set(keyword)
    out = []
    for key, score in scores.items():
        by = sorted(({"semantic"} if key in sem else set()) | ({"keyword"} if key in kw else set()))
        out.append((key, round(score, 6), by))
    order = {k: i for i, k in enumerate([*semantic, *keyword])}
    out.sort(key=lambda x: (-x[1], order.get(x[0], 0)))
    return out


# ---------------------------------------------------------------------------
# The profile's stored vectors
# ---------------------------------------------------------------------------

_VECTOR_CACHE_MAX = 64
_vector_cache: "weakref.WeakKeyDictionary[Any, OrderedDict]" = weakref.WeakKeyDictionary()
_vector_lock = threading.Lock()


def _flat_from_sqlite(index) -> Any:
    """A directory store's sqlite index as a searchable ``FlatEmbeddingIndex``.

    The sqlite index has no vector-level search, and the rest of this module
    works on one matrix, so its rows are read once into the flat form. All
    rows are kept, private ones included; hits are tier-filtered per caller.
    """
    import numpy as np

    from ..embeddings._sqlite import deserialize_vec
    from ..embeddings.flat import FlatEmbeddingIndex, _read_rows

    rows, dim, backend_spec = _read_rows(index.db_path)
    if not rows or dim <= 0:
        return None
    vectors = np.asarray([deserialize_vec(bytes(r[5])) for r in rows], dtype=np.float32)
    chunks = [
        {
            "source_type": r[0],
            "source_id": r[1],
            "chunk_index": r[2],
            "section": r[3],
            "char_count": r[4],
        }
        for r in rows
    ]
    meta = {
        "backend_spec": backend_spec,
        "dim": dim,
        "count": len(rows),
        "normalized": False,
        "metric": "cosine",
        "rows": list(range(len(rows))),
        "file": "sqlite",
    }
    return FlatEmbeddingIndex(meta, vectors, chunks)


def profile_vectors(store, ref: str):
    """The profile's chunk index from the store, or ``None``.

    ``None`` when the store cannot serve vectors or holds none for this
    profile. LRU-cached per store, keyed by ``(ref, store write generation)``
    so any write drops the entry; at most 64 profiles per store.
    """
    if not hasattr(store, "vector_index"):
        return None
    key = (ref, getattr(store, "generation", None))
    with _vector_lock:
        try:
            per_store = _vector_cache.setdefault(store, OrderedDict())
        except TypeError:  # pragma: no cover - a store that cannot be weakly referenced
            per_store = OrderedDict()
        if key in per_store:
            per_store.move_to_end(key)
            return per_store[key]
    try:
        index = store.vector_index(ref)
        if not hasattr(index, "search_vector"):
            index = _flat_from_sqlite(index)
    # Boundary: no index, a corrupt blob, or a store without the vectors extra
    # all mean "no semantic search here", never a failed read.
    except Exception:
        logger.debug("no vectors for %r", ref, exc_info=True)
        index = None
    if index is not None and getattr(index, "count", 0) == 0:
        index = None
    with _vector_lock:
        per_store[key] = index
        while len(per_store) > _VECTOR_CACHE_MAX:
            per_store.popitem(last=False)
    return index


# ---------------------------------------------------------------------------
# The query encoder
# ---------------------------------------------------------------------------

_backends: dict[str, Any] = {}
_backend_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rp-query-embed")
_QUERY_CACHE_MAX = 1024
_query_cache: OrderedDict = OrderedDict()


def _query_backend(store) -> Any:
    """The encoder queries are embedded with, the one ``/match`` uses.

    The ``RESEARCHER_PROFILES_EMBEDDING_BACKEND`` override wins, then the
    store's ``backend_spec`` (the space its vectors live in). Loaded once per
    process and kept. ``None`` when neither names one or it will not load.
    """
    spec = os.environ.get("RESEARCHER_PROFILES_EMBEDDING_BACKEND") or getattr(
        store, "backend_spec", None
    )
    if not spec:
        return None
    with _backend_lock:
        if spec not in _backends:
            from ..embeddings.backends import get_backend

            try:
                _backends[spec] = get_backend(spec)
            # Boundary: a missing extra or a model that will not load.
            except Exception:
                logger.warning("query encoder %r unavailable", spec, exc_info=True)
                return None
        return _backends[spec]


def _store_of(request_or_app) -> Any:
    app = getattr(request_or_app, "app", request_or_app)
    return getattr(getattr(app, "state", None), "store", None)


def encode(request, text: str, *, timeout_s: Optional[float] = None):
    """``(unit vector | None, encoder name | None, note | None)`` for ``text``.

    ``timeout_s`` defaults to :data:`EMBED_TIMEOUT_S`.
    """
    import numpy as np

    timeout_s = EMBED_TIMEOUT_S if timeout_s is None else timeout_s
    backend = _query_backend(_store_of(request))
    if backend is None:
        return None, None, NOTE_NO_ENCODER
    name = str(getattr(backend, "name", ""))
    key = (name, " ".join((text or "").lower().split()))
    hit = _query_cache.get(key)
    if hit is not None:
        _query_cache.move_to_end(key)
        return hit, name, None
    try:
        raw = _pool.submit(backend.embed, [text]).result(timeout=timeout_s)[0]
    except FutureTimeout:
        return None, name, NOTE_TIMEOUT
    # Boundary: whatever a third-party encoder raises.
    except Exception:
        logger.warning("query embedding failed", exc_info=True)
        return None, name, NOTE_NO_ENCODER
    vec = np.asarray(raw, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(vec))
    if n > 1e-12:
        vec = vec / n
    _query_cache[key] = vec
    while len(_query_cache) > _QUERY_CACHE_MAX:
        _query_cache.popitem(last=False)
    return vec, name, None


def embed_query(request, text: str, *, timeout_s: Optional[float] = None):
    """The query as a unit vector in the store's space, or ``None`` (fall back)."""
    return encode(request, text, timeout_s=timeout_s)[0]


def warm_encoder(app_or_request) -> bool:
    """Load the query encoder and embed one dummy query. ``True`` when it worked.

    Call once at startup so the first real search does not pay the model load
    inside its 3 s budget.
    """
    vec, _name, _note = encode(app_or_request, "warm up", timeout_s=120.0)
    return vec is not None


_SAME_WEIGHTS_PREFIXES = ("st", "fastembed")


def same_space(encoder_name: str | None, index_spec: str | None) -> bool:
    """Whether a query from ``encoder_name`` may be compared with ``index_spec`` vectors.

    True when the names are equal, or when both are the same model under the
    ``st:`` and ``fastembed:`` prefixes (the same weights; ``/match`` already
    treats them as one space).
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


# ---------------------------------------------------------------------------
# Semantic hits, tier-filtered
# ---------------------------------------------------------------------------


def _visible_hits(prof_md, viewer: ViewerTier, hits) -> list:
    """Drop every hit whose chunk source tier exceeds ``viewer``."""
    from ._projection import _visible_hits as visible

    return visible(prof_md, viewer, hits)


def semantic_hits(
    request,
    store,
    prof,
    viewer: ViewerTier,
    query: str,
    *,
    source_types: Iterable[str],
    source_id: Optional[str] = None,
    k: int,
) -> tuple[list, Optional[str]]:
    """Cosine top-``k`` within ONE profile, as ``(hits, note)``.

    Restricted to ``source_types`` (and to one source, usually a paper, when
    ``source_id`` is given), then filtered by the caller's tier per chunk
    (``chunk_source_tiers``). ``hits`` are ``FlatHit``s carrying no text;
    ``note`` is set on every fallback and the hits are then ``[]``. When no
    note is returned, semantic search ran (even if nothing matched).
    """
    import numpy as np

    from ..embeddings.flat import FlatHit

    index = profile_vectors(store, prof.slug)
    if index is None:
        return [], NOTE_NO_VECTORS
    vec, name, note = encode(request, query)
    if vec is None:
        return [], note
    if not same_space(name, index.backend_spec) or vec.shape[0] != index.dim:
        return [], NOTE_MISMATCH
    wanted = set(source_types)
    rows = [
        i
        for i, c in enumerate(index.chunks)
        if c["source_type"] in wanted and (source_id is None or c["source_id"] == source_id)
    ]
    if not rows:
        return [], None
    mat = index._unit_rows()[rows]
    scores = mat @ vec
    order = np.argsort(-scores)
    hits = []
    for j in order:
        c = index.chunks[rows[int(j)]]
        cosine = float(scores[int(j)])
        hits.append(
            FlatHit(
                source_type=c["source_type"],
                source_id=c["source_id"],
                chunk_index=int(c["chunk_index"]),
                section=c.get("section"),
                cosine=round(cosine, 4),
                score=round(max(0.0, 0.5 * (1.0 + cosine)), 4),
            )
        )
    visible = _visible_hits(prof.metadata, viewer, hits)
    return visible[: max(0, k)], None


def chunk_char_counts(store, prof) -> dict[tuple, Optional[int]]:
    """``{(source_type, source_id, chunk_index): char_count}`` of the stored rows."""
    index = profile_vectors(store, prof.slug)
    if index is None:
        return {}
    return {
        (c["source_type"], c["source_id"], int(c["chunk_index"])): c.get("char_count")
        for c in index.chunks
    }


def stored_source_types(store, prof) -> set[str]:
    """The chunk ``source_type``s that have at least one stored vector."""
    return {key[0] for key in chunk_char_counts(store, prof)}


# ---------------------------------------------------------------------------
# Text recovery
# ---------------------------------------------------------------------------

#: The artifact (and its manifest role) each chunk source type is read from.
_SOURCE_ARTIFACT = {
    "soul": ("personality/SOUL.md", "soul"),
    "expertise": ("personality/expertise.md", "expertise"),
    "paper_abstract": ("sources/papers.jsonld", "works"),
    "cv": ("sources/cv.md", "cv"),
    "grant": ("sources/grants.jsonld", "grants"),
}


def source_readable(
    prof, viewer: ViewerTier, source_type: str, source_id: str, explain=None
) -> bool:
    """Whether ``viewer`` may read the source a chunk was cut from.

    Two gates, both must pass: the chunk tier (``chunk_source_tiers``, the
    rule the vector export uses) and the concrete artifact's own tier
    (``artifact_visible``), which carries per-artifact settings such as one
    summary set private.
    """
    md = prof.metadata
    tier = chunk_source_tiers(md, [(source_type, source_id)])[(source_type, source_id)]
    if not tier_allows(viewer, tier):
        return False
    explain = explain if explain is not None else explain_tiers(md)
    if source_type == "paper_summary":
        url, role = f"sources/summaries/{source_id}.summary.md", "paper_summary"
    elif source_type == "web":
        url, role = f"sources/web/{source_id}.md", "web"
    elif source_type in _SOURCE_ARTIFACT:
        url, role = _SOURCE_ARTIFACT[source_type]
    else:
        return False
    return artifact_visible(explain, md, url, role, viewer)


def chunk_texts(
    prof, viewer: ViewerTier, keys: Mapping[tuple, Optional[int]]
) -> tuple[dict[tuple, Any], int]:
    """Recover chunk text for ``keys``, as ``({key: Chunk}, stale_count)``.

    ``keys`` maps ``(source_type, source_id, chunk_index)`` to the stored
    row's ``char_count`` (``None`` when unknown). The deterministic chunkers
    are re-run over the sources this viewer may read, through the profile's
    storage. A chunk whose recomputed length differs from the row's is stale
    (its source was edited since indexing): it is dropped and counted.
    """
    from ..embeddings.chunking import enumerate_source_chunks

    if not keys:
        return {}, 0
    explain = explain_tiers(prof.metadata)
    types = {k[0] for k in keys}
    readable = {(st, sid) for st, sid, _ in keys if source_readable(prof, viewer, st, sid, explain)}
    out: dict[tuple, Any] = {}
    for chunk in enumerate_source_chunks(prof, source_types=types):
        key = (chunk.source_type, chunk.source_id, chunk.chunk_index)
        if key not in keys or (chunk.source_type, chunk.source_id) not in readable:
            continue
        expected = keys[key]
        if expected is not None and int(expected) != len(chunk.text):
            continue
        out[key] = chunk
    # Readable but not recovered: the chunk moved or vanished since indexing.
    stale = sum(1 for k in keys if (k[0], k[1]) in readable and k not in out)
    return out, stale


def stale_note(count: int) -> Optional[str]:
    """The fixed note for semantic matches dropped as stale."""
    if not count:
        return None
    return f"{count} semantic match(es) skipped: the source changed since it was indexed."


def join_notes(*notes: Optional[str]) -> Optional[str]:
    """The non-empty notes, deduplicated, joined by a space; ``None`` when none."""
    seen: list[str] = []
    for n in notes:
        if n and n not in seen:
            seen.append(n)
    return " ".join(seen) or None


# ---------------------------------------------------------------------------
# Chunk search with text (POST /profiles/{slug}/search)
# ---------------------------------------------------------------------------


def search_chunks(
    request, store, prof, viewer: ViewerTier, query: str, *, source_types: Iterable[str], k: int
) -> tuple[Optional[list], Optional[str]]:
    """Semantic top-``k`` chunks of one profile, with their text, as ``(hits, note)``.

    ``hits`` is ``None`` when semantic search cannot run here (``note`` says
    why). Otherwise each hit is a :class:`~researcher_profiles.embeddings.cache.SearchHit`
    whose text was recovered from a source this viewer may read; a stale
    chunk is dropped and counted in ``note``.
    """
    from ..embeddings.cache import SearchHit

    raw, note = semantic_hits(
        request, store, prof, viewer, query, source_types=source_types, k=max(k * 4, k)
    )
    if note is not None:
        return None, note
    counts = chunk_char_counts(store, prof)
    keys = {(h.source_type, h.source_id, h.chunk_index): None for h in raw}
    for key in keys:
        keys[key] = counts.get(key)
    texts, stale = chunk_texts(prof, viewer, keys)
    out = []
    for h in raw:
        chunk = texts.get((h.source_type, h.source_id, h.chunk_index))
        if chunk is None:
            continue
        out.append(
            SearchHit(
                text=chunk.text,
                source_type=h.source_type,
                source_id=h.source_id,
                chunk_index=h.chunk_index,
                section=chunk.section,
                cosine=h.cosine,
                score=h.score,
                meta=dict(chunk.meta or {}),
            )
        )
        if len(out) >= k:
            break
    return out, stale_note(stale)


# ---------------------------------------------------------------------------
# Paper search
# ---------------------------------------------------------------------------


def hybrid_rank_papers(
    request, store, prof, viewer: ViewerTier, q: str, candidates: list[dict]
) -> tuple[list[tuple[str, float, list[str]]], str, Optional[str]]:
    """Rank a profile's papers for ``q``: meaning plus keywords, merged with RRF.

    ``candidates`` is one dict per paper, already filtered by the caller's
    other filters, with keys ``paper_id, title, journal, summary, abstract``
    (``summary`` / ``abstract`` are ``None`` when this viewer may not read
    them). Semantic side: cosine of ``q`` against the profile's
    ``paper_summary`` / ``paper_abstract`` vectors, best chunk per paper,
    kept at ``MIN_COSINE`` or above, and only for a text the candidate says
    this viewer may read. Keyword side: BM25 over title, summary, abstract
    and journal, kept when any query term matches. Papers neither side kept
    are dropped.

    Returns ``([(paper_id, rrf_score, matched_by)], mode, note)``: best first;
    ``mode`` is ``"hybrid"`` when the semantic ranking ran, else
    ``"keyword"`` with a fixed note saying why.
    """
    by_id = {str(c["paper_id"]): c for c in candidates if c.get("paper_id")}
    ids = list(by_id)
    docs = [
        " ".join(str(by_id[i].get(f) or "") for f in ("title", "summary", "abstract", "journal"))
        for i in ids
    ]
    keyword = BM25(docs).ranking(q, ids) if ids else []

    hits, note = semantic_hits(request, store, prof, viewer, q, source_types=_PAPER_TYPES, k=10_000)
    semantic: list[str] = []
    if note is None:
        best: dict[str, float] = {}
        for h in hits:
            cand = by_id.get(h.source_id)
            if cand is None or h.cosine < MIN_COSINE:
                continue
            field = "summary" if h.source_type == "paper_summary" else "abstract"
            if cand.get(field) is None:
                continue
            best[h.source_id] = max(best.get(h.source_id, -1.0), h.cosine)
        semantic = [pid for pid, _ in sorted(best.items(), key=lambda x: (-x[1], x[0]))]
        mode = "hybrid"
    else:
        mode = "keyword"
    return fuse(semantic, keyword), mode, note


__all__ = [
    "BM25",
    "EMBED_TIMEOUT_S",
    "MIN_COSINE",
    "RRF_K",
    "chunk_texts",
    "embed_query",
    "encode",
    "fuse",
    "hybrid_rank_papers",
    "join_notes",
    "profile_vectors",
    "rrf",
    "same_space",
    "search_chunks",
    "semantic_hits",
    "source_readable",
    "stale_note",
    "tokens",
    "warm_encoder",
]
