"""The shared list envelope (``api/paging.py``): page math, clamping, unknown params."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from researcher_profiles.api import install_service_errors
from researcher_profiles.api.paging import (
    PageRequest,
    page_params,
    page_request,
    paged_from_sql,
    paginate,
    sql_window,
)
from researcher_profiles.errors import Invalid


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


def test_sql_window_and_wrap():
    req = _req(page=2, page_size=10)
    assert sql_window(req) == (10, 20)
    got = paged_from_sql(["a"], 21, req)
    assert (got.pagination.total, got.pagination.total_pages) == (21, 3)


def test_page_request_outside_fastapi():
    assert page_request(1, 5000, 1000) == PageRequest(page=1, page_size=1000, asked=5000)
    with pytest.raises(Invalid):
        page_request(-1, 10)
    with pytest.raises(Invalid):
        page_request(0, 0)


@pytest.fixture
def client():
    app = FastAPI()
    install_service_errors(app)

    @app.get("/things")
    def things(paging: PageRequest = Depends(page_params(10, 50, filters={"kind"}))):
        return paginate(list(range(120)), paging).model_dump(mode="json")

    return TestClient(app)


def test_the_dependency_clamps_and_validates(client):
    body = client.get("/things", params={"page": 1, "page_size": 500}).json()
    assert body["pagination"] == {
        "page": 1,
        "page_size": 50,
        "total": 120,
        "total_pages": 3,
    }
    assert body["results"][0] == 50
    assert client.get("/things", params={"page": -1}).status_code == 422
    assert client.get("/things", params={"page_size": 0}).status_code == 422


def test_an_unknown_parameter_is_named(client):
    r = client.get("/things", params={"orcid": "x", "kind": "a"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_argument"
    assert "orcid" in detail["message"]
    assert "kind" not in detail["message"]
