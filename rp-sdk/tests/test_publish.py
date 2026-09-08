"""Tests for the profile renderer and static-site builder.

A profile directory is already its published form, so there is no separate
publish transform. These tests exercise the two pieces that do exist:

- ``render_profile``: refreshes one profile folder in place (manifest,
  ``index.html``, ``.publishignore``).
- ``build_site``: writes the collection files describing a set of profiles.

Plus the framework-free helpers reused by both (markdown, hosting configs).
"""

import json
import shutil
from pathlib import Path

import pytest

from researcher_profiles.publish import build_site, render_profile
from researcher_profiles.publish._hosting import (
    cloudflare_headers,
    robots_txt,
    sitemap_xml,
    well_known_json,
)
from researcher_profiles.publish._jsonld import grant_node, paper_node
from researcher_profiles.publish._markdown import md_to_html
from researcher_profiles.schema import GrantRecord, PaperRecord
from researcher_profiles.utils.paths import cache_dir

from .factories import FakeBackend, copy_fixture, sync_with_publishignore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def profiles_root(fixture_profiles_root) -> Path:
    """A profiles root holding two real fixture profiles."""
    return fixture_profiles_root("jane-doe", "john-smith")


# ---------------------------------------------------------------------------
# render_profile
# ---------------------------------------------------------------------------


class TestRenderProfile:
    def test_writes_index_html(self, jane_doe_dir):
        render_profile(jane_doe_dir)
        html = (jane_doe_dir / "index.html").read_text()
        assert "<!DOCTYPE html>" in html
        assert "Jane A. Doe" in html

    def test_refreshes_manifest_into_profile_jsonld(self, jane_doe_dir):
        render_profile(jane_doe_dir)
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())

        has_part = {p["contentUrl"]: p for p in doc["hasPart"]}
        subject_of = {p["contentUrl"]: p for p in doc["subjectOf"]}

        # Persona docs are subjectOf.
        assert "personality/SOUL.md" in subject_of
        assert "personality/expertise.md" in subject_of
        assert subject_of["personality/SOUL.md"]["role"] == "soul"

        # Record parts are hasPart.
        assert "sources/papers.jsonld" in has_part
        assert has_part["sources/papers.jsonld"]["role"] == "works"
        assert "sources/citations.json" in has_part

        # A paper summary is enumerated with its paperId.
        summary_parts = [p for p in doc["hasPart"] if p.get("role") == "paper_summary"]
        assert summary_parts
        assert all(p.get("paperId") for p in summary_parts)

    def test_index_html_in_manifest_with_relative_url(self, jane_doe_dir):
        render_profile(jane_doe_dir)
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        parts = doc["hasPart"]

        html_entry = next(p for p in parts if p.get("role") == "html")
        assert html_entry["contentUrl"] == "index.html"
        assert html_entry["encodingFormat"] == "text/html"

        # The external consumer-skill doc is not a manifest entry: manifest
        # contentUrls are relative paths to the profile's own files.
        assert not any(p.get("role") == "consumer_skill" for p in parts)
        for p in [*parts, *doc.get("subjectOf", [])]:
            assert "://" not in p["contentUrl"], "manifest contentUrls must be relative"

    def test_consumer_flags_recorded(self, jane_doe_dir):
        render_profile(jane_doe_dir)
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        # jane-doe ships a citations.json but no embedding index.
        assert doc["hasCitationGraph"] is True
        assert doc["hasEmbeddingIndex"] is False
        # Her expertise.md cites published paper ids like [doe2016example].
        assert doc["expertiseCitesPaperIds"] is True

    def test_embedding_flag_true_iff_flat_form_exists(self, jane_doe_dir):
        # hasEmbeddingIndex tracks the SERVED flat form, not the restricted
        # sqlite. Building the sqlite and rendering self-heals the flat form.
        from researcher_profiles.embeddings import SqliteEmbeddingIndex

        SqliteEmbeddingIndex(jane_doe_dir).build_index(backend=FakeBackend())
        render_profile(jane_doe_dir)
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        assert (jane_doe_dir / "embeddings" / "index.json").is_file()
        assert doc["hasEmbeddingIndex"] is True

    def test_embedding_flag_false_without_flat_form(self, jane_doe_dir):
        # A placeholder sqlite that is not a real index yields no flat form,
        # so the profile does not advertise embeddings.
        cache_dir(jane_doe_dir).mkdir(exist_ok=True)
        (cache_dir(jane_doe_dir) / "embeddings.sqlite").write_bytes(b"SQLite")
        render_profile(jane_doe_dir)
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        assert not (jane_doe_dir / "embeddings" / "index.json").is_file()
        assert doc["hasEmbeddingIndex"] is False

    def test_citation_flag_false_without_citations(self, jane_doe_dir):
        (jane_doe_dir / "sources" / "citations.json").unlink()
        render_profile(jane_doe_dir)
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        assert doc["hasCitationGraph"] is False

    def test_no_index_meta_tag(self, jane_doe_dir):
        render_profile(jane_doe_dir, no_index=True)
        html = (jane_doe_dir / "index.html").read_text()
        assert 'name="robots" content="noindex"' in html

    def test_rerender_is_idempotent(self, jane_doe_dir):
        render_profile(jane_doe_dir)
        first = (jane_doe_dir / "profile.jsonld").read_text()
        render_profile(jane_doe_dir)
        assert (jane_doe_dir / "profile.jsonld").read_text() == first

    def test_render_stamps_date_modified(self, jane_doe_dir):
        render_profile(jane_doe_dir)
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        assert "dateModified" in doc
        first_stamp = doc["dateModified"]
        render_profile(jane_doe_dir)
        doc2 = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        assert doc2["dateModified"] == first_stamp

    def test_render_advances_stamp_on_consumer_flag_change(self, jane_doe_dir):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch

        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = t1 + timedelta(days=90)
        with patch("researcher_profiles.utils.clock.datetime") as mock_dt:
            mock_dt.now.return_value = t1
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            render_profile(jane_doe_dir)
        doc = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        first_stamp = doc["dateModified"]
        assert doc["hasCitationGraph"] is True
        (jane_doe_dir / "sources" / "citations.json").unlink()
        with patch("researcher_profiles.utils.clock.datetime") as mock_dt:
            mock_dt.now.return_value = t2
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            render_profile(jane_doe_dir)
        doc2 = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        assert doc2["hasCitationGraph"] is False
        assert doc2["dateModified"] != first_stamp

    def test_missing_profile_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            render_profile(tmp_path / "nope")


# ---------------------------------------------------------------------------
# .publishignore
# ---------------------------------------------------------------------------


class TestPublishIgnore:
    def _lines(self, profile_dir: Path) -> list[str]:
        """The exclude PATTERNS only.

        Comments are allowed to name concepts (e.g. a ``# personality/`` note),
        so they are never patterns and are dropped here.
        """
        render_profile(profile_dir)
        body = (profile_dir / ".publishignore").read_text()
        return [
            ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")
        ]

    @pytest.mark.parametrize(
        "pattern, present",
        [
            (".cache/", True),
            ("personality/", False),
            ("sources/summaries/", False),
            ("sources/papers.jsonld", False),
            ("profile.jsonld", False),
        ],
        ids=[
            "cache-excluded",
            "personality-public",
            "summaries-public",
            "papers-jsonld-public",
            "profile-jsonld-public",
        ],
    )
    def test_pattern_presence(self, jane_doe_dir, pattern, present):
        assert (pattern in self._lines(jane_doe_dir)) is present

    def test_full_text_papers_excluded(self, jane_doe_dir):
        # jane-doe carries sources/papers/*.md (restricted, copyright). The
        # manifest lists each full-text file, so each is excluded by path.
        assert (jane_doe_dir / "sources" / "papers").is_dir()
        lines = self._lines(jane_doe_dir)
        fulltext = [ln for ln in lines if ln.startswith("sources/papers/")]
        assert fulltext, "expected full-text papers to be excluded by path"

    def test_local_sqlite_index_is_never_served(self, jane_doe_dir):
        # .cache/embeddings.sqlite is a derived index (tier restricted): it stays
        # under the excluded .cache/ prefix and is not re-included. The servable
        # embeddings are the flat embeddings/ files.
        cache_dir(jane_doe_dir).mkdir(exist_ok=True)
        (cache_dir(jane_doe_dir) / "embeddings.sqlite").write_bytes(b"SQLite")
        lines = self._lines(jane_doe_dir)
        assert ".cache/" in lines
        assert not any(ln.startswith("!") for ln in lines)


# ---------------------------------------------------------------------------
# End-to-end: syncing with only .publishignore drops all restricted content
# (plan step 19c: the invariant the tiered layout buys us)
# ---------------------------------------------------------------------------


class TestPublishIgnoreEndToEnd:
    def test_synced_tree_has_no_restricted_content(self, jane_doe_dir):
        from researcher_profiles.privacy import effective_tiers
        from researcher_profiles.profile import ResearcherProfile

        render_profile(jane_doe_dir)
        # A restricted local index must exist to make the test meaningful.
        cache_dir(jane_doe_dir).mkdir(exist_ok=True)
        (cache_dir(jane_doe_dir) / "embeddings.sqlite").write_bytes(b"SQLite")

        dst = jane_doe_dir.parent / "published"
        sync_with_publishignore(jane_doe_dir, dst)

        prof = ResearcherProfile.from_files(jane_doe_dir)
        eff = effective_tiers(prof.metadata)

        # Every artifact above `public` is absent from the synced tree.
        for url, tier in eff.items():
            if tier != "public":
                assert not (dst / url).exists(), f"{url} ({tier}) leaked into sync"

        # And the always-restricted directories never appear at all.
        assert not (dst / ".cache").exists()
        assert not (dst / ".keys").exists()
        assert not (dst / ".publishignore").exists()
        assert not list(dst.glob("sources/papers/*.md"))

        # Sanity: the sync is not trivially empty; public content survives.
        assert (dst / "profile.jsonld").is_file()
        assert (dst / "sources" / "papers.jsonld").is_file()


# ---------------------------------------------------------------------------
# build_site
# ---------------------------------------------------------------------------


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under ``root``: file bytes, or ``None`` for a directory."""
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in root.rglob("*")
    }


def _boom(*args, **kwargs):
    raise RuntimeError("writer exploded mid-build")


class TestBuildSite:
    @pytest.fixture(scope="class")
    @classmethod
    def built_site(cls, tmp_path_factory) -> Path:
        """One ``build_site`` run over the two-profile root, shared by the
        read-only assertions below.

        Class-scoped, so it cannot use ``fixture_profiles_root`` (function-scoped
        via ``tmp_path``) and copies the fixtures itself. Tests that pass different
        arguments or assert on the return value build their own.
        """
        root = tmp_path_factory.mktemp("build-site-root")
        for slug in ("jane-doe", "john-smith"):
            copy_fixture(slug, root)
        out = tmp_path_factory.mktemp("build-site-out") / "site"
        build_site(root, out)
        return out

    def test_writes_collection_files(self, built_site):
        for rel in [
            "index.json",
            "index.jsonld",
            "by-rid.json",
            "SKILL.md",
            "style.css",
            "_headers",
            ".well-known/researcher-profiles.json",
            "context/v1.jsonld",
        ]:
            assert (built_site / rel).is_file(), f"missing collection file: {rel}"

    def test_does_not_copy_profile_folders(self, built_site):
        # build_site emits collection files only; folders ship via rsync.
        assert not (built_site / "profiles").exists()

    def test_index_json_lists_profiles(self, built_site):
        urls = json.loads((built_site / "index.json").read_text())
        assert "profiles/jane-doe/" in urls
        assert "profiles/john-smith/" in urls

    def test_by_rid_maps_identity_to_profile(self, built_site):
        by_rid = json.loads((built_site / "by-rid.json").read_text())
        assert "0000-0002-1825-0097" in by_rid
        assert by_rid["0000-0002-1825-0097"] == "profiles/jane-doe/profile.jsonld"

    def test_sitemap_and_robots_with_base_url(self, profiles_root, tmp_path):
        out = tmp_path / "site"
        build_site(profiles_root, out, base_url="https://profiles.example.com")
        assert (out / "sitemap.xml").is_file()
        assert (out / "robots.txt").is_file()
        assert "profiles.example.com" in (out / "sitemap.xml").read_text()

    def test_no_sitemap_without_base_url(self, profiles_root, tmp_path):
        out = tmp_path / "site"
        result = build_site(profiles_root, out)
        assert not (out / "sitemap.xml").is_file()
        assert any("base_url" in w for w in result.warnings)

    def test_deterministic_with_pinned_now(self, profiles_root, tmp_path):
        ts = "2026-01-15T00:00:00+00:00"
        out1 = tmp_path / "s1"
        out2 = tmp_path / "s2"
        build_site(profiles_root, out1, base_url="https://x.example", now=ts)
        build_site(profiles_root, out2, base_url="https://x.example", now=ts)
        files1 = sorted(f.relative_to(out1) for f in out1.rglob("*") if f.is_file())
        files2 = sorted(f.relative_to(out2) for f in out2.rglob("*") if f.is_file())
        assert files1 == files2
        for rel in files1:
            assert (out1 / rel).read_bytes() == (out2 / rel).read_bytes()

    def test_missing_root_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            build_site(tmp_path / "nope", tmp_path / "out")

    def test_failed_build_leaves_output_unchanged(self, profiles_root, tmp_path, monkeypatch):
        """A build that raises must not disturb the site already served from ``out``.

        Everything is staged off to one side and only moved in once the whole
        build has succeeded, so a mid-build failure is invisible to readers.
        """
        import researcher_profiles.publish as publish

        out = tmp_path / "site"
        build_site(profiles_root, out)
        before = _tree_snapshot(out)

        # Change the inputs so a clobbered index.json would be detectable, then
        # blow up on SKILL.md, after index.json and friends have been written.
        shutil.rmtree(profiles_root / "john-smith")
        monkeypatch.setattr(publish._site, "generate_skill_md", _boom)

        with pytest.raises(RuntimeError, match="exploded"):
            build_site(profiles_root, out)

        assert _tree_snapshot(out) == before

    def test_failed_build_does_not_leave_an_output_dir_behind(
        self, profiles_root, tmp_path, monkeypatch
    ):
        import researcher_profiles.publish as publish

        monkeypatch.setattr(publish._site, "generate_skill_md", _boom)
        out = tmp_path / "fresh" / "site"
        with pytest.raises(RuntimeError, match="exploded"):
            build_site(profiles_root, out)
        assert not (tmp_path / "fresh").exists()

    def test_preserves_sibling_trees_it_does_not_own(self, profiles_root, tmp_path):
        """``profiles/`` and ``app/`` are rsynced in by the deploy script.

        ``build_site`` owns only the collection files, so it must never swap or
        clear the whole output directory.
        """
        out = tmp_path / "site"
        (out / "profiles" / "jane-doe").mkdir(parents=True)
        (out / "profiles" / "jane-doe" / "index.html").write_text("rsynced profile page")
        (out / "app").mkdir()
        (out / "app" / "index.html").write_text("explorer build")

        build_site(profiles_root, out, base_url="https://profiles.example.com")

        assert (out / "profiles" / "jane-doe" / "index.html").read_text() == "rsynced profile page"
        assert (out / "app" / "index.html").read_text() == "explorer build"
        assert (out / "index.json").is_file()


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


class TestMarkdownRenderer:
    @pytest.mark.parametrize(
        "source, expected",
        [
            ("# Hello", ["<h1>Hello</h1>"]),
            ("**bold**", ["<strong>bold</strong>"]),
            (
                "[click](http://example.com)",
                ['href="http://example.com"', ">click</a>"],
            ),
            ("- item1\n- item2", ["<ul>", "<li>item1</li>"]),
        ],
        ids=["heading", "bold", "link", "list"],
    )
    def test_renders(self, source, expected):
        result = md_to_html(source)
        for fragment in expected:
            assert fragment in result

    def test_empty(self):
        assert md_to_html("") == ""

    @pytest.mark.parametrize(
        "source, expected",
        [
            ("a <b> & c", "<p>a &lt;b&gt; &amp; c</p>"),
            ("# R&D <lab>", "<h1>R&amp;D &lt;lab&gt;</h1>"),
            ("- <script>x</script>", "<ul><li>&lt;script&gt;x&lt;/script&gt;</li></ul>"),
            ("`a < b`", "<p><code>a &lt; b</code></p>"),
            (
                '[x](http://e.com/?a=1&b="2")',
                '<p><a href="http://e.com/?a=1&amp;b=&quot;2&quot;">x</a></p>',
            ),
            ("**x & y**", "<p><strong>x &amp; y</strong></p>"),
        ],
        ids=["paragraph", "heading", "list", "code", "link", "bold"],
    )
    def test_escapes_html_in_plain_text(self, source, expected):
        assert md_to_html(source) == expected


# ---------------------------------------------------------------------------
# Embedded JSON-LD graph
# ---------------------------------------------------------------------------


class TestEmbeddedGraphNodes:
    """The index.html graph carries the same ids and terms as the collections."""

    def test_paper_fragment_matches_papers_jsonld(self):
        paper = PaperRecord(title="T", paper_id="doe2020x")
        assert paper_node(paper)["@id"] == paper.resolve_id() == "#paper/doe2020x"

    def test_paper_doi_wins_over_fragment(self):
        paper = PaperRecord(title="T", paper_id="doe2020x", doi="https://doi.org/10.1/ABC")
        node = paper_node(paper)
        assert node["@id"] == "https://doi.org/10.1/ABC"
        assert node["identifier"] == "https://doi.org/10.1/ABC"

    def test_grant_node_uses_collection_terms(self):
        grant = GrantRecord(id="nih-r01", name="Atlas", role="pi", status="funded")
        node = grant_node(grant)
        assert node["@id"] == "#grant/nih-r01"
        assert node["role"] == "pi"
        assert node["status"] == "funded"
        assert "rp:role" not in node and "rp:status" not in node

    def test_rendered_page_embeds_collection_ids(self, jane_doe_dir):
        render_profile(jane_doe_dir)
        html = (jane_doe_dir / "index.html").read_text()
        assert "#paper/" in html
        assert "#paper-" not in html


# ---------------------------------------------------------------------------
# Hosting configs
# ---------------------------------------------------------------------------


class TestHostingConfigs:
    def test_headers_has_cors(self):
        assert "Access-Control-Allow-Origin: *" in cloudflare_headers()

    @pytest.mark.parametrize(
        "no_index, expected",
        [(True, "Disallow: /"), (False, "Allow: /")],
        ids=["no-index", "allow"],
    )
    def test_robots(self, no_index, expected):
        assert expected in robots_txt(no_index=no_index)

    def test_sitemap(self):
        s = sitemap_xml(["alice", "bob"], base_url="https://example.com")
        assert "example.com/profiles/alice/index.html" in s
        assert "example.com/profiles/bob/index.html" in s

    def test_well_known_json(self):
        data = json.loads(well_known_json(base_url="https://example.com"))
        assert data["version"] == 1
        assert data["base_url"] == "https://example.com"
