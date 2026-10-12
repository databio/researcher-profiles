"""Caps on every caller-chosen count the HTTP API accepts.

Clamp and report: a count above its cap is lowered to the cap, never refused,
and the response says what ran (``limit_applied``, ``k_applied``, ...). The
caps are stated in the docs and in tool descriptions, never as a schema
``maximum``: some clients validate a request against the schema before
sending it and would reject the call outright instead of getting the cap.
"""

from __future__ import annotations


def clamp(n: int | None, default: int, cap: int) -> int:
    """Clamp a caller's count to ``[1, cap]``; ``None`` means the default."""
    return max(1, min(n if n is not None else default, cap))


#: ``GET /profiles/{slug}/papers``: (default, cap) rows per page.
PAPERS_LIMIT = (20, 100)
#: Ids in one batch read (``?ids=``, ``/summaries?ids=``).
BATCH_IDS_CAP = 20
#: ``POST /profiles/{slug}/search``: (default, cap) hits.
SEARCH_K = (5, 20)
#: ``POST /match`` and ``/match/reviewers``: (default, cap) matches.
MATCH_K = (5, 50)
#: Profiles the match centroid prefilter may keep.
MATCH_PREFILTER_CAP = 200
#: Evidence chunks per match.
TOPK_CHUNKS_CAP = 10
#: ``POST /profiles/{slug}/rank-works``: (default, cap) works.
RANK_K = (10, 50)
#: OpenAlex pages one rank-works call may fetch.
RANK_MAX_PAGES_CAP = 10
#: Retrieval depth of the persona routes (ask, review, innovate, riff).
PERSONA_K_CAP = 20
#: ``POST .../passages``: (default, cap) passages.
PASSAGES_K = (3, 10)
#: The most text one search hit or passage carries, in characters.
SNIPPET_CHARS = 1200
#: The default text page, in characters (about 20K tokens).
TEXT_CHUNK = 80_000
#: The most text one page may carry when the caller asks for more.
TEXT_MAX_CHARS_CAP = 200_000


def truncate_words(text: str, limit: int = SNIPPET_CHARS) -> tuple[str, bool]:
    """``(text, truncated)``: ``text`` cut to ``limit`` chars at a word boundary."""
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(), True


__all__ = [
    "BATCH_IDS_CAP",
    "MATCH_K",
    "MATCH_PREFILTER_CAP",
    "PAPERS_LIMIT",
    "PASSAGES_K",
    "PERSONA_K_CAP",
    "RANK_K",
    "RANK_MAX_PAGES_CAP",
    "SEARCH_K",
    "SNIPPET_CHARS",
    "TEXT_CHUNK",
    "TEXT_MAX_CHARS_CAP",
    "TOPK_CHUNKS_CAP",
    "clamp",
    "truncate_words",
]
