"""Caps on caller-chosen counts (``api/_limits.py``): clamp and report, never refuse.

Every capped parameter runs at its cap when asked for more, and the response
says what ran. A refusal (a schema ``maximum``, a 422) would make a client
that asked for "everything" fail instead of getting the most it may have.
Search and persona ``k``, match counts and passage ``k`` are pinned with
their routes' own tests (``test_api.py``, ``test_semantic.py``,
``test_passages.py``); this file holds the read-surface caps.
"""

import pytest

from .factories import build_profile_dir

SLUG = "many-papers"


@pytest.fixture
def client(tmp_path, make_api_client):
    root = tmp_path / "profiles"
    papers = [{"paper_id": f"p{i:03d}", "title": f"Study {i}", "year": 2000} for i in range(130)]
    build_profile_dir(
        root / SLUG,
        papers=papers,
        summaries={f"p{i:03d}": f"Summary {i}.\n" for i in range(30)},
    )
    return make_api_client(root)


@pytest.mark.parametrize(
    "limit, applied, rows",
    [(10_000, 100, 100), (None, 20, 20), (0, 1, 1)],
    ids=["huge", "default", "zero"],
)
def test_the_page_size_is_clamped_and_reported(client, limit, applied, rows):
    params = {} if limit is None else {"limit": limit}
    page = client.get(f"/api/v1/profiles/{SLUG}/papers", params=params).json()
    assert page["limit_applied"] == applied
    assert len(page["items"]) == rows


def test_a_row_batch_reads_at_most_twenty(client):
    ids = ",".join(f"p{i:03d}" for i in range(25))
    page = client.get(f"/api/v1/profiles/{SLUG}/papers", params={"ids": ids}).json()
    assert page["limit_applied"] == 20
    assert len(page["items"]) == 20
    assert page["note"]


def test_a_summary_batch_reads_at_most_twenty_and_names_the_rest(client):
    ids = [f"p{i:03d}" for i in range(25)]
    got = client.get(f"/api/v1/profiles/{SLUG}/summaries", params={"ids": ",".join(ids)}).json()
    assert got["limit_applied"] == 20
    assert got["not_processed"] == ids[20:]
    assert sorted(got["summaries"]) == ids[:20]


def test_hit_text_is_cut_at_a_word_and_flagged(client, monkeypatch):
    """A search hit carries at most ``SNIPPET_CHARS`` of text, and says when it was cut."""
    from researcher_profiles.api import _semantic
    from researcher_profiles.api._limits import SNIPPET_CHARS
    from researcher_profiles.embeddings.cache import SearchHit

    long = "word " * 1000
    hit = SearchHit(
        text=long,
        source_type="paper_summary",
        source_id="p001",
        chunk_index=0,
        section=None,
        cosine=0.9,
        score=0.9,
        meta={},
    )
    monkeypatch.setattr(_semantic, "search_chunks", lambda *a, **kw: ([hit], None))
    r = client.post(f"/api/v1/profiles/{SLUG}/search", json={"query": "x", "k": 1})
    assert r.status_code == 200, r.text
    served = r.json()["hits"][0]
    assert served["truncated"] is True
    assert len(served["text"]) <= SNIPPET_CHARS
    assert long.startswith(served["text"]) and not served["text"].endswith(" ")
