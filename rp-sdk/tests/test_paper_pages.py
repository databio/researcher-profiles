"""Token-paged paper rows (``GET /profiles/{slug}/papers``) and their tokens (``api/_page_token.py``).

A caller loops on ``pagination.next_page_token``: every row once, last page ``null``; a token
is bound to its filters, so reusing it with others is a 400 ``page_token_mismatch``.
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


@pytest.fixture(scope="module")
def _pages_root(tmp_path_factory):
    """Built once: every test here is a GET, so the tree is read-only."""
    root = tmp_path_factory.mktemp("pages") / "profiles"
    build_profile_dir(root / SLUG, papers=_papers(), summaries=False)
    return root


@pytest.fixture
def client(_pages_root, make_api_client):
    return make_api_client(_pages_root)


def _page(c, **params):
    r = c.get(f"/api/v1/profiles/{SLUG}/papers", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _walk(c, **params) -> list[str]:
    seen: list[str] = []
    page = _page(c, **params)
    while True:
        seen += [row["paper_id"] for row in page["results"]]
        token = page["pagination"].get("next_page_token")
        if not token:
            return seen
        page = _page(c, **params, page_token=token)


class TestPageToken:
    def test_following_the_token_visits_every_row_once_newest_first(self, client):
        first = _page(client, page_size=20)
        assert first["pagination"]["page_size"] == 20
        assert first["pagination"]["total"] == N + 1
        assert first["pagination"]["next_page_token"]
        seen = _walk(client, page_size=20)
        assert len(seen) == len(set(seen)) == N + 1
        years = [r["year"] for r in _page(client, page_size=100)["results"] if r.get("year")]
        assert years == sorted(years, reverse=True)
        assert seen[-1] == "noyear"  # undated last

    def test_page_size_may_change_between_pages(self, client):
        token = _page(client, page_size=5)["pagination"]["next_page_token"]
        second = _page(client, page_size=7, page_token=token)
        assert len(second["results"]) == 7

    def test_filters_apply_before_paging(self, client):
        seen = _walk(client, page_size=5, missing_ids=True, year_min=2010)
        expected = {r["paper_id"] for r in _papers() if "doi" not in r and r.get("year", 0) >= 2010}
        assert set(seen) == expected
        page = _page(client, page_size=5, missing_ids=True, year_min=2010)
        assert page["filters_applied"] == {"missing_ids": True, "year_min": 2010}
        assert page["pagination"]["total"] == len(expected)

    @pytest.mark.parametrize(
        ("token_kind", "other"),
        [
            ("real", {"year_min": 2001}),
            ("real", {"missing_ids": True}),
            ("not-a-token", {}),
        ],
        ids=["year_min", "missing_ids", "garbled"],
    )
    def test_a_token_is_bound_to_its_filters(self, client, token_kind, other):
        token = (
            _page(client, page_size=5)["pagination"]["next_page_token"]
            if token_kind == "real"
            else token_kind
        )
        r = client.get(
            f"/api/v1/profiles/{SLUG}/papers",
            params={"page_size": 5, "page_token": token, **other},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "page_token_mismatch"


class TestRankedPages:
    def test_a_query_is_one_top_n_page(self, client):
        """No index on this profile: keyword ranking, and the response says so."""
        page = _page(client, q="chromatin", page_size=4)
        assert page["search_mode_used"] == "keyword"
        assert page["note"]
        assert page["pagination"] == {"page_size": 4}  # no total, no next token
        assert len(page["results"]) == 4
        assert all(row["matched_by"] == ["keyword"] for row in page["results"])

    def test_a_query_takes_no_page_token(self, client):
        token = _page(client, page_size=5)["pagination"]["next_page_token"]
        r = client.get(
            f"/api/v1/profiles/{SLUG}/papers",
            params={"q": "chromatin", "page_token": token},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_argument"


class TestIds:
    def test_ids_read_those_rows_in_order(self, client):
        page = _page(client, ids="p007,p001,nobody")
        assert [r["paper_id"] for r in page["results"]] == ["p007", "p001"]
        assert page["pagination"] == {"page_size": 3, "total": 2}
