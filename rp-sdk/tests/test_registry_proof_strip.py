"""No write path stores a registry-issued proof.

``orcid_login`` is computed by the registry each time it serves a document.
A copy an author supplies (create, tarball push, JSON push, PATCH, merge) is
dropped by the store on write, and ``content_hash`` covers the stripped
document. Every other proof kind is kept untouched.
"""

import io
import json
import tarfile
from pathlib import Path

import pytest

from researcher_profiles.schema import ProfileDocument
from researcher_profiles.store import FilesystemProfileStore
from researcher_profiles.store.db import content_hash_for
from researcher_profiles.store.sql import SqlProfileStore

from .factories import build_profile_dir

ORCID = "0000-0002-1825-0097"
LOCAL = "local:lee-local-abc123"
FORGED = {
    "kind": "orcid_login",
    "issuer": "https://evil.example",
    "orcid": ORCID,
    "verifiedAt": "2026-09-01T12:00:00+00:00",
}
ROUNDTRIP = {"kind": "orcid_roundtrip", "issuer": f"https://orcid.org/{ORCID}"}


@pytest.fixture(params=["filesystem", "sql"])
def store(request, tmp_path):
    if request.param == "filesystem":
        return FilesystemProfileStore(tmp_path / "root")
    s = SqlProfileStore("sqlite://")
    s.create_all()
    return s


def _doc(rid: str = ORCID) -> ProfileDocument:
    return ProfileDocument(
        name="Ada Lovelace",
        rid=rid,
        provenance="third_party",
        proof=[FORGED, ROUNDTRIP] if rid == ORCID else [ROUNDTRIP],
    )


def _tar_of_dir(src: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for child in sorted(src.iterdir()):
            tf.add(child, arcname=child.name)
    return buf.getvalue()


def _assert_stripped(store, slug: str) -> None:
    stored = json.loads(store.document_bytes(slug))
    kinds = [p["kind"] for p in stored.get("proof", [])]
    assert "orcid_login" not in kinds
    assert "orcid_roundtrip" in kinds
    soul = store.get(slug).soul or ""
    assert store.content_hash(slug) == content_hash_for(stored, soul)


def test_create(store):
    store.create(_doc(), slug="ada")
    _assert_stripped(store, "ada")
    # The in-memory profile matches what was persisted.
    assert [p.kind for p in store.get("ada").metadata.proof] == ["orcid_roundtrip"]


def test_tarball_push(store, tmp_path, make_api_client):
    staged = build_profile_dir(
        tmp_path / "stage",
        name="Ada Lovelace",
        rid=ORCID,
        level="lite",
        papers=False,
        personality=False,
        summaries=False,
        proof=[FORGED, ROUNDTRIP],
    )
    c = make_api_client(store)
    r = c.put("/api/v1/profiles/ada", content=_tar_of_dir(staged))
    assert r.status_code == 200, r.text
    _assert_stripped(store, "ada")


def test_json_push(store, make_api_client):
    c = make_api_client(store)
    body = {
        "name": "Ada Lovelace",
        "rid": ORCID,
        "provenance": "third_party",
        "proof": [FORGED, ROUNDTRIP],
    }
    r = c.put("/api/v1/profiles/ada", json=body)
    assert r.status_code == 200, r.text
    _assert_stripped(store, "ada")


def test_patch_metadata(store, make_api_client):
    store.create(_doc(), slug="ada")
    c = make_api_client(store)
    r = c.patch("/api/v1/profiles/ada/metadata", json={"proof": [FORGED]})
    # ``proof`` is not an owner-editable field; whatever the answer, nothing is stored.
    assert r.status_code >= 400
    r = c.patch("/api/v1/profiles/ada/metadata", json={"summary": "An edit."})
    assert r.status_code == 200, r.text
    _assert_stripped(store, "ada")


def test_merge_into(tmp_path):
    store = SqlProfileStore("sqlite://")
    store.create_all()
    store.create(_doc(LOCAL), slug="lee")
    staged = build_profile_dir(
        tmp_path / "stage",
        name="Ada Lovelace",
        rid=ORCID,
        level="lite",
        papers=False,
        personality=False,
        summaries=False,
        proof=[FORGED, ROUNDTRIP],
    )
    store.merge_into(
        LOCAL, staged, survivor_rid=ORCID, survivor_slug="lee", build_missing_index=False
    )
    _assert_stripped(store, "lee")


def test_other_kinds_are_kept_verbatim(store):
    store.create(_doc(LOCAL), slug="lee")
    stored = json.loads(store.document_bytes("lee"))
    assert stored["proof"] == [ROUNDTRIP]
