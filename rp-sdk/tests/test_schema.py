"""The published format: what the fields mean, what bytes they serialize to.

Three layers of one contract: the exported JSON Schema, the pydantic field rules
(``provenance`` above all), and the canonical JSON-LD serialization that keeps a profile diff-clean.
"""

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from researcher_profiles.build_state import BuildState
from researcher_profiles.schema import (
    CareerStage,
    GrantRecord,
    GrantsDocument,
    PaperRecord,
    PapersDocument,
    ProfileDocument,
)
from researcher_profiles.schema.jsonld import KEY_ORDER, canonical_dumps, canonicalize
from researcher_profiles.schema_export import build_schemas, export_schemas
from researcher_profiles.validate import _SCHEMA_MODELS, schema_fingerprint

ORCID = "0000-0002-1825-0097"
LOCAL = "local:charles-darwin-a3f19c"
URL = "https://profiles.example.org/darwin"
NOW = "2026-07-29T00:00:00Z"
DROP = object()  # in an override dict: remove this key


def _dig(d, path):
    for key in path:
        d = d[key]
    return d


def _provenance_doc(**kw):
    return ProfileDocument.model_validate({"name": "Someone", "rid": ORCID, **kw})


def _canonical_doc(**kw) -> ProfileDocument:
    return ProfileDocument(name="Jane Doe", rid=ORCID, provenance="third_party", **kw)


@pytest.fixture(scope="module")
def schemas():
    return build_schemas()


class TestSchemaExport:
    def test_build_schemas_covers_core_models(self, schemas):
        assert {"profile_jsonld", "papers_jsonld", "grants_jsonld"} <= set(schemas)
        # `provenance` has no default anywhere in the format: an unlabeled published
        # assertion is what the field exists to prevent, so it must be REQUIRED here too.
        assert {"name", "rid", "provenance"} <= set(schemas["profile_jsonld"]["required"])

    def test_schemas_use_jsonld_aliases_not_python_names(self, schemas):
        """A model-config change must not publish ``conforms_to`` / ``id_`` into the contract."""
        props = schemas["profile_jsonld"]["properties"]
        assert {"@context", "@id", "@type", "conformsTo", "sameAs", "hasPart"} <= set(props)
        assert not {"conforms_to", "id_", "type_", "same_as", "has_part"} & set(props)

    def test_published_documents_allow_extra_keys(self, schemas):
        """Extras allowed at every depth: a closed Training entry once made half the corpus unloadable."""
        for name in ("profile_jsonld", "papers_jsonld", "grants_jsonld"):
            schema = schemas[name]
            assert schema.get("additionalProperties") is not False, name
            closed = [
                def_name
                for def_name, definition in schema.get("$defs", {}).items()
                if definition.get("additionalProperties") is False
            ]
            assert not closed, (
                f"{name}: nested definitions {closed} reject unknown keys. "
                "Drift is reported by validate.undeclared_terms, not enforced "
                "by refusing to load the document."
            )

    @pytest.mark.parametrize(
        "name,path,expected",
        [
            ("profile_jsonld", ("properties", "level", "default"), "full"),
            ("profile_jsonld", ("properties", "level", "enum"), ["lite", "full", "deep"]),
            ("grants_jsonld", ("type",), "object"),
            ("grants_jsonld", ("$defs", "GrantRecord", "required"), ["id", "name"]),
            (
                "profile_jsonld",
                ("$defs", "CareerStage", "properties", "sources_checked", "items", "enum"),
                ["grants", "publications", "training", "employment"],
            ),
            (
                "profile_jsonld",
                ("$defs", "CareerStage", "properties", "sources_checked", "default"),
                [],
            ),
        ],
        ids=[
            "level-default",
            "level-enum",
            "grants-is-object",
            "grant-record-required",
            "sources-checked-enum",
            "sources-checked-default",
        ],
    )
    def test_schema_pin(self, schemas, name, path, expected):
        assert _dig(schemas[name], path) == expected

    def test_sources_checked_description_carries_its_semantics(self, schemas):
        """The LLM prompt contract is rendered from the field description, so the schema must carry it."""
        prop = schemas["profile_jsonld"]["$defs"]["CareerStage"]["properties"]["sources_checked"]
        assert "established absence" in prop["description"]

    def test_deep_input_fields_live_in_the_build_sidecar(self, schemas):
        """Supplied-input fields that define the deep level are build state, never published."""
        assert "build_state" not in schemas
        props = BuildState.model_json_schema()["$defs"]["BuildInputs"]["properties"]
        assert {"grants_source", "reporter_supplement", "cv_source", "websites"} <= set(props)
        assert props["reporter_supplement"]["default"] is False
        assert props["websites"]["default"] == []
        assert not {"grants_source", "cv_source", "websites"} & set(
            schemas["profile_jsonld"]["properties"]
        )

    def test_career_stage_sources_checked_round_trips_through_model(self):
        base = {
            "as_of": "2026-08-06",
            "tenure_status": "unknown",
            "independence": "unknown",
            "evidence": "ORCID employments.",
            "confidence": "low",
        }

        # Absent means nothing was checked.
        assert CareerStage.model_validate(base).sources_checked == []
        stage = CareerStage.model_validate({**base, "sources_checked": ["grants"]})
        assert stage.sources_checked == ["grants"]

        with pytest.raises(ValueError):
            CareerStage.model_validate({**base, "sources_checked": ["orcid"]})

    def test_export_matches_committed_schemas(self, tmp_path):
        """A failure means the models drifted: re-run ``rp schema export schemas/`` and commit."""
        repo_schemas = Path(__file__).resolve().parent.parent / "schemas"
        written = export_schemas(tmp_path)

        committed = {p.name for p in repo_schemas.glob("*.schema.json")}
        exported = {p.name for p in written}
        assert exported == committed, (
            "schema file set drifted (exported vs committed); "
            f"only-exported={exported - committed}, "
            f"only-committed={committed - exported}"
        )
        for p in written:
            committed_text = (repo_schemas / p.name).read_text()
            assert p.read_text() == committed_text, (
                f"{p.name} drifted from the committed schema; "
                "re-run `rp schema export schemas/` and commit."
            )

    def test_schema_fingerprint_pinned_to_golden(self):
        """A change here is a contract change: update this golden and re-export ``schemas/`` together."""
        assert schema_fingerprint() == (
            "cc81b19010b469c6ad585e0e150d39c44ab5ec6afed9bf72551181af19c9eec1"
        )

    def test_exported_schema_accepts_what_the_model_writes(self, schemas):
        """A document this package serializes must pass its own JSON Schema.

        Fields like ``datePublished`` and ``author`` hold a flat Python value but are written as a
        typed literal or node; when the schema described the Python type, the browser validator
        flagged every published works file.
        """
        papers = PapersDocument.model_validate(
            {
                "about": {"@id": "https://orcid.org/0000-0002-1825-0097"},
                "hasPart": [
                    {
                        "name": "A paper",
                        "doi": "10.1/x",
                        "datePublished": "2024",
                        "isPartOf": {"@type": "Periodical", "name": "J"},
                        "author": [{"@type": "Person", "name": "A. Author"}],
                    }
                ],
            }
        )
        grants = GrantsDocument.model_validate(
            {
                "about": {"@id": "https://orcid.org/0000-0002-1825-0097"},
                "hasPart": [
                    {
                        "id": "g1",
                        "name": "A grant",
                        "funder": {"@type": "Organization", "name": "NIH"},
                    }
                ],
            }
        )
        profile = _canonical_doc(
            affiliation="University of Somewhere",
            affiliation_id="https://ror.org/000000000",
        )
        for name, doc in (
            ("papers_jsonld", papers),
            ("grants_jsonld", grants),
            ("profile_jsonld", profile),
        ):
            wire = json.loads(canonical_dumps(doc.model_dump(mode="json", by_alias=True)))
            errors = [
                f"{'/'.join(map(str, e.path))}: {e.message}"
                for e in jsonschema.Draft202012Validator(schemas[name]).iter_errors(wire)
            ]
            assert not errors, f"{name}: {errors}"

    def test_validation_module_schema_names_subset_of_build_schemas(self, schemas):
        missing = set(_SCHEMA_MODELS) - set(schemas)
        assert not missing, f"validation module uses {missing} not in build_schemas()"


class TestProvenance:
    """`provenance` stops a profile being an unearned claim; it has no default."""

    def test_provenance_is_required(self):
        with pytest.raises(ValidationError) as e:
            ProfileDocument.model_validate({"name": "X", "rid": ORCID})
        assert "provenance" in str(e.value)

    @pytest.mark.parametrize(
        "value,extra",
        [
            ("self_published", {}),
            ("third_party", {}),
            ("synthetic", {"rid": LOCAL}),
            ("historical", {"rid": LOCAL}),
            ("domain_verified", {"url": URL}),
            ("key_signed", {"url": URL}),
        ],
    )
    def test_accepted_values(self, value, extra):
        """The headline set is OPEN: the pluralistic identity tiers load."""
        assert _provenance_doc(provenance=value, **extra).provenance == value

    @pytest.mark.parametrize(
        "kwargs,wire_key,attr,value",
        [
            (
                {"license": "https://example.org/licence"},
                "license",
                "license_",
                "https://example.org/licence",
            ),
            (
                {"provenanceNote": "self-reported via a structured interview; unverified"},
                "provenanceNote",
                "provenance_note",
                "self-reported via a structured interview; unverified",
            ),
        ],
        ids=["license", "provenanceNote"],
    )
    def test_field_round_trips_under_its_json_ld_name(self, kwargs, wire_key, attr, value):
        doc = _provenance_doc(provenance="self_published", **kwargs)
        assert getattr(doc, attr) == value
        data = doc.model_dump(by_alias=True, mode="json")
        assert data[wire_key] == value
        assert attr not in data
        assert getattr(ProfileDocument.model_validate(data), attr) == value

    def test_provenance_note_follows_provenance(self):
        data = _provenance_doc(provenance="self_published", provenanceNote="n").model_dump(
            by_alias=True
        )
        assert list(data).index("provenanceNote") == list(data).index("provenance") + 1

    def test_unknown_provenance_is_tolerated_not_rejected(self):
        """profile-document.md §4: a stranger's newer tier must round-trip through this validator."""
        with pytest.warns(UserWarning, match="known values"):
            doc = _provenance_doc(provenance="probably_fine", url=URL)
        assert doc.provenance == "probably_fine"

    # orcid_verified: the only tier that claims the subject endorsed the profile

    ORCID_VERIFIED = {
        "provenance": "orcid_verified",
        "url": URL,
        "verifiedAt": NOW,
        "@id": f"https://orcid.org/{ORCID}",
    }

    def test_orcid_verified_happy_path(self):
        doc = _provenance_doc(**self.ORCID_VERIFIED)
        assert doc.id_ == f"https://orcid.org/{ORCID}"
        assert doc.orcid == ORCID

    @pytest.mark.parametrize(
        "override,match",
        [
            ({"rid": LOCAL}, "requires an ORCID rid"),
            ({"@id": URL}, "requires @id"),
            ({"url": DROP}, "requires a url"),
            ({"verifiedAt": DROP}, "requires verifiedAt"),
        ],
        ids=["local-rid", "non-orcid-id", "no-url", "no-verified-at"],
    )
    def test_orcid_verified_rejects(self, override, match):
        kwargs = {**self.ORCID_VERIFIED, **override}
        kwargs = {k: v for k, v in kwargs.items() if v is not DROP}
        with pytest.raises(ValidationError, match=match):
            _provenance_doc(**kwargs)

    @pytest.mark.parametrize("value", ["synthetic", "historical"])
    def test_synthetic_and_historical_require_a_local_rid(self, value):
        with pytest.raises(ValidationError, match="requires a local: rid"):
            _provenance_doc(provenance=value)

    @pytest.mark.parametrize("value", ["synthetic", "historical"])
    def test_synthetic_and_historical_are_first_class(self, value):
        doc = ProfileDocument.model_validate(
            {"name": "Charles Darwin", "rid": LOCAL, "provenance": value}
        )
        assert doc.orcid is None
        assert doc.id_ == "#me"  # the self-referential fragment, not an error

    @pytest.mark.parametrize(
        "kwargs, expected",
        [
            ({"url": URL}, URL),
            # `#me` resolves against wherever the document ends up being served.
            ({}, "#me"),
            (
                {"url": URL, "@id": f"https://orcid.org/{ORCID}"},
                f"https://orcid.org/{ORCID}",
            ),
        ],
        ids=[
            "id_defaults_to_url_when_present",
            "id_defaults_to_the_self_referential_fragment",
            "explicit_id_wins",
        ],
    )
    def test_id_resolution_order(self, kwargs, expected):
        assert _provenance_doc(provenance="third_party", **kwargs).id_ == expected


def _roundtrip_doc():
    return _canonical_doc(affiliation="Example University", expertise=["genomics", "epigenetics"])


def _roundtrip_papers():
    return PapersDocument(
        about=f"https://orcid.org/{ORCID}",
        has_part=[
            PaperRecord(title="Ünïcode Paper", year=2021, journal="Nature", doi="10.1/a"),
            PaperRecord(title="Second", year=2019),
        ],
    )


class TestCanonicalSerialization:
    """Re-serializing an unchanged document must be a byte-level no-op, or every build is a spurious diff."""

    @pytest.mark.parametrize(
        "data,pick,expected_keys",
        [
            (
                {
                    "zzz_drift": 1,
                    "name": "Jane",
                    "@type": "Person",
                    "aaa_drift": 2,
                    "@context": "ctx",
                    "rid": ORCID,
                },
                lambda d: d,
                # Unknown keys land after the known ones, alphabetically: where
                # drift keys from older writers go, deterministically.
                ["@context", "@type", "name", "rid", "aaa_drift", "zzz_drift"],
            ),
            (
                {"outer": {"zzz": 1, "@id": "x", "name": "n"}},
                lambda d: d["outer"],
                ["@id", "name", "zzz"],
            ),
        ],
        ids=["known-keys-first", "applied-recursively"],
    )
    def test_key_order(self, data, pick, expected_keys):
        assert list(pick(canonicalize(data))) == expected_keys

    def test_list_order_is_preserved(self):
        """A works list is meaningful; canonicalization must never re-sort it."""
        data = {"hasPart": [{"name": "b"}, {"name": "a"}]}
        assert [p["name"] for p in canonicalize(data)["hasPart"]] == ["b", "a"]

    @pytest.mark.parametrize(
        "model_cls,make", [(ProfileDocument, _roundtrip_doc), (PapersDocument, _roundtrip_papers)]
    )
    def test_reserialization_is_a_byte_level_noop(self, model_cls, make):
        first = canonical_dumps(make().model_dump(mode="json"))
        reloaded = model_cls.model_validate(json.loads(first))
        assert canonical_dumps(reloaded.model_dump(mode="json")) == first

    @pytest.mark.parametrize(
        "data,must_contain,must_not_contain",
        [
            ({"name": "Jörg Müller — 北京"}, "Jörg Müller — 北京", "\\u"),
            ({"a": {"b": 1}}, '\n  "a": {\n    "b": 1\n  }\n', "\t"),
        ],
        ids=["unicode-not-escaped", "fixed-indent"],
    )
    def test_output_format(self, data, must_contain, must_not_contain):
        text = canonical_dumps(data)
        assert text.endswith("\n")
        assert must_contain in text
        assert must_not_contain not in text

    def test_context_and_id_and_type_lead_every_document(self):
        assert KEY_ORDER[:3] == ("@context", "@id", "@type")
        text = canonical_dumps(_canonical_doc().model_dump(mode="json"))
        lines = [line.strip() for line in text.splitlines()[1:4]]
        assert lines[0].startswith('"@context"')
        assert lines[1].startswith('"@id"')
        assert lines[2].startswith('"@type"')

    def test_none_and_empty_collections_are_pruned(self):
        """An absent key and an empty one say the same thing; only one is written."""
        data = _canonical_doc().model_dump(mode="json")
        assert "critiques" not in data
        assert "summary" not in data
        assert all(v is not None for v in data.values())

    @pytest.mark.parametrize(
        ("record", "expected_keys"),
        [
            (
                GrantRecord(id="g1", name="Big Grant", funder="NIH"),
                ["@id", "@type", "id", "name", "funder"],
            ),
            (
                PaperRecord(title="A Paper", year=2021, doi="10.1/a"),
                ["@id", "@type", "name", "datePublished", "doi"],
            ),
        ],
    )
    def test_a_derived_id_stays_in_id_position(self, record, expected_keys):
        """Pruning runs AFTER the ``_jsonld_node`` hook, so a generated ``@id`` leads the node
        instead of being appended (which ``model_dump_json()`` would hand straight to an API caller).
        """
        assert list(record.model_dump(mode="json")) == expected_keys


class TestJsonLdErgonomics:
    """JSON-LD shape lives in the serializer, never in the attribute types (``p.affiliation`` is a ``str``)."""

    @staticmethod
    def _round_trip(model):
        return type(model).model_validate(json.loads(json.dumps(model.model_dump(mode="json"))))

    @pytest.mark.parametrize(
        "kwargs,wire_key,wire_value,attr",
        [
            ({"year": 2025}, "datePublished", "2025", "year"),
            (
                {"journal": "Nature Methods"},
                "isPartOf",
                {"@type": "Periodical", "name": "Nature Methods"},
                "journal",
            ),
            (
                {"authors": ["A Author", "B Author"]},
                "author",
                [{"@type": "Person", "name": "A Author"}, {"@type": "Person", "name": "B Author"}],
                "authors",
            ),
            ({"title": "A Paper"}, "name", "A Paper", "title"),
        ],
        ids=["year-gyear", "journal-periodical", "authors-person-nodes", "title-is-name"],
    )
    def test_paper_serializes_as_json_ld(self, kwargs, wire_key, wire_value, attr):
        paper = PaperRecord(**{"title": "T", **kwargs})
        assert paper.model_dump(mode="json")[wire_key] == wire_value
        assert getattr(self._round_trip(paper), attr) == getattr(paper, attr)

    def test_affiliation_stays_a_string_but_gains_a_node_when_a_ror_exists(self):
        plain = _canonical_doc(affiliation="Example University")
        assert plain.model_dump(mode="json")["affiliation"] == "Example University"

        typed = _canonical_doc(
            affiliation="Example University", affiliation_id="https://ror.org/0153tk833"
        )
        dumped = typed.model_dump(mode="json")
        assert dumped["affiliation"] == {
            "@type": "Organization",
            "@id": "https://ror.org/0153tk833",
            "name": "Example University",
        }
        # And it comes back as a plain string with the ROR kept aside.
        back = ProfileDocument.model_validate(dumped)
        assert back.affiliation == "Example University"
        assert back.affiliation_id == "https://ror.org/0153tk833"

    @pytest.mark.parametrize(
        "kwargs,expected_id",
        [
            ({"doi": "10.1/x"}, "https://doi.org/10.1/x"),
            ({"doi": "https://doi.org/10.1/x"}, "https://doi.org/10.1/x"),
            ({"openalex_id": "W123"}, "https://openalex.org/W123"),
            ({"paper_id": "doe2020x"}, "#paper/doe2020x"),
        ],
    )
    def test_paper_id_resolution_order(self, kwargs, expected_id):
        assert PaperRecord(title="T", **kwargs).resolve_id() == expected_id


def test_a_preprint_links_to_its_journal_version_under_dcterms_is_version_of():
    """``isVersionOf`` links a preprint to the journal article it became; it must survive a write and a read."""
    target = "https://doi.org/10.3390/bioengineering11030263"
    rec = PaperRecord(name="A preprint", paper_id="doe2023a", is_version_of=target)
    data = rec.model_dump(mode="json")
    assert data["isVersionOf"] == target
    assert PaperRecord.model_validate(data).is_version_of == target
    context_path = (
        Path(__file__).resolve().parent.parent / "src/researcher_profiles/context/v1.jsonld"
    )
    term = json.loads(context_path.read_text())["@context"]["isVersionOf"]
    assert term == {"@id": "dcterms:isVersionOf", "@type": "@id"}
