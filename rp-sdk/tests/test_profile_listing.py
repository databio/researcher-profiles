"""``GET /api/v1/profiles``: page-based, with exact ``rid``/``clinical``/``affiliation`` filters."""

import pytest

from researcher_profiles.api.deps import TierFloor

from .factories import build_profile_dir

AREA = {"system": "http://snomed.info/sct", "code": "363346000", "label": "Cancer"}
DOC = "0000-0002-1825-0097"
RES = "0000-0004-4600-113X"
LAB = "0000-0001-5109-3700"


@pytest.fixture
def client(tmp_path, make_api_client):
    root = tmp_path / "root"
    build_profile_dir(root / "doc", rid=DOC, therapeutic_areas=[AREA], affiliation="UVA")
    build_profile_dir(root / "res", rid=RES, affiliation="Stanford")
    build_profile_dir(
        root / "held",
        rid=LAB,
        therapeutic_areas=[AREA],
        affiliation="uva ",
        section_visibility=[{"section": "clinical", "visibility": "private"}],
    )
    return make_api_client(root)


def _list(c, **params):
    r = c.get("/api/v1/profiles", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _slugs(body):
    return [row["slug"] for row in body["results"]]


def test_the_envelope_pages_in_slug_order(client):
    body = _list(client)
    assert _slugs(body) == ["doc", "held", "res"]
    assert body["pagination"] == {
        "page": 0,
        "page_size": 100,
        "total": 3,
        "total_pages": 1,
    }
    second = _list(client, page=1, page_size=2)
    assert _slugs(second) == ["res"]
    assert second["pagination"]["total_pages"] == 2


def test_clinical_counts_only_clinical_rows(client):
    body = _list(client, clinical="true")
    # ``held`` has therapeutic areas, but its clinical section is private.
    assert _slugs(body) == ["doc"]
    assert body["pagination"]["total"] == 1
    assert _slugs(_list(client, clinical="false")) == ["held", "res"]


def test_rid_is_repeatable_and_ors(client):
    assert _slugs(_list(client, rid=[RES, DOC])) == ["doc", "res"]
    unknown = "0000-0003-1415-9269"
    assert _slugs(_list(client, rid=[unknown])) == []


def test_filters_and_together(client):
    assert _slugs(_list(client, rid=[DOC, RES], clinical="true")) == ["doc"]


def test_affiliation_is_exact_ignoring_case_and_spaces(client):
    assert _slugs(_list(client, affiliation="UVA")) == ["doc", "held"]
    assert _slugs(_list(client, affiliation="UV")) == []


def test_an_invalid_rid_is_400(client):
    r = client.get("/api/v1/profiles", params={"rid": "not-an-orcid"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_argument"


def test_orcid_is_not_a_filter(client):
    r = client.get("/api/v1/profiles", params={"orcid": DOC})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_argument"
    assert "orcid" in r.json()["detail"]["message"]


def test_a_hidden_profile_is_never_counted(client):
    client.app.state.hooks.profile_tier_floor = lambda caller, prof, slug: (
        TierFloor("private", "held back") if slug == "res" else None
    )
    body = _list(client)
    assert _slugs(body) == ["doc", "held"]
    assert body["pagination"]["total"] == 2
    assert _slugs(_list(client, rid=[RES])) == []
