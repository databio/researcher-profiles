"""Registry-issued proofs are attached when a document is served.

``app.state.registry_proofs`` computes them per read. Both ``profile.jsonld``
URLs and the detail payload carry them; the archive does not; a stored copy is
never served; without the hook the stored bytes go out verbatim.
"""

import io
import json
import tarfile

import pytest

from researcher_profiles.schema import ProfileDocument, Proof
from researcher_profiles.store.db import ProfileRow
from researcher_profiles.store.sql import SqlProfileStore

from .factories import build_profile_dir

ORCID = "0000-0002-1825-0097"
LOCAL = "local:lee-local-abc123"
ISSUER = "https://registry.example"


def _proof(when: str = "2026-09-01T12:00:00+00:00") -> Proof:
    return Proof(kind="orcid_login", issuer=ISSUER, orcid=ORCID, verifiedAt=when)


@pytest.fixture
def store():
    s = SqlProfileStore("sqlite://")
    s.create_all()
    s.create(ProfileDocument(name="Ada Lovelace", rid=ORCID, provenance="third_party"), slug="ada")
    return s


@pytest.fixture
def calls():
    return []


@pytest.fixture
def client(store, make_api_client, calls):
    c = make_api_client(store)
    state = {"when": "2026-09-01T12:00:00+00:00"}

    def hook(request, rid):
        calls.append(rid)
        return [_proof(state["when"])] if rid == ORCID else []

    c.app.state.registry_proofs = hook
    c.hook_state = state
    return c


def _login_proofs(doc: dict, key: str = "proof") -> list[dict]:
    return [p for p in doc.get(key, []) if p["kind"] == "orcid_login"]


def test_all_three_reads_carry_the_proof_once(client):
    for url in (
        "/api/v1/profiles/ada/profile.jsonld",
        "/api/v1/profiles/ada/content/profile.jsonld",
    ):
        r = client.get(url)
        assert r.status_code == 200, r.text
        proofs = _login_proofs(r.json())
        assert proofs == [
            {
                "kind": "orcid_login",
                "issuer": ISSUER,
                "orcid": ORCID,
                "verifiedAt": "2026-09-01T12:00:00+00:00",
            }
        ]
    detail = client.get("/api/v1/profiles/ada").json()
    assert [p["kind"] for p in detail["metadata"]["proof"]] == ["orcid_login"]


def test_both_urls_same_bytes_and_etag_and_304(client):
    a = client.get("/api/v1/profiles/ada/profile.jsonld")
    b = client.get("/api/v1/profiles/ada/content/profile.jsonld")
    assert a.content == b.content
    assert a.headers["etag"] == b.headers["etag"]
    r = client.get(
        "/api/v1/profiles/ada/content/profile.jsonld",
        headers={"If-None-Match": a.headers["etag"]},
    )
    assert r.status_code == 304


def test_hook_change_changes_etag(client):
    before = client.get("/api/v1/profiles/ada/profile.jsonld").headers["etag"]
    client.hook_state["when"] = "2026-09-02T12:00:00+00:00"
    after = client.get("/api/v1/profiles/ada/profile.jsonld").headers["etag"]
    assert before != after


def test_stored_copy_is_not_served(client, store):
    forged = {**_proof().model_dump(mode="json"), "issuer": "https://evil.example"}
    with store.session() as s:
        row = s.get(ProfileRow, ORCID)
        row.document = {**row.document, "proof": [forged]}
        s.add(row)
        s.commit()
    store.evict("ada")
    doc = client.get("/api/v1/profiles/ada/profile.jsonld").json()
    assert [p["issuer"] for p in _login_proofs(doc)] == [ISSUER]


def test_no_hook_serves_stored_bytes_verbatim(store, make_api_client):
    c = make_api_client(store)
    r = c.get("/api/v1/profiles/ada/profile.jsonld")
    assert r.content == store.document_bytes("ada")
    assert c.get("/api/v1/profiles/ada/content/profile.jsonld").content == r.content


def test_no_hook_still_strips_a_stored_copy(store, make_api_client):
    forged = {**_proof().model_dump(mode="json"), "issuer": "https://evil.example"}
    with store.session() as s:
        row = s.get(ProfileRow, ORCID)
        row.document = {**row.document, "proof": [forged]}
        s.add(row)
        s.commit()
    store.evict("ada")
    c = make_api_client(store)
    for url in (
        "/api/v1/profiles/ada/profile.jsonld",
        "/api/v1/profiles/ada/content/profile.jsonld",
    ):
        r = c.get(url)
        assert r.status_code == 200, r.text
        assert _login_proofs(r.json()) == []


def test_raising_hook_is_a_200_without_proof(store, make_api_client):
    c = make_api_client(store)

    def boom(request, rid):
        raise RuntimeError("nope")

    c.app.state.registry_proofs = boom
    r = c.get("/api/v1/profiles/ada/profile.jsonld")
    assert r.status_code == 200
    assert _login_proofs(r.json()) == []
    assert c.get("/api/v1/profiles/ada").status_code == 200


def test_archive_carries_no_registry_proof(client):
    r = client.get("/api/v1/profiles/ada/archive")
    assert r.status_code == 200, r.text
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
        doc = json.loads(tf.extractfile("profile.jsonld").read())
    assert _login_proofs(doc) == []


def test_section_projection_keeps_the_proof(client, store):
    from researcher_profiles.schema import SectionVisibility

    prof = store.get("ada")
    doc = prof.metadata
    doc.summary = "A restricted summary."
    doc.section_visibility = [SectionVisibility(section="summary", visibility="restricted")]
    prof.save_profile(doc)
    store.evict("ada")
    for url in (
        "/api/v1/profiles/ada/profile.jsonld",
        "/api/v1/profiles/ada/content/profile.jsonld",
    ):
        served = client.get(url).json()
        assert "summary" not in served
        assert len(_login_proofs(served)) == 1


def test_retired_alias_serves_the_survivor_proof(tmp_path, make_api_client, calls):
    store = SqlProfileStore("sqlite://")
    store.create_all()
    store.create(ProfileDocument(name="Lee", rid=LOCAL, provenance="synthetic"), slug="lee")
    store.create(ProfileDocument(name="Ada", rid=ORCID, provenance="third_party"), slug="ada")
    staged = build_profile_dir(
        tmp_path / "stage",
        name="Ada",
        rid=ORCID,
        level="lite",
        papers=False,
        personality=False,
        summaries=False,
    )
    store.merge_into(
        "lee", staged, survivor_rid=ORCID, survivor_slug="ada", build_missing_index=False
    )
    c = make_api_client(store)
    c.app.state.registry_proofs = lambda request, rid: (
        calls.append(rid) or ([_proof()] if rid == ORCID else [])
    )
    doc = c.get("/api/v1/profiles/lee/profile.jsonld").json()
    assert doc["rid"] == ORCID
    assert len(_login_proofs(doc)) == 1
    assert calls and set(calls) == {ORCID}
