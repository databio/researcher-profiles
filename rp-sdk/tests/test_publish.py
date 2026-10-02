"""Tests for the profile renderer and static-site builder.

A profile directory is already its published form, so there is no separate
publish transform. These tests exercise the two pieces that do exist:

- ``render_profile``: refreshes one profile folder in place (manifest,
  ``index.html``).
- ``publish_collection``: writes the static tree one audience may see.
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

from .factories import FakeBackend, copy_fixture

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
        # hasEmbeddingIndex tracks the SERVED flat form, not the private
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
# publish_collection: the static tree one audience may see
# ---------------------------------------------------------------------------

_EMAIL = "secret-contact@example.org"
_METHOD = "methodsonlyforgrantedreaders"
_FULLTEXT = "fulltextnobodymaypublish"


def _audience_profile(root: Path, slug: str = "ada", **doc_fields) -> Path:
    """A profile with something at every tier, plus a real local index.

    ``sources/papers/p1.md`` is private (role default), ``sources/grants.jsonld``
    is declared ``limited``, the ``contact`` section (email) is private and the
    ``methods`` section is limited.
    """
    from researcher_profiles.embeddings import SqliteEmbeddingIndex
    from researcher_profiles.profile import ResearcherProfile
    from researcher_profiles.schema import SectionVisibility

    from .factories import build_profile_dir

    pdir = build_profile_dir(root / slug, grants=True, manifest=False)
    (pdir / "sources" / "papers").mkdir(parents=True, exist_ok=True)
    (pdir / "sources" / "papers" / "p1.md").write_text(f"{_FULLTEXT}\n", encoding="utf-8")
    SqliteEmbeddingIndex(pdir).build_index(backend=FakeBackend())
    render_profile(pdir)

    prof = ResearcherProfile.from_files(pdir)
    doc = prof.metadata
    doc.email = _EMAIL
    doc.methodological_commitments = [_METHOD]
    doc.section_visibility = [
        SectionVisibility(section="contact", visibility="private"),
        SectionVisibility(section="methods", visibility="limited"),
    ]
    for part in doc.has_part:
        if part.content_url == "sources/grants.jsonld":
            part.visibility = "limited"
    for name, value in doc_fields.items():
        setattr(doc, name, value)
    prof.save_profile(doc)
    return pdir


def _set_part_tier(pdir: Path, content_url: str, tier: str) -> Path:
    """Declare one manifest artifact of the profile at ``pdir`` at ``tier``."""
    from researcher_profiles.profile import ResearcherProfile

    prof = ResearcherProfile.from_files(pdir)
    doc = prof.metadata
    (part,) = [p for p in [*doc.has_part, *doc.subject_of] if p.content_url == content_url]
    part.visibility = tier
    prof.save_profile(doc)
    return pdir


def _add_private_chunk(pdir: Path) -> None:
    """Put one ``cv`` chunk (private by role default) into the local index.

    The indexer does not chunk a CV, so the row goes in by hand. Its vector
    points far from the rest, so a centroid that includes it is visibly off.
    """
    import numpy as np

    from researcher_profiles.embeddings._sqlite import connect_vec

    conn = connect_vec(cache_dir(pdir) / "embeddings.sqlite")
    dim = len(conn.execute("SELECT embedding FROM chunk_vec LIMIT 1").fetchone()[0]) // 4
    cur = conn.execute(
        "INSERT INTO chunks (source_type, source_id, chunk_index, text, text_hash, "
        "section, char_count, indexed_at) VALUES ('cv', 'cv', 0, 'x', 'x', NULL, 1, '')"
    )
    vec = -np.ones(dim, dtype="<f4") * 50
    conn.execute(
        "INSERT INTO chunk_vec (id, embedding) VALUES (?, ?)", (cur.lastrowid, vec.tobytes())
    )
    conn.commit()
    conn.close()
    for cached in cache_dir(pdir).rglob("*.npz"):
        cached.unlink()


def _tree_text(root: Path) -> str:
    return "\n".join(
        f.read_text(encoding="utf-8", errors="ignore") for f in root.rglob("*") if f.is_file()
    )


class TestPublishCollection:
    @pytest.mark.parametrize(
        "who, shipped, withheld, present, absent",
        [
            (
                "public",
                {"sources/summaries", "embeddings/index.json"},
                {"sources/grants.jsonld", "sources/papers/p1.md"},
                set(),
                {_EMAIL, _METHOD, _FULLTEXT},
            ),
            (
                "limited",
                {"sources/grants.jsonld", "embeddings/index.json"},
                {"sources/papers/p1.md"},
                {_METHOD},
                {_EMAIL, _FULLTEXT},
            ),
        ],
        ids=["public-gets-only-public", "limited-adds-limited-not-private"],
    )
    def test_export_holds_exactly_what_the_audience_may_see(
        self, tmp_path, who, shipped, withheld, present, absent
    ):
        from researcher_profiles.publish import publish_collection

        root = tmp_path / "profiles"
        _audience_profile(root)
        out = tmp_path / "out"
        publish_collection(root, out, viewer=who)

        prof_out = out / "profiles" / "ada"
        for rel in shipped:
            assert (prof_out / rel).exists(), f"{rel} missing at --who {who}"
        for rel in withheld:
            assert not (prof_out / rel).exists(), f"{rel} leaked at --who {who}"
        assert not list(out.rglob(".cache"))
        assert not list(out.rglob("*.sqlite"))

        # The projection reaches inside profile.jsonld and index.html.
        document = (prof_out / "profile.jsonld").read_text(encoding="utf-8")
        for marker in present:
            assert marker in document, f"{marker} missing at --who {who}"
        for name in ("profile.jsonld", "index.html"):
            text = (prof_out / name).read_text(encoding="utf-8")
            for marker in absent:
                assert marker not in text, f"{marker} leaked into {name} at --who {who}"
        everything = _tree_text(out)
        for marker in absent:
            assert marker not in everything

        # Embeddings are exported at the audience's tier: grant, CV and web
        # chunks are private, so neither audience gets them.
        chunks_file = next((prof_out / "embeddings").glob("*.chunks.json"))
        source_types = {c["source_type"] for c in json.loads(chunks_file.read_text())}
        assert source_types
        assert not source_types & {"grant", "cv", "web"}

    @pytest.mark.parametrize(
        "who, listed", [("public", False), ("limited", True)], ids=["public", "limited"]
    )
    def test_a_limited_profile_reaches_only_a_limited_audience(self, tmp_path, who, listed):
        from researcher_profiles.publish import publish_collection

        root = tmp_path / "profiles"
        _audience_profile(root, "ada")
        _audience_profile(root, "held", rid="0000-0002-1825-0097", visibility="limited")
        out = tmp_path / "out"
        result = publish_collection(root, out, viewer=who)

        index = json.loads((out / "index.json").read_text())
        by_rid = json.loads((out / "by-rid.json").read_text())
        assert ("profiles/held/" in index) is listed
        assert ("0000-0002-1825-0097" in by_rid) is listed
        assert (out / "profiles" / "held").exists() is listed
        assert ("held" in result.skipped) is not listed
        if who != "public":
            assert "Disallow: /" in (out / "robots.txt").read_text()
            assert "noindex" in (out / "profiles" / "ada" / "index.html").read_text()

    def test_a_rerun_removes_what_a_tightened_tier_withholds(self, tmp_path):
        from researcher_profiles.profile import ResearcherProfile
        from researcher_profiles.publish import publish_collection

        root = tmp_path / "profiles"
        pdir = _audience_profile(root)
        out = tmp_path / "out"
        publish_collection(root, out)
        summary = next((out / "profiles" / "ada" / "sources" / "summaries").iterdir())
        rel = summary.relative_to(out / "profiles" / "ada").as_posix()

        prof = ResearcherProfile.from_files(pdir)
        doc = prof.metadata
        for part in doc.has_part:
            if part.content_url == rel:
                part.visibility = "limited"
        prof.save_profile(doc)

        result = publish_collection(root, out)
        assert not summary.exists()
        assert f"profiles/ada/{rel}" in result.removed

    def test_refuses_a_folder_it_did_not_write(self, tmp_path):
        from researcher_profiles.publish import PublishError, publish_collection

        root = tmp_path / "profiles"
        _audience_profile(root)
        out = tmp_path / "out"
        out.mkdir()
        (out / "keep.txt").write_text("not ours", encoding="utf-8")
        with pytest.raises(PublishError):
            publish_collection(root, out)
        assert sorted(p.name for p in out.iterdir()) == ["keep.txt"]

    @pytest.mark.parametrize("who", ["public", "limited", "private"])
    def test_viewer_archive_ships_the_same_set_as_the_export(self, tmp_path, who):
        import io
        import tarfile

        from researcher_profiles.api.upload import build_viewer_archive
        from researcher_profiles.publish import plan_profile_export

        pdir = _audience_profile(tmp_path / "profiles")
        plan = plan_profile_export(pdir, who)
        with tarfile.open(fileobj=io.BytesIO(build_viewer_archive(pdir, viewer=who))) as tf:
            members = {m.name for m in tf.getmembers() if m.isfile()}
            document = tf.extractfile("profile.jsonld").read()
        assert members == {"profile.jsonld", *plan.files}
        assert document == plan.document

    def test_cli_refuses_a_broken_derivation_chain(self, tmp_path, capsys):
        from researcher_profiles.cli import main
        from researcher_profiles.profile import ResearcherProfile

        root = tmp_path / "profiles"
        pdir = _audience_profile(root)
        prof = ResearcherProfile.from_files(pdir)
        doc = prof.metadata
        doc.has_part[0].derived_from = ["no-such-artifact"]
        prof.save_profile(doc)

        out = tmp_path / "out"
        assert main(["publish", str(root), "--out", str(out)]) == 1
        assert "derivedFrom" in capsys.readouterr().err
        assert not out.exists()

    def test_cli_dry_run_json_writes_nothing(self, tmp_path, capsys):
        from researcher_profiles.cli import main

        root = tmp_path / "profiles"
        _audience_profile(root)
        out = tmp_path / "out"
        assert main(["publish", str(root), "-o", str(out), "--dry-run", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["who"] == "public"
        (ada,) = payload["profiles"]
        assert "profile.jsonld" in ada["files"]
        assert ada["withheld"]["sources/papers/p1.md"].startswith("private")
        assert not out.exists()

    # ---- embeddings follow their own artifact's tier ------------------------

    @pytest.mark.parametrize("who, ships", [("public", False), ("private", True)])
    def test_withheld_embeddings_do_not_ship(self, tmp_path, who, ships):
        from researcher_profiles.publish import publish_collection

        root = tmp_path / "profiles"
        _set_part_tier(_audience_profile(root), "embeddings/index.json", "private")
        out = tmp_path / "out"
        result = publish_collection(root, out, viewer=who)

        prof_out = out / "profiles" / "ada"
        assert (prof_out / "embeddings").exists() is ships
        doc = json.loads((prof_out / "profile.jsonld").read_text())
        assert doc.get("hasEmbeddingIndex", False) is ships
        (export,) = result.profiles
        assert any(f.startswith("embeddings/") for f in export.files) is ships
        # The collection does not advertise a profile whose vectors it withheld.
        bundle = json.loads((out / "collection.jsonld").read_text())
        assert bool(bundle["artifacts"]) is ships
        if not ships:
            assert not (out / "collection" / "embeddings").exists()

    def test_viewer_archive_drops_the_embedding_flag_with_the_embeddings(self, tmp_path):
        from researcher_profiles.publish import plan_profile_export

        pdir = _set_part_tier(
            _audience_profile(tmp_path / "profiles"), "embeddings/index.json", "private"
        )
        public = json.loads(plan_profile_export(pdir, "public").document)
        private = json.loads(plan_profile_export(pdir, "private").document)
        assert public.get("hasEmbeddingIndex", False) is False
        assert private["hasEmbeddingIndex"] is True

    def test_collection_centroid_is_the_mean_of_what_ships(self, tmp_path):
        import numpy as np

        from researcher_profiles.embeddings.flat import FlatEmbeddingIndex
        from researcher_profiles.profile import ResearcherProfile
        from researcher_profiles.publish import publish_collection

        root = tmp_path / "profiles"
        pdir = _audience_profile(root)
        _add_private_chunk(pdir)
        out = tmp_path / "out"
        publish_collection(root, out)

        shipped = FlatEmbeddingIndex.load(out / "profiles" / "ada" / "embeddings").centroid()
        index = json.loads((out / "collection" / "embeddings" / "index.json").read_text())
        assert index["rows"] == ["ada"]
        blob = (out / "collection" / "embeddings" / index["file"]).read_bytes()
        row = np.frombuffer(blob, dtype="<f4")
        np.testing.assert_allclose(row, shipped, atol=1e-6)
        # The private chunk moves the all-chunk centroid, so equality above is
        # not an accident of every chunk being public.
        everything = ResearcherProfile.from_files(pdir).index.embedding("centroid")
        assert not np.allclose(everything, shipped, atol=1e-4)

    # ---- a profile that will not load stops the publish ----------------------

    def test_a_profile_that_will_not_load_fails_the_publish(self, tmp_path, capsys):
        from researcher_profiles.cli import main
        from researcher_profiles.publish import PublishError, publish_collection

        root = tmp_path / "profiles"
        _audience_profile(root, "ada")
        bad = _audience_profile(root, "bob", rid="0000-0002-1825-0097")
        out = tmp_path / "out"
        publish_collection(root, out)
        assert (out / "profiles" / "bob" / "index.html").is_file()
        before = _tree_snapshot(out)

        doc = json.loads((bad / "profile.jsonld").read_text())
        doc["visibility"] = "restricted"
        (bad / "profile.jsonld").write_text(json.dumps(doc), encoding="utf-8")

        with pytest.raises(PublishError, match="bob"):
            publish_collection(root, out)
        assert _tree_snapshot(out) == before
        assert main(["publish", str(root), "--out", str(out)]) != 0
        assert "bob" in capsys.readouterr().err
        assert _tree_snapshot(out) == before

    # ---- the marker ------------------------------------------------------------

    @pytest.mark.parametrize("marker", ["[]", '{"files": "x"}', '"x"', "{}"])
    def test_refuses_a_marker_of_the_wrong_shape(self, tmp_path, marker):
        from researcher_profiles.publish import MARKER, PublishError, publish_collection

        root = tmp_path / "profiles"
        _audience_profile(root)
        out = tmp_path / "out"
        out.mkdir()
        (out / MARKER).write_text(marker, encoding="utf-8")
        with pytest.raises(PublishError, match="unreadable"):
            publish_collection(root, out)

    def test_a_crash_before_pruning_is_cleaned_up_next_run(self, tmp_path, monkeypatch):
        from researcher_profiles.publish import _publish, publish_collection

        root = tmp_path / "profiles"
        pdir = _audience_profile(root)
        out = tmp_path / "out"
        publish_collection(root, out)
        summary = next((out / "profiles" / "ada" / "sources" / "summaries").iterdir())
        _set_part_tier(pdir, summary.relative_to(out / "profiles" / "ada").as_posix(), "limited")

        real = _publish._remove_stale
        monkeypatch.setattr(_publish, "_remove_stale", _boom)
        with pytest.raises(RuntimeError):
            publish_collection(root, out)
        assert summary.exists()
        monkeypatch.setattr(_publish, "_remove_stale", real)

        publish_collection(root, out)
        assert not summary.exists()

    def test_dry_run_reports_what_it_would_remove(self, tmp_path, capsys):
        from researcher_profiles.cli import main
        from researcher_profiles.publish import publish_collection

        root = tmp_path / "profiles"
        pdir = _audience_profile(root)
        out = tmp_path / "out"
        publish_collection(root, out)
        summary = next((out / "profiles" / "ada" / "sources" / "summaries").iterdir())
        rel = summary.relative_to(out / "profiles" / "ada").as_posix()
        _set_part_tier(pdir, rel, "limited")

        result = publish_collection(root, out, dry_run=True)
        assert result.would_remove == [f"profiles/ada/{rel}"]
        assert result.removed == []
        assert summary.exists()

        assert main(["publish", str(root), "-o", str(out), "--dry-run", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["would_remove"] == [f"profiles/ada/{rel}"]
        assert main(["publish", str(root), "-o", str(out), "--dry-run"]) == 0
        assert f"would remove profiles/ada/{rel}" in capsys.readouterr().out
        assert summary.exists()

    @pytest.mark.parametrize("who, sitemap", [("public", True), ("limited", False)])
    def test_sitemap_only_for_a_public_export(self, tmp_path, who, sitemap):
        from researcher_profiles.publish import publish_collection

        root = tmp_path / "profiles"
        _audience_profile(root)
        out = tmp_path / "out"
        publish_collection(root, out, viewer=who, base_url="https://example.org")
        assert (out / "sitemap.xml").exists() is sitemap
        robots = (out / "robots.txt").read_text()
        assert ("Disallow: /\n" in robots) is not sitemap


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
