"""A generated artifact's ``inputsDigest`` survives a manifest refresh only
while the file is byte-identical."""

from __future__ import annotations

import json

from researcher_profiles.schema.manifest import build_manifest

from .factories import build_profile_dir


def _stamp(d, url, digest):
    doc = json.loads((d / "profile.jsonld").read_text())
    for part in doc.get("subjectOf") or []:
        if part["contentUrl"] == url:
            part["inputsDigest"] = digest
    (d / "profile.jsonld").write_text(json.dumps(doc))


def test_digest_is_carried_while_the_file_is_unchanged(tmp_path):
    d = build_profile_dir(tmp_path / "p")
    _stamp(d, "personality/expertise.md", "abc123")
    _, subjects = build_manifest(d)
    by_url = {s.content_url: s for s in subjects}
    assert by_url["personality/expertise.md"].inputs_digest == "abc123"
    assert by_url["personality/SOUL.md"].inputs_digest is None


def test_digest_is_dropped_once_the_file_is_edited(tmp_path):
    d = build_profile_dir(tmp_path / "p")
    _stamp(d, "personality/expertise.md", "abc123")
    (d / "personality" / "expertise.md").write_text("edited by hand\n")
    _, subjects = build_manifest(d)
    by_url = {s.content_url: s for s in subjects}
    assert by_url["personality/expertise.md"].inputs_digest is None
