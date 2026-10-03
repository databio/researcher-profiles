"""Bounded text reads (``api/_text.py``): ``.../papers/{id}/text`` and ``.../text``.

Pins the paging contract: whole up to 80K chars; above that a first chunk
flagged ``has_more`` whose ``next_offset`` read continues exactly where it
stopped and reaches the end; ``section=`` reads one heading's span; offsets
are indices into the whole stored text.
"""

import pytest

from .factories import refresh_manifest

SLUG = "jane-doe"
PID = "doe2016example"
PARA = "Region sets were tested for overlap with a reference database. " * 12 + "\n\n"


def _long_text() -> str:
    parts = ["# Title\n\n", PARA * 5, "## Methods\n\n", PARA * 300, "## Results\n\n", PARA * 50]
    return "".join(parts)


@pytest.fixture
def client(make_api_client, fixture_profiles_root):
    root = fixture_profiles_root(SLUG)
    c = make_api_client(root)
    c.app.state.viewer_resolver = lambda request, slug: "private"
    c.profile_dir = root / SLUG
    return c


def _text(c, **params):
    r = c.get(f"/api/v1/profiles/{SLUG}/papers/{PID}/text", params=params)
    assert r.status_code == 200, r.text
    return r.json()


class TestPaperText:
    def test_a_short_text_comes_back_whole(self, client):
        stored = (client.profile_dir / "sources" / "papers" / f"{PID}.md").read_text()
        page = _text(client)
        assert page["text"] == stored
        assert (page["has_more"], page["returned_chars"]) == (False, len(stored))
        assert "next_offset" not in page

    def test_a_long_text_is_flagged_and_the_next_call_reaches_the_end(self, client):
        stored = _long_text()
        (client.profile_dir / "sources" / "papers" / f"{PID}.md").write_text(stored)
        refresh_manifest(client.profile_dir)
        page = _text(client)
        assert page["has_more"] is True
        assert page["total_chars"] == len(stored)
        assert page["returned_chars"] <= 80_000
        assert page["text"].endswith("\n\n")  # cut at a paragraph
        got = page["text"]
        while page["has_more"]:
            assert page["next_offset"] == page["offset"] + page["returned_chars"]
            page = _text(client, offset=page["next_offset"])
            got += page["text"]
        assert got == stored
        assert [s["name"] for s in page["sections"]] == ["Title", "Methods", "Results"]

    def test_section_reads_one_heading_and_offsets_stay_absolute(self, client):
        stored = _long_text()
        (client.profile_dir / "sources" / "papers" / f"{PID}.md").write_text(stored)
        refresh_manifest(client.profile_dir)
        page = _text(client, section="Results")
        start = stored.index("## Results")
        assert page["offset"] == start
        assert page["text"] == stored[start:]
        # A later offset inside the section starts there, and still stops at its end.
        mid = start + 1000
        assert _text(client, section="results", offset=mid)["text"] == stored[mid:]

    def test_an_unknown_section_lists_the_valid_ones(self, client):
        r = client.get(f"/api/v1/profiles/{SLUG}/papers/{PID}/text", params={"section": "Nope"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["error"] == "unknown_section"
        assert detail["valid"] and "Nope" not in detail["valid"]

    def test_max_chars_is_clamped(self, client):
        page = _text(client, max_chars=10)
        assert page["returned_chars"] <= 10 and page["has_more"] is True


class TestNarrativeText:
    def test_both_parts_joined_with_the_version(self, client):
        r = client.get(f"/api/v1/profiles/{SLUG}/text")
        assert r.status_code == 200, r.text
        page = r.json()
        assert page["text"].startswith("# Soul\n\n")
        assert [s["name"] for s in page["sections"]] == ["soul", "expertise"]
        detail = client.get(f"/api/v1/profiles/{SLUG}").json()
        assert page["content_hash"] == detail["content_hash"]

    def test_one_part_is_the_stored_text_alone(self, client):
        soul = (client.profile_dir / "personality" / "SOUL.md").read_text()
        page = client.get(f"/api/v1/profiles/{SLUG}/text", params={"section": "soul"}).json()
        assert page["text"] == soul
        assert page["section"] == "soul"

    def test_an_unknown_part_is_a_400(self, client):
        r = client.get(f"/api/v1/profiles/{SLUG}/text", params={"section": "cv"})
        assert r.status_code == 400
        assert r.json()["detail"]["valid"] == ["soul", "expertise"]
