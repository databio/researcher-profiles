"""Tests for the profile renderer and static-site builder.

A profile directory is already its published form. These tests cover the pieces that exist:
``render_profile`` (refresh one folder in place), ``publish_collection`` (the tree one audience
may see), ``build_site`` (the collection files), and the framework-free helpers they share.
"""

import io
import json
import shutil
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

import researcher_profiles.publish as publish
from researcher_profiles.api.upload import build_viewer_archive
from researcher_profiles.cli import build_parser, main
from researcher_profiles.embeddings import SqliteEmbeddingIndex
from researcher_profiles.embeddings._sqlite import connect_vec
from researcher_profiles.embeddings.flat import FlatEmbeddingIndex
from researcher_profiles.profile import ResearcherProfile
from researcher_profiles.publish import (
    MARKER,
    PublishError,
    _publish,
    build_site,
    plan_profile_export,
    publish_collection,
    render_profile,
)
from researcher_profiles.publish._hosting import (
    cloudflare_headers,
    robots_txt,
    sitemap_xml,
    well_known_json,
)
from researcher_profiles.publish._jsonld import grant_node, paper_node
from researcher_profiles.publish._markdown import md_to_html
from researcher_profiles.schema import GrantRecord, PaperRecord, SectionVisibility
from researcher_profiles.utils.paths import cache_dir

from .factories import FakeBackend, build_profile_dir, copy_fixture


@pytest.fixture
def profiles_root(fixture_profiles_root) -> Path:
    """A profiles root holding two real fixture profiles."""
    return fixture_profiles_root("jane-doe", "john-smith")


def _read_doc(pdir: Path) -> dict:
    return json.loads((pdir / "profile.jsonld").read_text())


class TestRenderProfile:
    @pytest.fixture(scope="class")
    @classmethod
    def rendered(cls, tmp_path_factory):
        """jane-doe rendered once; read-only tests share it. Tests that mutate use ``jane_doe_dir``."""
        pdir = copy_fixture("jane-doe", tmp_path_factory.mktemp("render"))
        render_profile(pdir)
        return SimpleNamespace(
            dir=pdir, doc=_read_doc(pdir), html=(pdir / "index.html").read_text()
        )

    @pytest.mark.parametrize(
        "fragment,present",
        [("<!DOCTYPE html>", True), ("Jane A. Doe", True), ("#paper/", True), ("#paper-", False)],
    )
    def test_index_html(self, rendered, fragment, present):
        assert (fragment in rendered.html) is present

    def test_refreshes_manifest_into_profile_jsonld(self, rendered):
        doc = rendered.doc
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

    def test_index_html_in_manifest_with_relative_url(self, rendered):
        doc = rendered.doc
        parts = doc["hasPart"]

        html_entry = next(p for p in parts if p.get("role") == "html")
        assert html_entry["contentUrl"] == "index.html"
        assert html_entry["encodingFormat"] == "text/html"

        # The external consumer-skill doc is not a manifest entry: contentUrls are relative.
        assert not any(p.get("role") == "consumer_skill" for p in parts)
        for p in [*parts, *doc.get("subjectOf", [])]:
            assert "://" not in p["contentUrl"], "manifest contentUrls must be relative"

    @pytest.mark.parametrize(
        "key,expected",
        [
            ("hasCitationGraph", True),
            ("hasEmbeddingIndex", False),
            ("expertiseCitesPaperIds", True),
        ],
        ids=["citations.json-shipped", "no-embedding-index", "expertise-cites-ids"],
    )
    def test_consumer_flags_recorded(self, rendered, key, expected):
        assert rendered.doc[key] is expected

    def test_embedding_flag_true_iff_flat_form_exists(self, jane_doe_dir):
        # hasEmbeddingIndex tracks the SERVED flat form, not the private sqlite; rendering self-heals it.
        SqliteEmbeddingIndex(jane_doe_dir).build_index(backend=FakeBackend())
        render_profile(jane_doe_dir)
        doc = _read_doc(jane_doe_dir)
        assert (jane_doe_dir / "embeddings" / "index.json").is_file()
        assert doc["hasEmbeddingIndex"] is True

    def test_embedding_flag_false_without_flat_form(self, jane_doe_dir):
        # A placeholder sqlite that is not a real index yields no flat form: no embeddings advertised.
        cache_dir(jane_doe_dir).mkdir(exist_ok=True)
        (cache_dir(jane_doe_dir) / "embeddings.sqlite").write_bytes(b"SQLite")
        render_profile(jane_doe_dir)
        doc = _read_doc(jane_doe_dir)
        assert not (jane_doe_dir / "embeddings" / "index.json").is_file()
        assert doc["hasEmbeddingIndex"] is False

    def test_no_index_meta_tag(self, jane_doe_dir):
        render_profile(jane_doe_dir, no_index=True)
        html = (jane_doe_dir / "index.html").read_text()
        assert 'name="robots" content="noindex"' in html

    def test_rerender_is_idempotent_and_stamps_date_modified(self, jane_doe_dir):
        render_profile(jane_doe_dir)
        first = (jane_doe_dir / "profile.jsonld").read_text()
        assert "dateModified" in json.loads(first)
        render_profile(jane_doe_dir)
        assert (jane_doe_dir / "profile.jsonld").read_text() == first

    def test_render_advances_stamp_on_consumer_flag_change(self, jane_doe_dir):
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def render_at(when):
            with patch("researcher_profiles.utils.clock.datetime") as mock_dt:
                mock_dt.now.return_value = when
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                render_profile(jane_doe_dir)
            return _read_doc(jane_doe_dir)

        doc = render_at(t1)
        assert doc["hasCitationGraph"] is True
        (jane_doe_dir / "sources" / "citations.json").unlink()
        doc2 = render_at(t1 + timedelta(days=90))
        assert doc2["hasCitationGraph"] is False
        assert doc2["dateModified"] != doc["dateModified"]

    def test_missing_profile_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            render_profile(tmp_path / "nope")


_EMAIL = "secret-contact@example.org"
_METHOD = "methodsonlyforgrantedreaders"
_FULLTEXT = "fulltextnobodymaypublish"


def _audience_profile(root: Path, slug: str = "ada", **doc_fields) -> Path:
    """A profile with something at every tier, plus a real local index.

    ``sources/papers/p1.md`` is private (role default), ``sources/grants.jsonld`` is declared
    ``limited``, the ``contact`` section (email) is private and ``methods`` is limited.
    """
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
    prof = ResearcherProfile.from_files(pdir)
    doc = prof.metadata
    (part,) = [p for p in [*doc.has_part, *doc.subject_of] if p.content_url == content_url]
    part.visibility = tier
    prof.save_profile(doc)
    return pdir


def _add_private_chunk(pdir: Path) -> None:
    """Put one ``cv`` chunk (private by role) into the index; its far-off vector skews any centroid including it."""
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


@pytest.fixture(scope="module")
def audience_template(tmp_path_factory) -> Path:
    """The default ``_audience_profile``, built once; tests copy it rather than rebuild."""
    return _audience_profile(tmp_path_factory.mktemp("audience"))


@pytest.fixture
def audience_root(tmp_path, audience_template) -> Path:
    """A fresh profiles root holding a copy of the default ``ada`` audience profile."""
    root = tmp_path / "profiles"
    shutil.copytree(audience_template, root / "ada")
    return root


def _first_summary(out: Path) -> tuple[Path, str]:
    summary = next((out / "profiles" / "ada" / "sources" / "summaries").iterdir())
    return summary, summary.relative_to(out / "profiles" / "ada").as_posix()


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under ``root``: file bytes, or ``None`` for a directory."""
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in root.rglob("*")
    }


def _boom(*args, **kwargs):
    raise RuntimeError("writer exploded mid-build")


class TestPublishCollection:
    @pytest.mark.parametrize(
        "who, shipped, withheld, present, absent, listed",
        [
            (
                "public",
                {"sources/summaries", "embeddings/index.json"},
                {"sources/grants.jsonld", "sources/papers/p1.md"},
                set(),
                {_EMAIL, _METHOD, _FULLTEXT},
                False,
            ),
            (
                "limited",
                {"sources/grants.jsonld", "embeddings/index.json"},
                {"sources/papers/p1.md"},
                {_METHOD},
                {_EMAIL, _FULLTEXT},
                True,
            ),
        ],
        ids=["public-gets-only-public", "limited-adds-limited-not-private"],
    )
    def test_export_holds_exactly_what_the_audience_may_see(
        self, tmp_path, audience_root, who, shipped, withheld, present, absent, listed
    ):
        root = audience_root
        _audience_profile(root, "held", rid="0000-0002-1825-0097", visibility="limited")
        out = tmp_path / "out"
        result = publish_collection(root, out, viewer=who)

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

        # Embeddings are exported at the audience's tier: grant, CV and web chunks are
        # private, so neither audience gets them.
        chunks_file = next((prof_out / "embeddings").glob("*.chunks.json"))
        source_types = {c["source_type"] for c in json.loads(chunks_file.read_text())}
        assert source_types
        assert not source_types & {"grant", "cv", "web"}

        # A limited profile reaches only a limited audience.
        index = json.loads((out / "index.json").read_text())
        by_rid = json.loads((out / "by-rid.json").read_text())
        assert ("profiles/held/" in index) is listed
        assert ("0000-0002-1825-0097" in by_rid) is listed
        assert (out / "profiles" / "held").exists() is listed
        assert ("held" in result.skipped) is not listed
        if who != "public":
            assert "Disallow: /" in (out / "robots.txt").read_text()
            assert "noindex" in (prof_out / "index.html").read_text()

    def test_refuses_a_folder_it_did_not_write(self, tmp_path, audience_root):
        out = tmp_path / "out"
        out.mkdir()
        (out / "keep.txt").write_text("not ours", encoding="utf-8")
        with pytest.raises(PublishError):
            publish_collection(audience_root, out)
        assert sorted(p.name for p in out.iterdir()) == ["keep.txt"]

    @pytest.mark.parametrize("who", ["public", "limited", "private"])
    def test_viewer_archive_ships_the_same_set_as_the_export(self, audience_root, who):
        pdir = audience_root / "ada"
        plan = plan_profile_export(pdir, who)
        with tarfile.open(fileobj=io.BytesIO(build_viewer_archive(pdir, viewer=who))) as tf:
            members = {m.name for m in tf.getmembers() if m.isfile()}
            document = tf.extractfile("profile.jsonld").read()
        assert members == {"profile.jsonld", *plan.files}
        assert document == plan.document

    def test_cli_refuses_a_broken_derivation_chain(self, tmp_path, audience_root, capsys):
        prof = ResearcherProfile.from_files(audience_root / "ada")
        doc = prof.metadata
        doc.has_part[0].derived_from = ["no-such-artifact"]
        prof.save_profile(doc)

        out = tmp_path / "out"
        assert main(["publish", str(audience_root), "--out", str(out)]) == 1
        assert "derivedFrom" in capsys.readouterr().err
        assert not out.exists()

    def test_cli_dry_run_json_writes_nothing(self, tmp_path, audience_root, capsys):
        out = tmp_path / "out"
        assert main(["publish", str(audience_root), "-o", str(out), "--dry-run", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["who"] == "public"
        (ada,) = payload["profiles"]
        assert "profile.jsonld" in ada["files"]
        assert ada["withheld"]["sources/papers/p1.md"].startswith("private")
        assert not out.exists()

    # embeddings follow their own artifact's tier

    @pytest.mark.parametrize("who, ships", [("public", False), ("private", True)])
    def test_withheld_embeddings_do_not_ship(self, tmp_path, audience_root, who, ships):
        _set_part_tier(audience_root / "ada", "embeddings/index.json", "private")
        out = tmp_path / "out"
        result = publish_collection(audience_root, out, viewer=who)

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

    def test_viewer_archive_drops_the_embedding_flag_with_the_embeddings(self, audience_root):
        pdir = _set_part_tier(audience_root / "ada", "embeddings/index.json", "private")
        public = json.loads(plan_profile_export(pdir, "public").document)
        private = json.loads(plan_profile_export(pdir, "private").document)
        assert public.get("hasEmbeddingIndex", False) is False
        assert private["hasEmbeddingIndex"] is True

    def test_collection_centroid_is_the_mean_of_what_ships(self, tmp_path, audience_root):
        pdir = audience_root / "ada"
        _add_private_chunk(pdir)
        out = tmp_path / "out"
        publish_collection(audience_root, out)

        shipped = FlatEmbeddingIndex.load(out / "profiles" / "ada" / "embeddings").centroid()
        index = json.loads((out / "collection" / "embeddings" / "index.json").read_text())
        assert index["rows"] == ["ada"]
        blob = (out / "collection" / "embeddings" / index["file"]).read_bytes()
        row = np.frombuffer(blob, dtype="<f4")
        np.testing.assert_allclose(row, shipped, atol=1e-6)
        # The private chunk moves the all-chunk centroid, so equality above is not an
        # accident of every chunk being public.
        everything = ResearcherProfile.from_files(pdir).index.embedding("centroid")
        assert not np.allclose(everything, shipped, atol=1e-4)

    # a profile that will not load stops the publish

    def test_a_profile_that_will_not_load_fails_the_publish(self, tmp_path, audience_root, capsys):
        root = audience_root
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

    # the marker

    @pytest.mark.parametrize("marker", ["[]", '{"files": "x"}', '"x"', "{}"])
    def test_refuses_a_marker_of_the_wrong_shape(self, tmp_path, audience_root, marker):
        out = tmp_path / "out"
        out.mkdir()
        (out / MARKER).write_text(marker, encoding="utf-8")
        with pytest.raises(PublishError, match="unreadable"):
            publish_collection(audience_root, out)

    def test_a_crash_before_pruning_is_cleaned_up_next_run(
        self, tmp_path, audience_root, monkeypatch
    ):
        out = tmp_path / "out"
        publish_collection(audience_root, out)
        summary, rel = _first_summary(out)
        _set_part_tier(audience_root / "ada", rel, "limited")

        real = _publish._remove_stale
        monkeypatch.setattr(_publish, "_remove_stale", _boom)
        with pytest.raises(RuntimeError):
            publish_collection(audience_root, out)
        assert summary.exists()
        monkeypatch.setattr(_publish, "_remove_stale", real)

        result = publish_collection(audience_root, out)
        assert not summary.exists()
        assert f"profiles/ada/{rel}" in result.removed

    def test_dry_run_reports_what_it_would_remove(self, tmp_path, audience_root, capsys):
        out = tmp_path / "out"
        publish_collection(audience_root, out)
        summary, rel = _first_summary(out)
        _set_part_tier(audience_root / "ada", rel, "limited")

        result = publish_collection(audience_root, out, dry_run=True)
        assert result.would_remove == [f"profiles/ada/{rel}"]
        assert result.removed == []
        assert summary.exists()

        assert main(["publish", str(audience_root), "-o", str(out), "--dry-run", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["would_remove"] == [f"profiles/ada/{rel}"]
        assert main(["publish", str(audience_root), "-o", str(out), "--dry-run"]) == 0
        assert f"would remove profiles/ada/{rel}" in capsys.readouterr().out
        assert summary.exists()

    @pytest.mark.parametrize("who, sitemap", [("public", True), ("limited", False)])
    def test_sitemap_only_for_a_public_export(self, tmp_path, audience_root, who, sitemap):
        out = tmp_path / "out"
        publish_collection(audience_root, out, viewer=who, base_url="https://example.org")
        assert (out / "sitemap.xml").exists() is sitemap
        robots = (out / "robots.txt").read_text()
        assert ("Disallow: /\n" in robots) is not sitemap


class TestBuildSite:
    @pytest.fixture(scope="class")
    @classmethod
    def built_site(cls, tmp_path_factory) -> Path:
        """One ``build_site`` run over the two-profile root, shared by the read-only assertions.

        Class-scoped, so it copies the fixtures itself instead of using ``fixture_profiles_root``.
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
        assert by_rid["0000-0002-1825-0097"] == "profiles/jane-doe/profile.jsonld"

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
        """A mid-build failure is invisible to readers: output is staged and moved in on success."""
        out = tmp_path / "site"
        build_site(profiles_root, out)
        before = _tree_snapshot(out)

        # Change the inputs so a clobbered index.json would be detectable, then blow up on
        # SKILL.md, after index.json and friends have been written.
        shutil.rmtree(profiles_root / "john-smith")
        monkeypatch.setattr(publish._site, "generate_skill_md", _boom)

        with pytest.raises(RuntimeError, match="exploded"):
            build_site(profiles_root, out)

        assert _tree_snapshot(out) == before

    def test_failed_build_does_not_leave_an_output_dir_behind(
        self, profiles_root, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(publish._site, "generate_skill_md", _boom)
        out = tmp_path / "fresh" / "site"
        with pytest.raises(RuntimeError, match="exploded"):
            build_site(profiles_root, out)
        assert not (tmp_path / "fresh").exists()

    def test_preserves_sibling_trees_it_does_not_own(self, profiles_root, tmp_path):
        """``profiles/`` and ``app/`` are rsynced in by deploy; build_site owns only collection files."""
        out = tmp_path / "site"
        (out / "profiles" / "jane-doe").mkdir(parents=True)
        (out / "profiles" / "jane-doe" / "index.html").write_text("rsynced profile page")
        (out / "app").mkdir()
        (out / "app" / "index.html").write_text("explorer build")

        build_site(profiles_root, out, base_url="https://profiles.example.com")

        assert (out / "profiles" / "jane-doe" / "index.html").read_text() == "rsynced profile page"
        assert (out / "app" / "index.html").read_text() == "explorer build"
        assert (out / "index.json").is_file()
        assert (out / "robots.txt").is_file()
        assert "profiles.example.com" in (out / "sitemap.xml").read_text()


class TestMarkdownRenderer:
    @pytest.mark.parametrize(
        "source, expected",
        [
            ("# Hello", "<h1>Hello</h1>"),
            ("**bold**", "<p><strong>bold</strong></p>"),
            ("[click](http://example.com)", '<p><a href="http://example.com">click</a></p>'),
            ("- item1\n- item2", "<ul><li>item1</li><li>item2</li></ul>"),
            ("", ""),
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
        ids=[
            "heading",
            "bold",
            "link",
            "list",
            "empty",
            "escape-paragraph",
            "escape-heading",
            "escape-list",
            "escape-code",
            "escape-link",
            "escape-bold",
        ],
    )
    def test_md_to_html(self, source, expected):
        assert md_to_html(source) == expected


class TestEmbeddedGraphNodes:
    """The index.html graph carries the same ids and terms as the collections."""

    @pytest.mark.parametrize(
        "kwargs, expected_id, expected_identifier",
        [
            ({"paper_id": "doe2020x"}, "#paper/doe2020x", None),
            (
                {"paper_id": "doe2020x", "doi": "https://doi.org/10.1/ABC"},
                "https://doi.org/10.1/ABC",
                "https://doi.org/10.1/ABC",
            ),
        ],
        ids=["fragment-matches-papers-jsonld", "doi-wins-over-fragment"],
    )
    def test_paper_node_id(self, kwargs, expected_id, expected_identifier):
        paper = PaperRecord(title="T", **kwargs)
        node = paper_node(paper)
        assert node["@id"] == paper.resolve_id() == expected_id
        assert node.get("identifier") == expected_identifier

    def test_grant_node_uses_collection_terms(self):
        grant = GrantRecord(id="nih-r01", name="Atlas", role="pi", status="funded")
        node = grant_node(grant)
        assert node["@id"] == "#grant/nih-r01"
        assert node["role"] == "pi"
        assert node["status"] == "funded"
        assert "rp:role" not in node and "rp:status" not in node


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


class TestChangeAudience:
    """Re-publishing into a folder must not silently widen its audience."""

    def test_widening_refused_unless_flagged_and_narrowing_allowed(self, tmp_path, audience_root):
        out = tmp_path / "out"
        publish_collection(audience_root, out, viewer="public")
        with pytest.raises(PublishError, match="widen"):
            publish_collection(audience_root, out, viewer="limited")
        publish_collection(audience_root, out, viewer="limited", change_audience=True)
        # Narrowing needs no flag.
        publish_collection(audience_root, out, viewer="public")
        assert json.loads((out / ".rp-publish.json").read_text())["who"] == "public"

    def test_cli_flag_is_registered(self):
        args = build_parser().parse_args(["publish", "x", "-o", "y", "--change-audience"])
        assert args.change_audience is True
