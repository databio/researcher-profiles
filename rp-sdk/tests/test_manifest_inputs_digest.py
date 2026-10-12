"""A generated artifact's ``inputsDigest`` survives a manifest refresh only while the file is byte-identical."""

from __future__ import annotations

import json

import pytest

from researcher_profiles.schema.manifest import build_manifest

from .factories import build_profile_dir


@pytest.mark.parametrize(
    "edit,expected", [(False, "abc123"), (True, None)], ids=["unchanged", "edited"]
)
def test_digest_is_carried_only_while_the_file_is_unchanged(tmp_path, edit, expected):
    d = build_profile_dir(tmp_path / "p")
    doc = json.loads((d / "profile.jsonld").read_text())
    for part in doc.get("subjectOf") or []:
        if part["contentUrl"] == "personality/expertise.md":
            part["inputsDigest"] = "abc123"
    (d / "profile.jsonld").write_text(json.dumps(doc))
    if edit:
        (d / "personality" / "expertise.md").write_text("edited by hand\n")
    _, subjects = build_manifest(d)
    by_url = {s.content_url: s for s in subjects}
    assert by_url["personality/expertise.md"].inputs_digest == expected
    assert by_url["personality/SOUL.md"].inputs_digest is None
