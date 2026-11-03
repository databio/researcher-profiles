"""The shared list envelope (``api/paging.py``): page math and clamping."""

from researcher_profiles.api.paging import PageRequest, paginate


def _req(page=0, page_size=10, asked=None):
    return PageRequest(page=page, page_size=page_size, asked=asked or page_size)


def test_an_empty_list_has_no_pages():
    got = paginate([], _req())
    assert got.results == []
    assert got.pagination.model_dump(exclude_none=True) == {
        "page": 0,
        "page_size": 10,
        "total": 0,
        "total_pages": 0,
    }


def test_the_last_page_is_partial():
    rows = list(range(25))
    got = paginate(rows, _req(page=2))
    assert got.results == [20, 21, 22, 23, 24]
    assert (got.pagination.total, got.pagination.total_pages) == (25, 3)
    assert paginate(rows, _req(page=3)).results == []


def test_a_clamped_page_size_is_reported():
    got = paginate(list(range(5)), _req(page_size=3, asked=50))
    assert got.pagination.page_size == 3
    assert "clamped to 3" in got.note
    assert paginate(list(range(5)), _req()).note is None
