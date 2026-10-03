"""Paged paper rows (``GET /profiles/{slug}/papers``) and their cursors (``api/_cursor.py``).

Pins the paging contract a caller loops on: following ``next_cursor`` visits
every matching row exactly once, the last page says ``next_cursor: null`` and
``has_more: false``, and a cursor is bound to the filters it was made under,
so reusing it with others is a 400 ``cursor_mismatch`` rather than a page of
some other query.
"""

import pytest

from .factories import build_profile_dir

SLUG = "many-papers"
N = 45


def _papers() -> list[dict]:
    out = []
    for i in range(N):
        row = {
            "paper_id": f"p{i:03d}",
            "title": f"Study {i} of chromatin" if i % 3 == 0 else f"Study {i} of proteins",
            "year": 2000 + (i % 20),
        }
        if i % 4:
            row["doi"] = f"10.1/{i}"
        out.append(row)
    out.append({"paper_id": "noyear", "title": "An undated preprint"})
    return out


@pytest.fixture
def client(tmp_path, make_api_client):
    root = tmp_path / "profiles"
    build_profile_dir(root / SLUG, papers=_papers(), summaries=False)
    return make_api_client(root)


def _page(c, **params):
    r = c.get(f"/api/v1/profiles/{SLUG}/papers", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _walk(c, **params) -> list[str]:
    seen: list[str] = []
    page = _page(c, **params)
    while True:
        seen += [row["paper_id"] for row in page["items"]]
        if not page["has_more"]:
            assert page.get("next_cursor") is None
            return seen
        page = _page(c, **params, cursor=page["next_cursor"])


class TestCursor:
    def test_following_the_cursor_visits_every_row_once_newest_first(self, client):
        first = _page(client, limit=20)
        assert (first["total"], first["limit_applied"], first["has_more"]) == (N + 1, 20, True)
        seen = _walk(client, limit=20)
        assert len(seen) == len(set(seen)) == N + 1
        years = [r["year"] for r in _page(client, limit=100)["items"] if r.get("year")]
        assert years == sorted(years, reverse=True)
        assert seen[-1] == "noyear"  # undated last

    def test_filters_apply_before_paging(self, client):
        seen = _walk(client, limit=5, missing_ids=True, year_min=2010)
        expected = {r["paper_id"] for r in _papers() if "doi" not in r and r.get("year", 0) >= 2010}
        assert set(seen) == expected
        page = _page(client, limit=5, missing_ids=True, year_min=2010)
        assert page["filters_applied"] == {"missing_ids": True, "year_min": 2010}
        assert page["total"] == len(expected)

    @pytest.mark.parametrize(
        "other",
        [{"year_min": 2001}, {"missing_ids": True}, {"q": "chromatin"}],
        ids=["year_min", "missing_ids", "query"],
    )
    def test_a_cursor_is_bound_to_its_filters(self, client, other):
        cursor = _page(client, limit=5)["next_cursor"]
        r = client.get(
            f"/api/v1/profiles/{SLUG}/papers", params={"limit": 5, "cursor": cursor, **other}
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "cursor_mismatch"

    def test_a_garbled_cursor_is_a_400(self, client):
        r = client.get(f"/api/v1/profiles/{SLUG}/papers", params={"cursor": "not-a-cursor"})
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "cursor_mismatch"


class TestRankedPages:
    def test_a_query_ranks_and_pages_by_offset(self, client):
        """No index on this profile: keyword ranking, and the response says so."""
        page = _page(client, q="chromatin", limit=4)
        assert page["search_mode_used"] == "keyword"
        assert page["note"]
        assert page["total"] == len([r for r in _papers() if "chromatin" in r["title"]])
        assert all(row["matched_by"] == ["keyword"] for row in page["items"])
        seen = _walk(client, q="chromatin", limit=4)
        assert len(seen) == len(set(seen)) == page["total"]


class TestIds:
    def test_ids_read_those_rows_in_order(self, client):
        page = _page(client, ids="p007,p001,nobody")
        assert [r["paper_id"] for r in page["items"]] == ["p007", "p001"]
        assert page["limit_applied"] == 3
