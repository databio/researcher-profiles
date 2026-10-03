"""Per-caller size declarations (``api/_sizes.py``) on rows, records and papers.

Pins the promise a caller builds on: ``available: true`` means *this* caller
can fetch that part through a route that exists, and ``available: false``
always says why (``not_permitted``, ``none``, ``not_uploaded``), so a caller
never retries a part it cannot have.
"""

import pytest

SLUG = "jane-doe"
TIERS = ["public", "limited", "private"]


@pytest.fixture
def client(make_api_client, fixture_profiles_root):
    """jane-doe on a directory store; the caller's tier comes from ``X-Tier``."""
    c = make_api_client(fixture_profiles_root(SLUG))
    c.app.state.viewer_resolver = lambda request, slug: request.headers.get("X-Tier", "public")
    return c


def _get(c, path, tier, **params):
    return c.get(f"/api/v1/profiles/{SLUG}{path}", headers={"X-Tier": tier}, params=params)


def _rows(c, tier):
    r = _get(c, "/papers", tier)
    assert r.status_code == 200, r.text
    return {row["paper_id"]: row for row in r.json()["items"]}


class TestReasons:
    def test_full_text_is_the_owners_alone(self, client):
        """Full text defaults to private: listed for everyone, readable by the owner."""
        owner = _rows(client, "private")["doe2016example"]["text"]
        assert owner["available"] is True
        assert owner["bytes"] > 0 and owner["approx_tokens"] == owner["bytes"] // 4
        for tier in ("public", "limited"):
            text = _rows(client, tier)["doe2016example"]["text"]
            assert text == {"available": False, "reason": "not_permitted"}

    def test_a_paper_with_no_full_text_says_none(self, client):
        assert _rows(client, "private")["doe2021widgets"]["text"] == {
            "available": False,
            "reason": "none",
        }

    def test_a_listed_body_never_pushed_is_not_uploaded(self, client, fixture_profiles_root):
        (fixture_profiles_root(SLUG) / SLUG / "sources" / "papers" / "doe2019methods.md").unlink()
        text = _rows(client, "private")["doe2019methods"]["text"]
        assert text == {"available": False, "reason": "not_uploaded"}

    def test_the_record_sizes_the_narrative_and_counts_what_is_withheld(self, client):
        public = _get(client, "", "public").json()
        assert public["parts"]["soul"]["available"] is True
        assert public["parts"]["files_withheld"] == {"paper_fulltext": 2}
        assert _get(client, "", "private").json()["parts"]["files_withheld"] == {}


@pytest.mark.parametrize("tier", TIERS)
class TestEveryAvailablePartFetches:
    """No door without a handle: each ``available: true`` is a 200 for that caller."""

    def test_rows(self, client, tier):
        for pid, row in _rows(client, tier).items():
            if row["summary"]["available"]:
                got = _get(client, "/summaries", tier, ids=pid).json()
                assert pid in got["summaries"], pid
            else:
                got = _get(client, "/summaries", tier, ids=pid).json()
                assert got["unavailable"][pid] == row["summary"]["reason"]
            status = _get(client, f"/papers/{pid}/text", tier).status_code
            assert status == (200 if row["text"]["available"] else 404), pid

    def test_record(self, client, tier):
        parts = _get(client, "", tier).json()["parts"]
        for section in ("soul", "expertise"):
            status = _get(client, "/text", tier, section=section).status_code
            assert status == (200 if parts[section]["available"] else 404)

    def test_paper_record_sections_are_readable(self, client, tier):
        view = _get(client, "/papers/doe2016example", tier).json()
        assert view["parts"]["text"]["available"] is (view.get("sections") is not None)
        for name in view.get("sections") or []:
            assert (
                _get(client, "/papers/doe2016example/text", tier, section=name).status_code == 200
            )
