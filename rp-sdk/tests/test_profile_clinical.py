"""The ``clinical`` flag on a profile summary (``GET /api/v1/profiles``).

It is computed from the PROJECTED document, so it must never reveal a clinical
section the viewer may not read.
"""

import json

from researcher_profiles import ResearcherProfile
from researcher_profiles.profile.payloads import profile_summary_dict

from .factories import build_profile_dir, refresh_manifest

AREA = {"system": "http://snomed.info/sct", "code": "363346000", "label": "Cancer"}


def _summary(path, viewer="public"):
    return profile_summary_dict(ResearcherProfile.from_files(path), viewer)


def test_therapeutic_areas_make_a_profile_clinical(tmp_path):
    path = build_profile_dir(tmp_path / "doc", therapeutic_areas=[AREA])
    assert _summary(path)["clinical"] is True


def test_a_research_only_profile_is_not_clinical(tmp_path):
    path = build_profile_dir(tmp_path / "res")
    assert _summary(path)["clinical"] is False


def test_site_capabilities_with_content_make_a_profile_clinical(tmp_path):
    path = build_profile_dir(
        tmp_path / "site", site_capabilities={"infrastructure": {"phase_experience": ["II"]}}
    )
    assert _summary(path)["clinical"] is True


def test_an_empty_site_capabilities_block_is_not_clinical(tmp_path):
    path = build_profile_dir(tmp_path / "empty", site_capabilities={})
    assert _summary(path)["clinical"] is False


def test_a_withheld_clinical_section_never_sets_the_flag(tmp_path):
    path = build_profile_dir(
        tmp_path / "held",
        therapeutic_areas=[AREA],
        section_visibility=[{"section": "clinical", "visibility": "private"}],
    )
    assert _summary(path, "public")["clinical"] is False
    assert _summary(path, "limited")["clinical"] is False
    assert _summary(path, "private")["clinical"] is True


def test_a_trials_source_counts_only_where_the_viewer_may_read_it(tmp_path):
    path = build_profile_dir(tmp_path / "trials")
    (path / "sources").mkdir(exist_ok=True)
    (path / "sources" / "trials.jsonld").write_text(
        json.dumps({"@type": "Collection", "hasPart": []}), encoding="utf-8"
    )
    refresh_manifest(path)
    # ``trials`` defaults to ``limited``: the public does not see it.
    assert _summary(path, "public")["clinical"] is False
    assert _summary(path, "limited")["clinical"] is True


def test_the_profile_list_carries_the_flag(tmp_path, make_api_client):
    build_profile_dir(
        tmp_path / "root" / "doc", rid="0000-0002-1825-0097", therapeutic_areas=[AREA]
    )
    build_profile_dir(tmp_path / "root" / "res")
    c = make_api_client(tmp_path / "root")
    entries = {p["slug"]: p for p in c.get("/api/v1/profiles").json()["profiles"]}
    assert entries["doc"]["clinical"] is True
    assert entries["res"]["clinical"] is False
