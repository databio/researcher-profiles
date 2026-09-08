"""A profile directory and the object that loads it.

``ResearcherProfile`` is the front door; everything else here is a sidecar it
reads or writes: the manifest of what the directory contains, the build-state
bookkeeping kept out of the published record, the curation overrides, the
coverage/staleness cache, citations, and the optional SQL mirror.
"""

import json
import time
from datetime import datetime, timezone

import pytest

from researcher_profiles import (
    Citation,
    Coverage,
    ProfileLoadError,
    ResearcherProfile,
)
from researcher_profiles.build_state import (
    BUILD_STATE_PAPER_FIELDS,
    BuildState,
    PaperBuildState,
)
from researcher_profiles.profile.storage import ArtifactStorage
from researcher_profiles.schema import PaperRecord
from researcher_profiles.schema.manifest import build_manifest, manifest_drift
from researcher_profiles.utils.paths import build_meta_dir, cache_dir

from .factories import write_papers, write_profile

# --------------------------------------------------------------------------
# The ResearcherProfile core class
# --------------------------------------------------------------------------


def test_doe_basic_fields(jane_doe_readonly):
    p = jane_doe_readonly
    assert p.slug == "jane-doe"
    assert p.name
    assert "Doe" in p.name
    assert p.orcid is not None
    assert p.affiliation
    assert p.field
    # papers present
    assert len(p.papers) > 0
    first = p.papers[0]
    assert first.title
    assert first.paper_id


def test_smith_loads(fixture_profile):
    p = ResearcherProfile.from_files(fixture_profile("john-smith"))
    assert p.slug == "john-smith"
    assert p.name
    # exercise schema variation
    assert isinstance(p.metadata.career, list)


def _plain_file(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    return f


def _empty_dir(tmp_path):
    d = tmp_path / "empty-profile"
    d.mkdir()
    return d


@pytest.mark.parametrize(
    "setup, expected_exc",
    [
        (lambda tmp_path: tmp_path / "does-not-exist", FileNotFoundError),
        (_plain_file, NotADirectoryError),
        (_empty_dir, ProfileLoadError),
    ],
    ids=["nonexistent", "not-a-directory", "empty-dir"],
)
def test_bad_path_raises(tmp_path, setup, expected_exc):
    """Construction rejects bad paths; an empty directory only fails on read."""
    with pytest.raises(expected_exc):
        _ = ResearcherProfile.from_files(setup(tmp_path)).metadata


def test_to_dict_json_roundtrip(jane_doe_readonly):
    p = jane_doe_readonly
    d = p.to_dict()
    s = json.dumps(d)
    parsed = json.loads(s)
    assert parsed["slug"] == "jane-doe"
    assert "metadata" in parsed
    assert "summary_ids" in parsed
    assert isinstance(parsed["summary_ids"], list)


def test_to_dict_include_summaries(jane_doe_readonly):
    p = jane_doe_readonly
    d = p.to_dict(include_summaries=True)
    # include_summaries swaps the id list for the bodies themselves.
    assert "summary_ids" not in d
    summaries = d["summaries"]
    # jane-doe carries exactly these five summary files.
    assert set(summaries) == {
        "doe2016example",
        "doe2019methods",
        "doe2021widgets",
        "doe2023framework",
        "doe2024atlas",
    }
    # Bodies, not paths or ids: every value is non-empty markdown text.
    assert all(body.strip() for body in summaries.values())


def test_to_agent_seed_shape(jane_doe_readonly):
    p = jane_doe_readonly
    seed = p.to_agent_seed()
    assert seed["slug"] == "jane-doe"
    assert "Doe" in seed["name"]
    assert seed["orcid"] == p.metadata.orcid
    assert seed["affiliation"] == p.metadata.affiliation
    assert seed["field"] == p.metadata.field
    assert seed["summary"] == p.metadata.summary
    assert isinstance(seed["expertise_md"], str)
    assert isinstance(seed["soul_md"], str)
    assert isinstance(seed["n_papers"], int)
    assert seed["n_papers"] == len(p.papers)
    # JSON-serializable
    json.dumps(seed)


def test_equality_and_hash(jane_doe_readonly_dir):
    p1 = ResearcherProfile.from_files(jane_doe_readonly_dir)
    p2 = ResearcherProfile.from_files(jane_doe_readonly_dir)
    assert p1 == p2
    assert hash(p1) == hash(p2)
    assert {p1, p2} == {p1}


# --------------------------------------------------------------------------
# meta/manifest.json: what the directory contains
# --------------------------------------------------------------------------


def _roles(parts):
    return {p.role for p in parts}


class TestManifest:
    """The manifest: what a profile contains, stated rather than guessed at.

    Without it, an agent landing on a published directory has to guess at
    ``personality/SOUL.md``, ``sources/summaries/*.md``, ``.cache/embeddings.sqlite``.
    The manifest replaces guessing with typed, relatively-linked entries.
    """

    def test_generation_covers_every_documented_member(self, tmp_path):
        p = tmp_path / "researcher"
        write_profile(p, name="Researcher")
        write_papers(p, [{"paper_id": "paper1", "title": "A paper"}])
        (p / "personality").mkdir()
        (p / "personality" / "SOUL.md").write_text("soul")
        (p / "personality" / "expertise.md").write_text("expertise")
        (p / "sources" / "summaries").mkdir(parents=True)
        (p / "sources" / "summaries" / "paper1.summary.md").write_text("s")
        (p / "sources" / "papers").mkdir(parents=True)
        (p / "sources" / "papers" / "paper1.md").write_text("f")
        (p / "sources" / "web").mkdir(parents=True)
        (p / "sources" / "web" / "1-lab.md").write_text("w")
        (p / "sources" / "cv.md").write_text("cv")
        (p / "sources" / "citations.json").write_text("{}")
        (p / "embeddings").mkdir()
        (p / "embeddings" / "index.json").write_text("{}")
        cache_dir(p).mkdir()
        (cache_dir(p) / "embeddings.sqlite").write_bytes(b"SQLite")
        (p / "SKILL.md").write_text("skill")

        parts, subjects = build_manifest(p)
        assert _roles(subjects) == {"soul", "expertise"}
        assert _roles(parts) == {
            "agent_entry_point",
            "works",
            "citations",
            "cv",
            "paper_summary",
            "paper_fulltext",
            "web",
            "embedding_index",  # flat servable embeddings/index.json (public)
            "embedding_index_sqlite",  # profile-adjacent .cache/embeddings.sqlite (restricted)
        }

    def test_build_state_is_never_in_the_manifest(self, tmp_path):
        """The sidecar is a build artifact, not published record."""
        p = tmp_path / "researcher"
        write_profile(p, name="Researcher")
        # Build-session bookkeeping lives in the build root (.build/<slug>/meta/),
        # outside the content root entirely.
        bm = build_meta_dir(p)
        bm.mkdir(parents=True, exist_ok=True)
        (bm / "build_state.json").write_text("{}")
        (bm / "questions.yaml").write_text("questions: []")
        (bm / "rejected.yaml").write_text("[]")

        parts, subjects = build_manifest(p)
        urls = {e.content_url for e in (*parts, *subjects)}
        assert not any(
            u.endswith(("build_state.json", "questions.yaml", "rejected.yaml")) for u in urls
        )

    def test_content_urls_are_always_relative(self, tmp_path):
        """A profile must stay portable: copying it elsewhere cannot break a link."""
        p = tmp_path / "researcher"
        write_profile(p, name="Researcher")
        (p / "personality").mkdir()
        (p / "personality" / "SOUL.md").write_text("soul")

        parts, subjects = build_manifest(p)
        for entry in (*parts, *subjects):
            assert not entry.content_url.startswith("/")
            assert "://" not in entry.content_url
            assert (p / entry.content_url).is_file()

    def test_summary_entries_carry_their_paper_id(self, tmp_path):
        p = tmp_path / "researcher"
        write_profile(p, name="Researcher")
        (p / "sources" / "summaries").mkdir(parents=True)
        (p / "sources" / "summaries" / "doe2019methods.summary.md").write_text("s")

        parts, _ = build_manifest(p)
        entry = next(e for e in parts if e.role == "paper_summary")
        assert entry.paper_id == "doe2019methods"
        assert entry.encoding_format == "text/markdown"

    def test_drift_detects_a_file_missing_from_the_manifest(self, tmp_path):
        p = tmp_path / "researcher"
        write_profile(p, name="Researcher")
        (p / "personality").mkdir()
        (p / "personality" / "SOUL.md").write_text("soul")

        assert manifest_drift(p, [])["missing"] == ["personality/SOUL.md"]

    def test_fixture_manifest_matches_disk(self, jane_doe_readonly):
        prof = jane_doe_readonly
        drift = manifest_drift(prof.directory, prof.manifest())
        assert drift == {"missing": [], "stale": []}

    def test_flat_embeddings_are_manifested_and_not_publishignored(self, tmp_path):
        """embeddings/index.json is a public manifest entry; the sibling blob and
        chunks files ship by default (they are not excluded by .publishignore)."""
        from researcher_profiles import privacy

        p = tmp_path / "researcher"
        write_profile(p, name="Researcher")
        emb = p / "embeddings"
        emb.mkdir()
        (emb / "index.json").write_text('{"count": 1}')
        (emb / "st-all-minilm-l6-v2.bin").write_bytes(b"\x00" * 4)
        (emb / "st-all-minilm-l6-v2.chunks.json").write_text("[]")

        parts, _ = build_manifest(p)
        embedding_entry = next(e for e in parts if e.role == "embedding_index")
        assert embedding_entry.content_url == "embeddings/index.json"
        assert embedding_entry.visibility == "public"

        # Write the manifest back so effective_tiers sees it, then check the
        # deny-list excludes none of the flat embedding files.
        prof = ResearcherProfile.from_files(p)
        prof.build_manifest(write=True)
        prof = ResearcherProfile.from_files(p)
        ignore = set(privacy.publishignore_lines(prof.metadata))
        assert "embeddings/index.json" not in ignore
        assert "embeddings/st-all-minilm-l6-v2.bin" not in ignore
        assert "embeddings/st-all-minilm-l6-v2.chunks.json" not in ignore


# --------------------------------------------------------------------------
# meta/build_state.json: the build's bookkeeping
# --------------------------------------------------------------------------


class TestBuildState:
    """`meta/build_state.json`: the build's bookkeeping, kept out of the record.

    Publishing a profile must not publish the build's dirty laundry: download
    attempts, rejection reasons, contamination flags, verification bookkeeping.
    These tests pin the split in both directions: the sidecar round-trips, and no
    published paper record carries a single one of the migrated field names.
    """

    def test_absent_sidecar_is_not_an_error(self, tmp_path):
        """A published profile legitimately has no build state."""
        state = BuildState.load(tmp_path)
        assert state.papers == {}
        assert state.build.completed_phases == []

    def test_round_trip(self, tmp_path):
        state = BuildState()
        state.build.completed_phases = ["collect.identity", "collect.works"]
        state.build.phase_retries = {"collect.works": 2}
        state.inputs.cv_source = "sources/cv-source.md"
        state.inputs.websites = ["https://example.edu/~x"]
        ps = state.paper("paper2020a")
        ps.status = "rejected"
        ps.identity_verified = False
        state.save(tmp_path)

        reloaded = BuildState.load(tmp_path)
        assert reloaded.build.completed_phases == ["collect.identity", "collect.works"]
        assert reloaded.build.phase_retries == {"collect.works": 2}
        assert reloaded.inputs.cv_source == "sources/cv-source.md"
        assert reloaded.papers["paper2020a"].status == "rejected"
        assert reloaded.papers["paper2020a"].identity_verified is False
        assert reloaded.status_of("paper2020a") == "rejected"

    def test_unknown_paper_keys_survive_round_trip(self, tmp_path):
        state = BuildState()
        state.papers["p1"] = PaperBuildState.model_validate(
            {"status": "pending", "download_attempts": 2}
        )
        state.save(tmp_path)
        assert BuildState.load(tmp_path).papers["p1"].model_extra == {"download_attempts": 2}

    def test_sidecar_keeps_its_own_private_schema_version(self, tmp_path):
        """A build-local counter is fine; a PUBLISHED artifact needs a resolvable IRI."""
        BuildState().save(tmp_path)
        raw = json.loads((build_meta_dir(tmp_path) / "build_state.json").read_text())
        assert raw["schema_version"] == 1
        assert "conformsTo" not in raw
        assert "@context" not in raw

    def test_papers_are_sorted_by_id_on_disk(self, tmp_path):
        state = BuildState()
        for pid in ("zed2020", "alpha2019", "mid2021"):
            state.paper(pid).status = "pending"
        state.save(tmp_path)
        raw = json.loads((build_meta_dir(tmp_path) / "build_state.json").read_text())
        assert list(raw["papers"]) == ["alpha2019", "mid2021", "zed2020"]

    def test_no_build_field_survives_on_a_published_paper_record(self):
        """The published record must not carry a single migrated field name."""
        modelled = set(PaperRecord.model_fields)
        assert not (modelled & BUILD_STATE_PAPER_FIELDS)

    def test_migrated_fixture_has_no_build_fields_on_its_papers(self, jane_doe_readonly):
        prof = jane_doe_readonly
        for paper in prof.papers:
            extra = set(paper.model_extra or {})
            assert not (extra & BUILD_STATE_PAPER_FIELDS), paper.paper_id

    def test_contamination_is_build_state_not_published_record(self, tmp_path):
        p = tmp_path / "researcher"
        write_profile(p, name="Researcher")
        write_papers(p, [{"paper_id": "paper1", "title": "A paper"}])

        prof = ResearcherProfile.from_files(p)
        assert prof.set_paper_contaminated("paper1", True) is True
        assert prof.set_paper_contaminated("nope", False) is False

        # The published record is untouched...
        raw = json.loads((p / "sources" / "papers.jsonld").read_text())
        assert "contaminated" not in raw["hasPart"][0]
        # ...and the flag landed in the sidecar.
        assert BuildState.load(p).is_contaminated("paper1") is True


# --------------------------------------------------------------------------
# Coverage: last_updated, staleness, recent work
# --------------------------------------------------------------------------


class TestCoverage:
    """Tests for the coverage module: last_updated, staleness, recent_work, get()."""

    def test_staleness_returns_keys(self, jane_doe):
        s = jane_doe.coverage.staleness()
        assert {"days_since_update", "days_since_newest_paper", "score", "label"} <= set(s.keys())
        assert s["label"] in ("fresh", "aging", "stale")

    def test_recent_work_year_filter(self, jane_doe):
        # `since` is relative to the current year but the fixture's paper years
        # are frozen (2016-2025), so a literal window like "1y" quietly empties
        # this test as the calendar moves. Derive both windows from the fixture.
        years = sorted((p.year for p in jane_doe.papers if p.year), reverse=True)
        assert len(years) == 8, "jane-doe must carry 8 dated papers"
        this_year = datetime.now(timezone.utc).year

        # Wide enough to reach the oldest paper: everything comes back, newest first.
        wide = jane_doe.coverage.recent_work(since=this_year - years[-1] + 1, limit=len(years))
        assert [c.year for c in wide] == years

        # Narrow enough to exclude everything but the newest year: the filter filters.
        narrow = jane_doe.coverage.recent_work(since=this_year - years[0], limit=len(years))
        assert [c.year for c in narrow] == [y for y in years if y >= years[0]]
        assert years[-1] not in [c.year for c in narrow]
        assert 0 < len(narrow) < len(wide)

    def test_recent_work_bad_since(self, jane_doe):
        with pytest.raises(ValueError):
            jane_doe.coverage.recent_work(since="bogus")

    def test_coverage_returns_coverage(self, jane_doe):
        s = jane_doe.coverage.get()
        assert isinstance(s, Coverage)
        assert s.name
        assert s.paper_count == len(jane_doe.papers)
        assert s.staleness_label in ("fresh", "aging", "stale")

    def test_coverage_uses_cache_on_second_call(self, jane_doe):
        jane_doe.coverage.get()  # first call populates the cache
        cache = cache_dir(jane_doe.directory) / "coverage.json"
        # Mutate cache file to detect re-read
        data = json.loads(cache.read_text())
        data["one_liner"] = "CACHED_SENTINEL"
        cache.write_text(json.dumps(data))
        s2 = jane_doe.coverage.get()
        assert s2.one_liner == "CACHED_SENTINEL"

    def test_coverage_cache_busts_on_mtime_change(self, jane_doe):
        import os

        jane_doe.coverage.get()
        cache = cache_dir(jane_doe.directory) / "coverage.json"
        data = json.loads(cache.read_text())
        data["one_liner"] = "OLD_SENTINEL"
        cache.write_text(json.dumps(data))
        # Bump profile.jsonld mtime forward
        later = time.time() + 10000
        os.utime(jane_doe.directory / "profile.jsonld", (later, later))
        s = jane_doe.coverage.get()
        assert s.one_liner != "OLD_SENTINEL"


# --------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------


class TestCitations:
    """Tests for citations module (cite, cite_many, verify, export)."""

    def test_cite_returns_citation(self, jane_doe):
        pid = jane_doe.papers[0].paper_id
        c = jane_doe.cite.get(pid)
        assert isinstance(c, Citation)
        assert c.paper_id == pid
        assert c.title

    def test_cite_unknown_raises_keyerror(self, jane_doe):
        with pytest.raises(KeyError):
            jane_doe.cite.get("does-not-exist-2099")

    @pytest.mark.parametrize(
        "paper_fields, expected_url_attrs",
        [
            (
                {
                    "doi": "10.1234/foo.bar",
                    "full_text_link": "https://example.com/article.html",
                },
                {
                    "url": "https://doi.org/10.1234/foo.bar",
                    "doi": "10.1234/foo.bar",
                },
            ),
            (
                {"full_text_link": "https://example.com/article.html"},
                {"url": "https://example.com/article.html", "pdf_url": None},
            ),
            (
                {"full_text_link": "https://example.org/paper.pdf"},
                {"pdf_url": "https://example.org/paper.pdf"},
            ),
        ],
        ids=["doi-first", "full-text-link-fallback", "pdf-link"],
    )
    def test_cite_canonical_url(self, paper_fields, expected_url_attrs):
        """If paper has a DOI, url should be the doi.org URL; otherwise it
        falls back to the full-text link, and a ``.pdf`` link also sets
        ``pdf_url``."""
        from researcher_profiles.profile.cite import _paper_to_citation

        p = PaperRecord(paper_id="x", title="t", **paper_fields)
        c = _paper_to_citation(p)
        for attr, expected in expected_url_attrs.items():
            assert getattr(c, attr) == expected

    def test_cite_many_strict_skips_missing(self, jane_doe):
        pid = jane_doe.papers[0].paper_id
        out = jane_doe.cite.many([pid, "missing-xyz"], strict=False)
        assert len(out) == 1
        assert out[0].paper_id == pid

    def test_cite_many_strict_raises(self, jane_doe):
        with pytest.raises(KeyError):
            jane_doe.cite.many(["missing-xyz"], strict=True)

    def test_verify_citations(self, jane_doe):
        pids = [p.paper_id for p in jane_doe.papers[:2]]
        res = jane_doe.cite.verify(pids + ["nope"])
        assert res == [True, True, False]

    @pytest.mark.parametrize(
        "fmt, n, expected_substrings",
        [
            # Substrings are templates: ``{pid}`` expands to each exported id.
            ("bibtex", 2, ["@article{{{pid}"]),
            ("plain", 1, []),
            ("ris", 1, ["TY  - JOUR", "ER  -"]),
        ],
        ids=["bibtex", "plain", "ris"],
    )
    def test_export_format(self, jane_doe, fmt, n, expected_substrings):
        pids = [p.paper_id for p in jane_doe.papers[:n]]
        out = jane_doe.cite.export(pids, fmt)
        assert out.strip()
        for template in expected_substrings:
            for pid in pids:
                assert template.format(pid=pid) in out

    def test_export_csl_json(self, jane_doe):
        pids = [p.paper_id for p in jane_doe.papers[:2]]
        out = jane_doe.cite.export(pids, "csl-json")
        data = json.loads(out)
        assert len(data) == 2
        assert all(r["type"] == "article-journal" for r in data)

    def test_export_unknown_format(self, jane_doe):
        with pytest.raises(ValueError):
            jane_doe.cite.export([], "xml")


# ---------------------------------------------------------------------------
# The storage layer: the public write surface
# ---------------------------------------------------------------------------


class TestSaveProfileStamps:
    """``save_profile()`` stamps ``dateModified``.

    Without that stamp, ``build_manifest(write=True)`` could move manifest
    ``sha256`` entries (a content change by ``date_modified``'s own doctrine)
    and leave the vintage stale.
    """

    def test_a_content_change_advances_the_stamp(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.save_profile(prof.metadata.model_copy(update={"field": "Genomics"}))
        first = json.loads((jane_doe_dir / "profile.jsonld").read_text())["dateModified"]

        prof2 = ResearcherProfile.from_files(jane_doe_dir)
        prof2.save_profile(prof2.metadata.model_copy(update={"field": "Epigenomics"}))
        second = json.loads((jane_doe_dir / "profile.jsonld").read_text())["dateModified"]

        assert second >= first
        assert json.loads((jane_doe_dir / "profile.jsonld").read_text())["field"] == "Epigenomics"

    def test_a_no_op_resave_leaves_the_stamp_alone(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.save_profile(prof.metadata.model_copy(update={"field": "Genomics"}))
        stamped = json.loads((jane_doe_dir / "profile.jsonld").read_text())["dateModified"]
        raw = (jane_doe_dir / "profile.jsonld").read_text()

        ResearcherProfile.from_files(jane_doe_dir).save_profile()

        after = json.loads((jane_doe_dir / "profile.jsonld").read_text())
        assert after["dateModified"] == stamped
        assert (jane_doe_dir / "profile.jsonld").read_text() == raw

    def test_an_invalid_document_never_reaches_the_store(self, jane_doe_dir):
        from researcher_profiles.errors import ProfileWriteError

        prof = ResearcherProfile.from_files(jane_doe_dir)
        before = (jane_doe_dir / "profile.jsonld").read_text()
        with pytest.raises(ProfileWriteError):
            prof.save_profile(prof.metadata.model_copy(update={"name": ""}))
        assert (jane_doe_dir / "profile.jsonld").read_text() == before


class TestPublicWritersRoundTrip:
    """Write through the public surface, reload from the same directory, read back."""

    def _reload(self, d):
        return ResearcherProfile.from_files(d)

    def test_save_expertise(self, jane_doe_dir):
        ResearcherProfile.from_files(jane_doe_dir).save_expertise("# Expertise\n\nATAC-seq.\n")
        assert self._reload(jane_doe_dir).expertise == "# Expertise\n\nATAC-seq.\n"

    def test_save_soul(self, jane_doe_dir):
        ResearcherProfile.from_files(jane_doe_dir).save_soul("# Soul\n")
        assert self._reload(jane_doe_dir).soul == "# Soul\n"

    def test_save_grants(self, jane_doe_dir):
        from researcher_profiles.schema import GrantRecord

        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.save_grants([GrantRecord(id="https://reporter.nih.gov/R01HG000000", name="A grant")])
        reloaded = self._reload(jane_doe_dir)
        assert [g.title for g in reloaded.grants] == ["A grant"]

    def test_save_papers(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        papers = list(prof.papers)
        papers.append(PaperRecord(paper_id="pmid:99999999", title="A late addition"))
        prof.save_papers(papers)
        assert "pmid:99999999" in {p.paper_id for p in self._reload(jane_doe_dir).papers}

    def test_save_citations_and_delete(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.save_citations({"nodes": [], "edges": []})
        assert self._reload(jane_doe_dir).citations == {"nodes": [], "edges": []}
        prof.save_citations(None)
        assert self._reload(jane_doe_dir).citations is None

    def test_save_and_delete_summary(self, jane_doe_dir):
        prof = ResearcherProfile.from_files(jane_doe_dir)
        prof.save_summary("pmid:12345678", "A summary body.\n")
        assert prof.summaries["pmid:12345678"] == "A summary body.\n"
        assert self._reload(jane_doe_dir).summaries["pmid:12345678"] == "A summary body.\n"
        prof.delete_summary("pmid:12345678")
        assert "pmid:12345678" not in prof.summaries
        assert "pmid:12345678" not in self._reload(jane_doe_dir).summaries


class DictStorage(ArtifactStorage):
    """A profile's artifacts in a plain dict. The proof the storage layer is real.

    Implements the whole of :class:`ArtifactStorage` over a dict, with a fake
    session and ``atomic=True``, so the suite exercises the atomic branch of
    the hook contract without a database. Small and test-only:
    this is not SDK API, and it has exactly one consumer, so it stays here
    rather than moving to ``factories.py``.
    """

    def __init__(self, store: dict):
        self.store = store
        self.committed = 0
        self.rolled_back = 0
        self.session = object()

    # Identity

    @property
    def key(self):
        return "dict:test-profile"

    @property
    def slug(self):
        return "test-profile"

    @property
    def rid_hint(self):
        return None

    @property
    def directory(self):
        return None

    def locate(self, *parts):
        return f"dict:test-profile/{'/'.join(parts)}"

    # Artifacts

    def load_persisted_document(self):
        return dict(self.store.get("document") or {})

    def load_document(self):
        from researcher_profiles.schema import ProfileDocument

        return ProfileDocument.model_validate(self.store["document"])

    def save_document(self, data):
        self.store["document"] = dict(data)

    def content_hash(self):
        import hashlib

        from researcher_profiles.schema.jsonld import canonical_dumps

        h = hashlib.sha256()
        h.update(canonical_dumps(self.load_persisted_document()).encode("utf-8"))
        h.update(b"\x00")
        h.update(self.load_soul().encode("utf-8"))
        return f"sha256:{h.hexdigest()}"

    def load_expertise(self):
        return self.store.get("expertise", "")

    def save_expertise(self, text):
        self.store["expertise"] = text

    def load_soul(self):
        return self.store.get("soul", "")

    def save_soul(self, text):
        self.store["soul"] = text

    def load_papers(self):
        return list(self.store.get("papers") or [])

    def save_papers(self, papers):
        self.store["papers"] = list(papers)

    def load_grants(self):
        return list(self.store.get("grants") or [])

    def save_grants(self, grants):
        self.store["grants"] = list(grants)

    def load_build_state(self):
        return BuildState.model_validate(self.store.get("build_state") or {})

    def save_build_state(self, state):
        self.store["build_state"] = state.model_dump(mode="json", exclude_none=True)

    def load_citations(self):
        return self.store.get("citations")

    def save_citations(self, data):
        self.store["citations"] = data

    def load_summaries(self):
        return dict(self.store.get("summaries") or {})

    def save_summary(self, paper_id, text):
        self.store.setdefault("summaries", {})[paper_id] = text

    def delete_summary(self, paper_id):
        self.store.get("summaries", {}).pop(paper_id, None)

    # Raw bodies

    def artifact_text(self, content_url):
        return (self.store.get("artifacts") or {}).get(content_url)

    def artifact_bytes(self, content_url):
        text = self.artifact_text(content_url)
        return None if text is None else text.encode("utf-8")

    def collection_envelope(self, content_url):
        return {}

    def build_manifest(self):
        doc = self.load_document()
        return list(doc.has_part), list(doc.subject_of)

    # Transaction

    def new_write_context(self, profile, kind):
        from researcher_profiles.profile.write_unit import WriteContext

        return WriteContext(
            profile=profile,
            slug=self.slug,
            rid=profile.rid_or_empty(),
            kind=kind,
            session=self.session,
            atomic=True,
        )

    def commit(self, ctx):
        self.committed += 1

    def rollback(self, ctx):
        self.rolled_back += 1


@pytest.fixture
def dict_profile(jane_doe_dir):
    """A ``DictStorage``-backed profile seeded from the jane-doe fixture."""
    src = ResearcherProfile.from_files(jane_doe_dir)
    return ResearcherProfile(
        DictStorage(
            {
                "document": json.loads((jane_doe_dir / "profile.jsonld").read_text()),
                "soul": src.soul,
                "expertise": src.expertise,
                "papers": list(src.papers),
                "grants": list(src.grants),
            }
        )
    )


class TestNonFilesystemBackend:
    def test_nested_edits_share_the_staged_document_and_hooks_see_it(self, dict_profile):
        seen = []
        dict_profile.add_pre_commit_hook(lambda ctx: seen.append(ctx.profile.metadata))
        with dict_profile.write_unit("owner-edit"):
            dict_profile.edit.patch_metadata({"field": "Systems Biology"})
            dict_profile.edit.set_visibility(profile_visibility="internal")

        assert dict_profile.metadata.field == "Systems Biology"
        assert dict_profile.metadata.visibility == "internal"
        assert seen[0].field == "Systems Biology"
        assert seen[0].visibility == "internal"

    def test_post_commit_hook_sees_the_committed_cache(self, dict_profile):
        seen = []
        dict_profile.add_post_commit_hook(lambda ctx: seen.append(ctx.profile.soul))
        dict_profile.edit.set_soul("# New soul\n")
        assert seen == ["# New soul\n"]

    def test_metadata_patch_goes_through_the_seam(self, dict_profile):
        dict_profile.edit.patch_metadata({"name": "Jane Q. Doe"})
        assert dict_profile.storage.store["document"]["name"] == "Jane Q. Doe"
        assert dict_profile.storage.committed == 1

    def test_set_soul_goes_through_the_seam(self, dict_profile):
        dict_profile.edit.set_soul("# A dict soul\n")
        assert dict_profile.storage.store["soul"] == "# A dict soul\n"
        assert dict_profile.soul == "# A dict soul\n"

    def test_save_papers_goes_through_the_seam(self, dict_profile):
        papers = list(dict_profile.papers)
        papers.append(PaperRecord(paper_id="pmid:99999999", title="Late"))
        dict_profile.save_papers(papers)
        assert dict_profile.storage.store["papers"][-1].paper_id == "pmid:99999999"

    def test_set_paper_contaminated_goes_through_the_seam(self, dict_profile):
        pid = dict_profile.papers[0].paper_id
        assert dict_profile.set_paper_contaminated(pid, True) is True
        assert dict_profile.storage.store["build_state"]["papers"][pid]["contaminated"] is True

    def test_hook_sees_an_atomic_context(self, dict_profile):
        seen = []
        dict_profile.add_pre_commit_hook(seen.append)
        dict_profile.save_soul("# soul\n")
        assert seen[0].atomic is True
        assert seen[0].session is dict_profile.storage.session

    def test_a_raising_hook_rolls_back_and_does_not_commit(self, dict_profile):
        from researcher_profiles.errors import WriteHookError

        dict_profile.add_pre_commit_hook(lambda ctx: (_ for _ in ()).throw(RuntimeError("no")))
        with pytest.raises(WriteHookError):
            dict_profile.save_soul("# soul\n")
        assert dict_profile.storage.committed == 0
        assert dict_profile.storage.rolled_back == 1
