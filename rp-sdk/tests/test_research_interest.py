"""``ResearchInterest``: the typed, signed-weight interest record.

Pins the on-disk contract of ``rp:researchInterests`` (the coded / text-only
concept forms, the -1..1 weight, None vs 0), the precedence rule every consumer
applies, the projection onto the plain ``interests`` / ``not_interests`` lists,
the owner-edit path that writes declared entries, and the JSON-LD mapping.
"""

import json

import pytest
from pydantic import ValidationError

from researcher_profiles import ResearcherProfile, to_foaf
from researcher_profiles.schema import (
    InterestConcept,
    ProfileDocument,
    ResearchInterest,
    effective_interests,
)

from .factories import REPO_ROOT

OPENALEX = "https://openalex.org/topics"
MESH = "http://id.nlm.nih.gov/mesh"


def _coded(code: str, display: str, system: str = OPENALEX) -> dict:
    iri = f"https://openalex.org/{code}" if system == OPENALEX else f"{MESH}/{code}"
    return {"@id": iri, "system": system, "code": code, "display": display, "version": "2026-09"}


def _entry(concept: dict, weight, method="declared", at="2026-09-25T12:00:00Z", **kw) -> dict:
    out = {"concept": concept, "method": method, "generator": kw.pop("generator", "user")}
    out["assertedAt"] = at
    if weight is not None:
        out["weight"] = weight
    return {**out, **kw}


def _doc(entries: list[dict]) -> ProfileDocument:
    return ProfileDocument.model_validate(
        {
            "name": "Interests",
            "rid": "local:interests-a1b2c3",
            "provenance": "self_published",
            "rp:researchInterests": entries,
        }
    )


class TestConcept:
    @pytest.mark.parametrize(
        "concept",
        [
            {"system": OPENALEX, "code": "T1"},
            {"system": OPENALEX, "display": "Genomics"},
            {"label": "graph methods"},
            {"label": "graph methods", "unmapped": True, "system": OPENALEX, "code": "T1"},
            {"label": "graph methods", "unmapped": True, "@id": "https://openalex.org/T1"},
            {"unmapped": True},
        ],
        ids=[
            "coded-without-display",
            "coded-without-code",
            "label-without-unmapped",
            "text-only-with-code",
            "text-only-with-iri",
            "text-only-without-label",
        ],
    )
    def test_mixed_or_partial_concepts_are_refused(self, concept):
        with pytest.raises(ValidationError):
            InterestConcept.model_validate(concept)

    def test_both_forms_round_trip_on_disk(self):
        doc = _doc(
            [
                _entry(_coded("T10222", "Genomics"), None, method="inferred"),
                _entry({"label": "interval data standards", "unmapped": True}, 0.5),
            ]
        )
        out = doc.model_dump(mode="json")["rp:researchInterests"]
        assert out[0]["concept"] == _coded("T10222", "Genomics")
        assert "weight" not in out[0]
        assert out[0]["@type"] == "ResearchInterest"
        assert out[1]["concept"] == {"label": "interval data standards", "unmapped": True}

    @pytest.mark.parametrize("weight", [1.5, -1.01], ids=["above-1", "below-minus-1"])
    def test_weight_is_bounded(self, weight):
        with pytest.raises(ValidationError):
            _doc([_entry(_coded("T1", "A"), weight)])


class TestProjection:
    @pytest.mark.parametrize(
        ("entries", "interests", "not_interests"),
        [
            (
                [
                    _entry(_coded("T1", "Core"), 1),
                    _entry(_coded("T2", "Neutral"), 0),
                    _entry(_coded("T3", "Unknown"), None, method="inferred"),
                    _entry(_coded("T4", "Less"), -0.5),
                ],
                ["Core"],
                ["Less"],
            ),
            (
                [
                    _entry(_coded("T1", "A"), -1, at="2026-09-25T12:00:00Z"),
                    _entry(_coded("T1", "A"), 0.5, method="inferred", at="2026-09-26T12:00:00Z"),
                ],
                [],
                ["A"],
            ),
            (
                [
                    _entry(_coded("T1", "A"), -1, at="2026-09-25T12:00:00Z"),
                    _entry(_coded("T1", "A"), 0.9, at="2026-09-26T12:00:00Z"),
                ],
                ["A"],
                [],
            ),
            (
                [
                    _entry({"label": "Graphs", "unmapped": True}, 0.5, method="inferred"),
                    _entry({"label": "graphs", "unmapped": True}, 0),
                ],
                [],
                [],
            ),
        ],
        ids=[
            "sign-decides-and-none-or-zero-is-neither",
            "declared-beats-newer-inferred",
            "newest-declared-wins",
            "text-only-keys-ignore-case",
        ],
    )
    def test_plain_lists_are_rebuilt_from_effective_entries(
        self, entries, interests, not_interests
    ):
        doc = _doc(entries)
        assert doc.interests == interests
        assert doc.not_interests == not_interests

    def test_one_effective_entry_per_concept(self):
        doc = _doc(
            [
                _entry(_coded("T1", "A"), None, method="inferred"),
                _entry(_coded("T1", "A"), 0.5),
                _entry(_coded("D1", "A", MESH), 0.5),
            ]
        )
        assert len(effective_interests(doc.research_interests)) == 2

    def test_plain_lists_stay_when_there_are_no_typed_entries(self):
        doc = ProfileDocument(
            name="Plain", rid="local:plain-a1b2c3", provenance="self_published", interests=["x"]
        )
        assert doc.interests == ["x"]


class TestMerge:
    def test_a_rebuild_replaces_its_own_entries_and_keeps_the_rest(self):
        from researcher_profiles.schema import merge_generated_interests

        doc = _doc(
            [
                _entry(
                    _coded("T1", "Old topic"),
                    None,
                    method="inferred",
                    generator="openalex-topics@2025-01",
                ),
                _entry(_coded("T2", "Declared"), -1, generator="openalex-topics@2025-01"),
                _entry(
                    {"label": "graphs", "unmapped": True}, 0.5, method="inferred", generator="llm"
                ),
            ]
        )
        new = ResearchInterest.model_validate(
            _entry(
                _coded("T3", "New topic"),
                None,
                method="inferred",
                generator="openalex-topics@2026-09",
            )
        )
        merged = merge_generated_interests(
            doc.research_interests, [new], generator_prefix="openalex-topics"
        )
        assert [e.concept.text for e in merged] == ["Declared", "graphs", "New topic"]


class TestOwnerEdit:
    def test_patching_the_plain_lists_writes_declared_entries(self, jane_doe, jane_doe_dir):
        jane_doe.edit.patch_metadata({"interests": ["single-cell"], "not_interests": ["admin"]})
        reloaded = ResearcherProfile.from_files(jane_doe_dir).metadata
        assert reloaded.interests == ["single-cell"]
        assert reloaded.not_interests == ["admin"]
        declared = {
            (e.concept.label, e.weight, e.generator)
            for e in reloaded.research_interests
            if e.method == "declared"
        }
        assert declared == {("single-cell", 0.5, "user"), ("admin", -0.5, "user")}

    def test_a_patch_to_one_list_keeps_the_other_and_coded_entries(self, jane_doe, jane_doe_dir):
        jane_doe.edit.patch_metadata(
            {
                "research_interests": [
                    _entry(_coded("T1", "Genomics"), 0.9),
                    _entry({"label": "grant admin", "unmapped": True}, -0.5),
                ]
            }
        )
        doc = jane_doe.edit.patch_metadata({"interests": ["Genomics", "graphs"]})
        assert doc.interests == ["Genomics", "graphs"]
        assert doc.not_interests == ["grant admin"]
        assert ResearcherProfile.from_files(jane_doe_dir).metadata.interests == doc.interests

    def test_dropping_a_coded_label_declares_it_unknown(self, jane_doe):
        jane_doe.edit.patch_metadata(
            {"research_interests": [_entry(_coded("T1", "Genomics"), 0.9, method="inferred")]}
        )
        doc = jane_doe.edit.patch_metadata({"interests": []})
        assert doc.interests == []
        winner = effective_interests(doc.research_interests)[0]
        assert (winner.method, winner.weight) == ("declared", None)


class TestJsonLd:
    def test_context_maps_concepts_to_skos_and_weight_to_wi(self):
        ctx = json.loads(
            (REPO_ROOT / "src/researcher_profiles/context/v1.jsonld").read_text(encoding="utf-8")
        )["@context"]
        entry = ctx["rp:researchInterests"]
        assert entry["@container"] == "@set"
        scoped = entry["@context"]
        concept = scoped["concept"]["@context"]
        assert concept["system"] == {"@id": "skos:inScheme", "@type": "@id"}
        assert (concept["code"], concept["display"]) == ("skos:notation", "skos:prefLabel")
        assert scoped["weight"]["@id"] == "wi:weight"
        assert ctx["skos"] == "http://www.w3.org/2004/02/skos/core#"
        assert ctx["wi"] == "http://purl.org/ontology/wi/core#"
        assert ctx["ResearchInterest"] == "rp:ResearchInterest"

    def test_foaf_view_carries_positive_weights_only(self):
        doc = _doc(
            [
                _entry(_coded("T1", "Core"), 1),
                _entry(_coded("T2", "Neutral"), 0),
                _entry(_coded("T3", "Unknown"), None, method="inferred"),
                _entry(_coded("T4", "Excluded"), -1),
                _entry({"label": "graphs", "unmapped": True}, 0.5),
            ]
        )
        foaf = to_foaf(doc)
        assert foaf["foaf:topic_interest"] == [
            {"@id": "https://openalex.org/T1", "skos:prefLabel": "Core"},
            {"skos:prefLabel": "graphs"},
        ]
        assert "knowsAbout" not in json.dumps(foaf)
