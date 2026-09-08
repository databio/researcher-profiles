"""The knowledge-base export surface: the blob, the selection, the bundle.

Everything here pins something a downstream connector would break by accident.
The load-bearing claim is **determinism**: ``render_export_text`` has no clock,
no counter and no host-dependent value in it, which is the only reason
``content_hash`` can be an idempotency key at all. If a test in this file fails,
a knowledge base is about to either re-ingest every profile on every run or
miss a real change.
"""

import json
import re
from pathlib import Path

import pytest

from researcher_profiles.cli import main
from researcher_profiles.profile import ResearcherProfile
from researcher_profiles.profile.export import (
    ExportOptions,
    ExportVisibilityError,
    ProfileExportBundle,
    build_export_bundle,
    explore_url,
    export_content_hash,
    render_export_text,
    select_export_papers,
)

from .factories import build_profile_dir

#: An ISO-8601 timestamp in any of the shapes the package emits. The rendered
#: blob must contain none of them.
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")

_CHROMATIN_A = (
    "We mapped chromatin accessibility with ATAC-seq across cortical neurons "
    "and identified accessible peaks marking distal enhancers. Peak calling was "
    "benchmarked against nucleosome positioning signal."
)
_CHROMATIN_B = (
    "We mapped chromatin accessibility using ATAC-seq across cortical neurons, "
    "identifying accessible peaks that mark distal enhancers. Peak calling is "
    "benchmarked against nucleosome positioning signal."
)


def _paper(pid: str, title: str, **kw) -> dict:
    row = {"paper_id": pid, "title": title, "year": 2020, "author_position": "first"}
    row.update(kw)
    return row


#: Six papers: two near-duplicates on one topic and four on distinct ones, with
#: citation counts tuned so the duplicates are the top two scorers. The corpus
#: stopword pass drops a token present in more than 60% of the papers, so the
#: shared chromatin vocabulary has to sit in 2 of 6 to still be measurable,
#: which is why this table is six rows and not three.
_DIVERSITY_PAPERS = [
    _paper(
        "dup1",
        "Chromatin accessibility of cortical neurons",
        abstract=_CHROMATIN_A,
        cited_by_count=1000,
    ),
    _paper(
        "dup2",
        "Chromatin accessibility across cortical tissue",
        abstract=_CHROMATIN_B,
        cited_by_count=900,
    ),
    _paper(
        "prot",
        "Folding kinetics of small proteins",
        cited_by_count=800,
        abstract=(
            "Folding kinetics were measured by stopped-flow fluorescence for "
            "designed miniproteins. Transition-state placement varies with "
            "solvent viscosity."
        ),
    ),
    _paper(
        "eco",
        "Grazing pressure in alpine meadows",
        cited_by_count=100,
        abstract=(
            "Grazing pressure was surveyed across alpine meadow plots over "
            "twelve seasons, relating herbivore density to floral diversity."
        ),
    ),
    _paper(
        "astro",
        "Timing residuals of millisecond pulsars",
        cited_by_count=50,
        abstract=(
            "Timing residuals for millisecond pulsars were fitted over a "
            "decade of radio observations to bound a stochastic background."
        ),
    ),
    _paper(
        "compil",
        "Register allocation under aliasing",
        cited_by_count=20,
        abstract=(
            "Register allocation is reformulated as graph colouring under "
            "pointer aliasing constraints inside an optimising compiler."
        ),
    ),
]


def _profile(path: Path, **kw) -> ResearcherProfile:
    """A synthetic profile on disk, loaded."""
    kw.setdefault("summaries", False)
    kw.setdefault("personality", True)
    build_profile_dir(path, **kw)
    return ResearcherProfile.from_files(path)


def _ids(papers) -> list[str]:
    return [p.paper_id for p in papers]


class TestRenderExportText:
    """The blob itself: stable bytes, clean prose, honest headings."""

    def test_two_renders_are_byte_identical(self, jane_doe_readonly):
        """Determinism is the whole contract the content hash rests on."""
        assert render_export_text(jane_doe_readonly) == render_export_text(jane_doe_readonly)

    def test_blob_carries_no_timestamp_and_no_hash(self, jane_doe_readonly):
        text = render_export_text(jane_doe_readonly)
        assert not _TIMESTAMP_RE.search(text)
        assert "sha256:" not in text

    def test_summary_bookkeeping_never_reaches_the_blob(self, tmp_path):
        """Frontmatter and the ``[abstract-only]`` marker are bookkeeping, not prose."""
        prof = _profile(
            tmp_path / "p",
            papers=[_paper("p1", "A study of widgets")],
            summaries={
                "p1": (
                    "---\n"
                    "paper_id: p1\n"
                    "source_kind: abstract\n"
                    "source_hash: deadbeef\n"
                    "---\n"
                    "[abstract-only]\n"
                    "Widgets were characterized by spectroscopy.\n"
                )
            },
        )
        text = render_export_text(prof)
        assert "Widgets were characterized by spectroscopy." in text
        assert "source_hash" not in text
        assert "abstract-only" not in text
        assert "paper_id: p1" not in text

    def test_a_section_with_no_content_emits_no_heading(self, tmp_path):
        prof = _profile(
            tmp_path / "p",
            papers=[_paper("p1", "A study of widgets", abstract="Widgets, characterized.")],
            expertise=["region-set-analysis"],
        )
        text = render_export_text(prof)
        assert "## Expertise" in text
        assert "## Interests" not in text
        assert "## Methodological commitments" not in text

    def test_char_budget_drops_whole_blocks_and_never_splits_a_body(self, tmp_path):
        long_a = "Alpha " * 400
        long_b = "Bravo " * 400
        prof = _profile(
            tmp_path / "p",
            papers=[
                _paper("newer", "Newer work", year=2022, abstract=long_a.strip()),
                _paper("older", "Older work", year=2001, abstract=long_b.strip()),
            ],
        )
        full = render_export_text(prof)
        assert "Older work" in full

        trimmed = render_export_text(prof, ExportOptions(char_budget=len(full) - 100))
        assert len(trimmed) < len(full)
        # The block that survived survived whole: a half-abstract embeds as a
        # claim its author did not make.
        assert long_a.strip() in trimmed
        assert "Older work" not in trimmed
        assert "Bravo" not in trimmed


class TestSelectExportPapers:
    """What gets in, what stays out, and why the set spans topics."""

    @pytest.mark.parametrize(
        "papers,contaminate,options,expected",
        [
            pytest.param(
                [
                    _paper("keep", "A kept paper", abstract="Kept prose about widgets."),
                    _paper("bad", "A contaminated paper", abstract="Contaminated prose."),
                ],
                "bad",
                ExportOptions(),
                ["keep"],
                id="contaminated dropped",
            ),
            pytest.param(
                [
                    _paper("keep", "A kept paper", abstract="Kept prose about widgets."),
                    _paper("blank", "   ", abstract="Untitled prose."),
                ],
                None,
                ExportOptions(),
                ["keep"],
                id="untitled dropped",
            ),
            pytest.param(
                [
                    _paper("keep", "A kept paper", abstract="Kept prose about widgets."),
                    _paper("empty", "A paper with no body"),
                ],
                None,
                ExportOptions(),
                ["keep"],
                id="bodyless dropped",
            ),
            pytest.param(
                _DIVERSITY_PAPERS,
                None,
                ExportOptions(max_papers=2, diversity=0.0),
                ["dup1", "dup2"],
                id="max_papers respected",
            ),
            pytest.param(
                _DIVERSITY_PAPERS,
                None,
                ExportOptions(max_papers=2, diversity=0.7),
                ["dup1", "prot"],
                id="near-duplicates split at diversity=0.7",
            ),
        ],
    )
    def test_selection(self, tmp_path, papers, contaminate, options, expected):
        prof = _profile(tmp_path / "p", papers=papers)
        if contaminate:
            assert prof.set_paper_contaminated(contaminate, True)
            prof = ResearcherProfile.from_files(prof.directory)
        assert sorted(_ids(select_export_papers(prof, options))) == sorted(expected)


class TestExportBundle:
    """The handoff: identity, links, the corpus DOI list, and the change detector."""

    def test_content_hash_ignores_the_clock(self, jane_doe_readonly):
        a = build_export_bundle(jane_doe_readonly, now="2020-01-01T00:00:00+00:00")
        b = build_export_bundle(jane_doe_readonly, now="2026-08-21T12:34:56+00:00")
        assert a.built_at != b.built_at
        assert a.content_hash == b.content_hash

    def test_content_hash_tracks_the_corpus(self, tmp_path):
        path = tmp_path / "p"
        prof = _profile(path, papers=[_paper("p1", "A study", abstract="Original prose.")])
        before = build_export_bundle(prof).content_hash

        papers_file = path / "sources" / "papers.jsonld"
        doc = json.loads(papers_file.read_text())
        doc["hasPart"][0]["abstract"] = "Revised prose, entirely different."
        papers_file.write_text(json.dumps(doc), encoding="utf-8")

        after = build_export_bundle(ResearcherProfile.from_files(path)).content_hash
        assert after != before

    def test_dois_are_the_whole_corpus_normalized_and_deduped(self, tmp_path):
        prof = _profile(
            tmp_path / "p",
            papers=[
                _paper(
                    "p1",
                    "In the blob",
                    abstract="Widgets were characterized.",
                    doi="https://doi.org/10.1234/AbC",
                ),
                _paper(
                    "p2",
                    "Not in the blob",
                    abstract="Gadgets were characterized.",
                    doi="doi:10.5555/zzz",
                ),
                _paper(
                    "p3",
                    "Also not in the blob",
                    abstract="Gizmos were characterized.",
                    doi="10.1234/abc",
                ),
            ],
        )
        bundle = build_export_bundle(prof, ExportOptions(max_papers=1))
        assert bundle.dois == ["10.1234/abc", "10.5555/zzz"]
        assert bundle.paper_count == 3
        assert len(bundle.papers) == 1

    def test_summary_round_trips_and_is_covered_by_the_hash(self, tmp_path):
        """``summary`` is content, not decoration: editing it must refresh the KB.

        A connector uses this as its document description, so it has to arrive
        on the bundle (rp-sdk owns profile reading; nobody downstream reopens
        ``profile.jsonld``) AND has to sit inside the hash coverage.
        """
        path = tmp_path / "p"
        prof = _profile(
            path,
            summary="Studies widgets and their kinetics.",
            papers=[_paper("p1", "A study", abstract="Widgets were characterized.")],
        )
        bundle = build_export_bundle(prof)
        assert bundle.summary == "Studies widgets and their kinetics."

        # Pin the coverage directly, not only the consequence: `summary` is in
        # the hashed payload, so it cannot later be excluded while the rest of
        # this test keeps passing on the rendered text alone.
        payload = bundle.model_dump(mode="json")
        payload.pop("built_at")
        payload.pop("content_hash")
        assert payload["summary"] == "Studies widgets and their kinetics."
        assert export_content_hash(payload) == bundle.content_hash

        doc_file = path / "profile.jsonld"
        doc = json.loads(doc_file.read_text())
        doc["summary"] = "Studies gadgets and their thermodynamics."
        doc_file.write_text(json.dumps(doc), encoding="utf-8")

        revised = build_export_bundle(ResearcherProfile.from_files(path))
        assert revised.summary == "Studies gadgets and their thermodynamics."
        assert revised.content_hash != bundle.content_hash

    def test_explore_url_is_the_shape_the_browser_app_parses(self):
        assert explore_url(
            "https://profiles.example.org/profiles/jane-doe/profile.jsonld",
            explore_base="https://explore.example.org",
        ) == (
            "https://explore.example.org/#/p?u="
            "https%3A%2F%2Fprofiles.example.org%2Fprofiles%2Fjane-doe%2F"
        )
        assert explore_url(None) is None
        assert explore_url("https://profiles.example.org/profiles/jane-doe/") is None

    def test_nonpublic_profile_refuses_until_the_caller_opts_in(self, tmp_path):
        prof = _profile(
            tmp_path / "p",
            visibility="internal",
            papers=[_paper("p1", "A study", abstract="Internal prose.")],
        )
        with pytest.raises(ExportVisibilityError):
            build_export_bundle(prof)

        bundle = build_export_bundle(prof, ExportOptions(allow_nonpublic=True))
        assert bundle.visibility == "internal"
        assert "Internal prose." in bundle.text

    def test_restricted_sources_never_reach_the_text(self, tmp_path):
        """``cv``/``web``/``grant`` are restricted by role, not by allowlist."""
        prof = _profile(
            tmp_path / "p",
            level="deep",
            papers=[_paper("p1", "A study", abstract="Public prose.")],
            cv=True,
            web=True,
            grants=True,
        )
        text = build_export_bundle(prof).text
        assert "Public prose." in text
        assert "PhD in genomics" not in text  # sources/cv.md
        assert "We study chromatin accessibility" not in text  # sources/web/
        assert "Funds work on enhancer prediction" not in text  # sources/grants.jsonld


class TestExportCli:
    """`rp export`: stdout, JSON, and the refusal exit code."""

    def test_prints_the_blob(self, jane_doe_readonly_dir, capsys):
        assert main(["export", str(jane_doe_readonly_dir)]) == 0
        assert "Jane A. Doe" in capsys.readouterr().out

    def test_json_validates_as_a_bundle(self, jane_doe_readonly_dir, capsys):
        assert main(["export", str(jane_doe_readonly_dir), "--json"]) == 0
        bundle = ProfileExportBundle.model_validate_json(capsys.readouterr().out)
        assert bundle.rid == "0000-0002-1825-0097"
        assert bundle.text_chars == len(bundle.text)

    def test_nonpublic_profile_exits_four(self, tmp_path, capsys):
        path = build_profile_dir(tmp_path / "p", visibility="internal")
        assert main(["export", str(path)]) == 4
        assert "--allow-nonpublic" in capsys.readouterr().err
