"""The pinned interest vocabularies: OpenAlex topics and MeSH descriptors.

Pins what a stored concept code resolves to (the ``@id``, label and release
every tool builds from the same snapshot), that an unknown code is reported as
unknown rather than invented, and the refresh command's file shape.
"""

import gzip
import json

import pytest

from researcher_profiles import vocab
from researcher_profiles.cli import main
from researcher_profiles.vocab import MESH_SYSTEM, OPENALEX_SYSTEM


class TestLookup:
    @pytest.mark.parametrize(
        ("system", "code", "iri", "display"),
        [
            (
                OPENALEX_SYSTEM,
                "T10222",
                "https://openalex.org/T10222",
                "Genomics and Chromatin Dynamics",
            ),
            (OPENALEX_SYSTEM, "https://openalex.org/t10222", "https://openalex.org/T10222", None),
            (MESH_SYSTEM, "D057890", "http://id.nlm.nih.gov/mesh/D057890", "Epigenomics"),
        ],
        ids=["openalex", "openalex-iri-form", "mesh"],
    )
    def test_known_codes_resolve_from_the_pinned_copy(self, system, code, iri, display):
        concept = vocab.lookup(system, code)
        assert concept is not None
        assert concept.id_ == iri
        assert concept.system == system
        assert concept.version == (
            vocab.OPENALEX_RELEASE if system == OPENALEX_SYSTEM else vocab.MESH_RELEASE
        )
        if display:
            assert concept.display == display

    @pytest.mark.parametrize(
        ("system", "code"),
        [(OPENALEX_SYSTEM, "T99999999"), (MESH_SYSTEM, "D999999999")],
        ids=["openalex", "mesh"],
    )
    def test_unknown_codes_are_none(self, system, code):
        assert vocab.lookup(system, code) is None

    def test_releases_name_the_snapshot_not_a_retrieval_date(self):
        assert len(vocab.MESH_RELEASE) == 4 and vocab.MESH_RELEASE.isdigit()
        year, _, month = vocab.OPENALEX_RELEASE.partition("-")
        assert year.isdigit() and month.isdigit()

    def test_search_ranks_an_exact_label_first(self):
        hits = vocab.search(MESH_SYSTEM, "machine learning", limit=3)
        assert hits[0].display == "Machine Learning"


_MESH_XML = """<?xml version="1.0"?>
<DescriptorRecordSet LanguageCode="eng">
<DescriptorRecord DescriptorClass="1">
  <DescriptorUI>D000001</DescriptorUI>
  <DescriptorName><String>Calcimycin</String></DescriptorName>
  <TreeNumberList><TreeNumber>D03.633.100</TreeNumber></TreeNumberList>
  <ConceptList><Concept><TermList>
    <Term><String>Calcimycin</String></Term>
    <Term><String>A-23187</String></Term>
  </TermList></Concept></ConceptList>
</DescriptorRecord>
</DescriptorRecordSet>
"""


class TestRefresh:
    def test_mesh_refresh_writes_the_release_from_the_file_name(self, tmp_path, capsys):
        src = tmp_path / "desc2031.gz"
        src.write_bytes(gzip.compress(_MESH_XML.encode()))
        out = tmp_path / "out"
        out.mkdir()
        assert main(["vocab", "refresh", "--mesh", str(src), "--out-dir", str(out)]) == 0
        data = json.loads(gzip.decompress((out / vocab.MESH_FILE).read_bytes()))
        assert data["release"] == "2031"
        assert data["system"] == MESH_SYSTEM
        assert data["descriptors"] == [
            {
                "id": "D000001",
                "label": "Calcimycin",
                "tree_numbers": ["D03.633.100"],
                "synonyms": ["A-23187"],
            }
        ]
        assert "1 added, 0 removed" in capsys.readouterr().out

    def test_nothing_to_refresh_is_a_usage_error(self):
        assert main(["vocab", "refresh"]) == 2
