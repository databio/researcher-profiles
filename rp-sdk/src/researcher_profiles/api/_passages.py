"""Find the passages inside one paper, or one profile, that answer a query.

A quote or a fact check by whole-text read costs 14-33K tokens; a few
passages cost 1-4K. This builds candidate passages from the sources the
caller may read, ranks them twice (semantic over embedded chunks, BM25 over
all of them), merges with reciprocal-rank fusion, and reports what ran.

- **Paper**: full text (keyword only: paragraphs merged into 400-1,200 char
  windows, each keeping its start offset and section), summary and abstract
  (hybrid over their embedded chunks).
- **Profile**: SOUL, expertise and the CV / web / grant chunks this caller may
  read (hybrid; a source with no stored vectors, which on the SQL store is
  every private one, is searched by keyword only and the note says so).

Offsets are absolute indices into the stored text the text routes page over:
``sources/papers/{pid}.md`` for full text, the raw ``SOUL.md`` /
``expertise.md`` for the narrative.
"""

from __future__ import annotations

import re
from typing import Optional

from ..models.api import Passage, PassageList
from ..privacy import ViewerTier, chunk_source_tiers, explain_tiers
from ..schema.manifest import _SUMMARY_SUFFIX
from . import _semantic as sem
from ._limits import PASSAGES_K, SNIPPET_CHARS, clamp, truncate_words
from ._projection import artifact_visible
from ._text import section_at, sections_of

#: Full-text window bounds, in characters.
WINDOW_MIN = 400
WINDOW_MAX = 1200

NOTE_NO_FULLTEXT = "Full text not available to you."
NOTE_PRIVATE_KEYWORD = "{source} searched by keyword only: no stored vectors for private sources."
NOTE_KEYWORD_ONLY = "{source} searched by keyword only: no stored vectors."

_BLANK = re.compile(r"\n[ \t]*\n")


def _paragraphs(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` spans of the blank-line separated paragraphs of ``text``."""
    spans, pos = [], 0
    for m in _BLANK.finditer(text):
        if text[pos : m.start()].strip():
            spans.append((pos, m.start()))
        pos = m.end()
    if text[pos:].strip():
        spans.append((pos, len(text)))
    return spans


def _split_long(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """A span longer than ``WINDOW_MAX`` cut into pieces at word boundaries."""
    out = []
    while end - start > WINDOW_MAX:
        cut = text.rfind(" ", start + WINDOW_MIN, start + WINDOW_MAX)
        cut = cut if cut != -1 else start + WINDOW_MAX
        out.append((start, cut))
        start = cut + 1 if text[cut : cut + 1] == " " else cut
    if text[start:end].strip():
        out.append((start, end))
    return out


def windows(text: str) -> list[tuple[int, int]]:
    """Paragraphs merged into 400-1,200 char windows: ``[(start, end)]``.

    A window grows paragraph by paragraph while it is under ``WINDOW_MIN``
    and the next one still fits under ``WINDOW_MAX``. A single paragraph
    over ``WINDOW_MAX`` is cut at word boundaries. Spans index ``text``
    directly, so ``text[start:end]`` is the passage verbatim.
    """
    pieces: list[tuple[int, int]] = []
    for s, e in _paragraphs(text):
        pieces.extend(_split_long(text, s, e) if e - s > WINDOW_MAX else [(s, e)])
    out: list[tuple[int, int]] = []
    cur: Optional[list[int]] = None
    for s, e in pieces:
        if cur is None:
            cur = [s, e]
        elif cur[1] - cur[0] < WINDOW_MIN and e - cur[0] <= WINDOW_MAX:
            cur[1] = e
        else:
            out.append((cur[0], cur[1]))
            cur = [s, e]
    if cur is not None:
        out.append((cur[0], cur[1]))
    return out


def _best_window(text: str, query: str) -> tuple[int, str]:
    """``(offset_in_text, snippet)``: the window of ``text`` that best matches ``query``."""
    if len(text) <= SNIPPET_CHARS:
        return 0, text
    spans = windows(text) or [(0, len(text))]
    scores = sem.BM25([text[s:e] for s, e in spans]).scores(query)
    i = max(range(len(spans)), key=lambda j: (scores[j], -j))
    s, e = spans[i]
    snippet, _ = truncate_words(text[s:e], SNIPPET_CHARS)
    return s, snippet


#: A passage needs this much text besides its headings to answer anything.
MIN_BODY_CHARS = 40


def _has_body(text: str) -> bool:
    """False for a heading-only chunk (an H2 directly followed by an H3 makes one)."""
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return len(body.strip()) >= MIN_BODY_CHARS


class _Candidate:
    """One passage candidate before ranking."""

    __slots__ = ("key", "source", "source_id", "section", "offset", "text", "vector_key")

    def __init__(self, key, source, source_id, section, offset, text, vector_key=None):
        self.key = key
        self.source = source
        self.source_id = source_id
        self.section = section
        self.offset = offset
        self.text = text
        self.vector_key = vector_key


def _rank(store, prof, viewer, query: str, cands: list[_Candidate], *, types, source_id, k):
    """Fuse a semantic and a keyword ranking over ``cands``.

    Returns ``(fused, semantic_ran, note, stale)``. Only candidates with a
    ``vector_key`` whose stored row still matches the current text (same
    ``char_count``) take part in the semantic side; the rest are counted in
    ``stale``.
    """
    counts = sem.chunk_char_counts(store, prof)
    by_vkey: dict = {}
    stale = 0
    for c in cands:
        if c.vector_key is None or c.vector_key not in counts:
            continue
        expected = counts[c.vector_key]
        if expected is not None and int(expected) != len(c.text):
            stale += 1
            continue
        by_vkey[c.vector_key] = c.key
    keyword = sem.BM25([c.text for c in cands]).ranking(query, [c.key for c in cands])
    semantic: list = []
    ran = False
    if not counts:
        note = sem.NOTE_NO_VECTORS
    elif not by_vkey:
        note = None
    else:
        hits, note = sem.semantic_hits(
            store, prof, viewer, query, source_types=types, source_id=source_id, k=10_000
        )
        if note is None:
            ran = True
            for h in hits:
                key = by_vkey.get((h.source_type, h.source_id, h.chunk_index))
                if key is not None and h.cosine >= sem.MIN_COSINE and key not in semantic:
                    semantic.append(key)
    return sem.fuse(semantic, keyword)[:k], ran, note, stale


def _summary_text(prof, viewer, explain, paper_id: str, record) -> tuple[Optional[str], bool]:
    """``(summary text, embedded)``: the summary artifact when readable, else the record's."""
    summaries = prof.summaries
    url = f"sources/summaries/{paper_id}{_SUMMARY_SUFFIX}"
    if paper_id in summaries and artifact_visible(
        explain, prof.metadata, url, "paper_summary", viewer
    ):
        return summaries[paper_id], True
    return (getattr(record, "summary", None) or None), False


def paper_passages(
    store, prof, viewer: ViewerTier, record, query: str, k: Optional[int]
) -> PassageList:
    """The passages of one paper (``record``) that best answer ``query``."""
    from ..embeddings.chunking import chunk_abstract, chunk_summary

    k_applied = clamp(k, *PASSAGES_K)
    pid = str(record.paper_id)
    explain = explain_tiers(prof.metadata)
    md = prof.metadata
    cands: list[_Candidate] = []
    searched: list[str] = []
    notes: list[Optional[str]] = []

    fulltext_url = f"sources/papers/{pid}.md"
    full = None
    if artifact_visible(explain, md, fulltext_url, "paper_fulltext", viewer):
        try:
            full = prof.storage.artifact_text(fulltext_url)
        except Exception:  # noqa: BLE001 - an unreadable body is an absent one
            full = None
    if full:
        searched.append("full_text")
        secs = sections_of(full)
        for i, (s, e) in enumerate(windows(full)):
            cands.append(_Candidate(("f", i), "full_text", None, section_at(secs, s), s, full[s:e]))
    else:
        notes.append(NOTE_NO_FULLTEXT)

    summary, embedded = _summary_text(prof, viewer, explain, pid, record)
    if summary:
        searched.append("summary")
        for ch in chunk_summary(summary, pid):
            off = max(summary.find(ch.text), 0)
            vkey = ("paper_summary", pid, ch.chunk_index) if embedded else None
            cands.append(
                _Candidate(("s", ch.chunk_index), "summary", None, None, off, ch.text, vkey)
            )
    abstract = getattr(record, "abstract", None)
    if abstract:
        searched.append("abstract")
        for ch in chunk_abstract(abstract, pid):
            off = max(abstract.find(ch.text), 0)
            vkey = ("paper_abstract", pid, ch.chunk_index)
            cands.append(
                _Candidate(("a", ch.chunk_index), "abstract", None, None, off, ch.text, vkey)
            )

    types = {"paper_summary", "paper_abstract"}
    fused, ran, sem_note, stale = _rank(
        store, prof, viewer, query, cands, types=types, source_id=pid, k=k_applied
    )
    notes.append(sem.stale_note(stale))
    if ran:
        mode = "hybrid+keyword_fulltext" if full else "hybrid"
    else:
        mode = "keyword"
        notes.insert(0, sem_note)
    return _passage_list(cands, fused, k_applied, mode, searched, notes, query)


#: Profile-level sources, in the order they are listed in ``searched``.
_PROFILE_SOURCES = ("soul", "expertise", "cv", "web", "grant")


def profile_passages(store, prof, viewer: ViewerTier, query: str, k: Optional[int]) -> PassageList:
    """The passages of one profile's narrative, CV, web pages and grants."""
    from ..embeddings.chunking import enumerate_source_chunks

    k_applied = clamp(k, *PASSAGES_K)
    explain = explain_tiers(prof.metadata)
    chunks = [
        c
        for c in enumerate_source_chunks(prof, source_types=set(_PROFILE_SOURCES))
        if sem.source_readable(prof, viewer, c.source_type, c.source_id, explain)
        and _has_body(c.text)
    ]
    raw = {}
    for st, getter in (
        ("soul", prof.storage.load_soul),
        ("expertise", prof.storage.load_expertise),
    ):
        if any(c.source_type == st for c in chunks):
            raw[st] = getter() or ""

    stored = sem.stored_source_types(store, prof)
    has_vectors = bool(stored)
    cands: list[_Candidate] = []
    cursor: dict[str, int] = {}
    for i, ch in enumerate(chunks):
        st = ch.source_type
        vkey = (st, ch.source_id, ch.chunk_index) if st in stored else None
        if st in raw:
            text = raw[st]
            base = text.find(ch.text, cursor.get(st, 0))
            base = base if base != -1 else max(text.find(ch.text), 0)
            cursor[st] = base
            cands.append(_Candidate(("p", i), st, None, st, base, ch.text, vkey))
        else:
            sid = ch.source_id if st in ("web", "grant") else None
            cands.append(_Candidate(("p", i), st, sid, ch.section, None, ch.text, vkey))

    searched = [s for s in _PROFILE_SOURCES if any(c.source_type == s for c in chunks)]
    notes: list[Optional[str]] = []
    if has_vectors:
        tiers = chunk_source_tiers(prof.metadata, [(s, s) for s in searched])
        for s in searched:
            if s not in stored:
                private = tiers[(s, s)] != "public"
                notes.append(
                    (NOTE_PRIVATE_KEYWORD if private else NOTE_KEYWORD_ONLY).format(source=s)
                )

    fused, ran, sem_note, stale = _rank(
        store, prof, viewer, query, cands, types=set(searched), source_id=None, k=k_applied
    )
    notes.append(sem.stale_note(stale))
    if ran:
        mode = "hybrid"
    else:
        mode = "keyword"
        notes.insert(0, sem_note)
    return _passage_list(cands, fused, k_applied, mode, searched, notes, query)


def _passage_list(cands, fused, k_applied, mode, searched, notes, query) -> PassageList:
    by_key = {c.key: c for c in cands}
    out = []
    for key, score, by in fused:
        c = by_key[key]
        text, offset = c.text, c.offset
        if len(text) > SNIPPET_CHARS:
            # A long chunk: show its best-matching window, not its first lines.
            off, text = _best_window(text, query)
            offset = None if offset is None else offset + off
        text, _ = truncate_words(text, SNIPPET_CHARS)
        out.append(
            Passage(
                source=c.source,
                source_id=c.source_id,
                section=c.section,
                offset=offset,
                text=text,
                score=score,
                matched_by=by,
            )
        )
    return PassageList(
        passages=out,
        k_applied=k_applied,
        search_mode_used=mode,
        searched=searched,
        note=sem.join_notes(*notes),
    )


__all__ = ["WINDOW_MAX", "WINDOW_MIN", "paper_passages", "profile_passages", "windows"]
