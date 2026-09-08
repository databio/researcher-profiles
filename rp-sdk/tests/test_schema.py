"""The published format: what the fields mean, what bytes they serialize to.

Three layers of one contract: the JSON Schema handed to outside consumers,
the field rules the pydantic models enforce (``provenance`` above all), and
the canonical JSON-LD serialization that keeps a version-controlled profile
diff-clean.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from researcher_profiles.schema import (
    GrantRecord,
    PaperRecord,
    PapersDocument,
    ProfileDocument,
)
from researcher_profiles.schema.jsonld import KEY_ORDER, canonical_dumps, canonicalize
from researcher_profiles.schema_export import build_schemas, export_schemas

# --------------------------------------------------------------------------
# Exported JSON Schema
# --------------------------------------------------------------------------


class TestSchemaExport:
    """Tests for JSON Schema export."""

    def test_build_schemas_covers_core_models(self):
        schemas = build_schemas()
        assert {
            "profile_jsonld",
            "papers_jsonld",
            "grants_jsonld",
        } <= set(schemas)
        required = set(schemas["profile_jsonld"]["required"])
        # `provenance` has no default anywhere in the format: an unlabeled
        # published assertion is exactly what the field exists to prevent, so it
        # must show up as REQUIRED in the published contract too.
        assert {"name", "rid", "provenance"} <= required

    def test_schemas_use_jsonld_aliases_not_python_names(self):
        """The published contract must speak JSON-LD, not pydantic attributes.

        ``model_json_schema()`` defaults to ``by_alias=True``. Pinning that here
        means a future model-config change cannot silently publish ``conforms_to``
        / ``id_`` / ``same_as`` into the contract consumers validate against.
        """
        props = build_schemas()["profile_jsonld"]["properties"]
        assert {"@context", "@id", "@type", "conformsTo", "sameAs", "hasPart"} <= set(props)
        assert not {"conforms_to", "id_", "type_", "same_as", "has_part"} & set(props)

    def test_published_documents_allow_extra_keys(self):
        """Baseline required, extras allowed, at every depth.

        Conformance means the core baseline fields are present and well-formed;
        additional keys are permitted and preserved. A schema that closed the
        vocabulary would make every third-party and older document invalid.

        Nested nodes get no carve-out. Asserting only the document root is what
        let ``additionalProperties: false`` ship on every ``$defs`` entry, which
        made half the authored corpus unloadable: a publisher could extend the
        root but not a ``Training`` entry. Spec ``linked-data.md`` section 9
        draws no such distinction, so neither does this test.
        """
        for name in ("profile_jsonld", "papers_jsonld", "grants_jsonld"):
            schema = build_schemas()[name]
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

    def test_grants_schema_shape(self):
        """grants_jsonld mirrors papers_jsonld: a Collection with hasPart records."""
        schema = build_schemas()["grants_jsonld"]
        assert schema["type"] == "object"
        assert "hasPart" in schema["properties"]
        record = schema["$defs"]["GrantRecord"]
        assert set(record["required"]) == {"id", "name"}

    def test_deep_input_fields_live_in_the_build_sidecar(self):
        """The four supplied-input fields that define the deep level are build state.

        They are inputs to the build, not part of the published record: what a
        profile publishes is the result (a CV manifest entry, web pages, grants).
        """
        from researcher_profiles.build_state import BuildState

        schemas = build_schemas()
        # BuildState is not in the exported schema set: it is
        # build-session bookkeeping, never part of the published record.
        assert "build_state" not in schemas
        props = BuildState.model_json_schema()["$defs"]["BuildInputs"]["properties"]
        assert {"grants_source", "reporter_supplement", "cv_source", "websites"} <= set(props)
        assert props["reporter_supplement"]["default"] is False
        assert props["websites"]["default"] == []
        assert not {"grants_source", "cv_source", "websites"} & set(
            schemas["profile_jsonld"]["properties"]
        )

    def test_level_field_in_profile_schema(self):
        props = build_schemas()["profile_jsonld"]["properties"]
        assert "level" in props
        # Ordered enum lite/full/deep, defaulting to full.
        assert set(props["level"]["enum"]) == {"lite", "full", "deep"}
        assert props["level"]["default"] == "full"

    def test_career_stage_sources_checked_in_schema(self):
        """``sources_checked`` disambiguates null facts (checked-and-absent vs
        never-looked), and the LLM prompt contract is rendered from
        ``Field(description=...)``, so the field must carry its semantics in the
        published schema, not only in Python."""
        for name in ("profile_jsonld",):
            props = build_schemas()[name]["$defs"]["CareerStage"]["properties"]
            prop = props["sources_checked"]
            assert prop["items"]["enum"] == [
                "grants",
                "publications",
                "training",
                "employment",
            ]
            assert prop["default"] == []
            assert "established absence" in prop["description"]

    def test_career_stage_sources_checked_round_trips_through_model(self):
        from researcher_profiles.schema import CareerStage

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

    def test_export_writes_files(self, tmp_path):
        written = export_schemas(tmp_path)
        assert written
        for p in written:
            assert p.exists()
            data = json.loads(p.read_text())
            assert "properties" in data or "$defs" in data

    def test_export_matches_committed_schemas(self, tmp_path):
        """The committed ``schemas/*.json`` must be byte-identical to a fresh
        export. A failure means the models drifted from the published
        JSON-Schema: re-run ``rp schema export schemas/`` and commit the diff.
        """
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
        """Pin ``schema_fingerprint()`` to a committed value. A change here is a
        contract change to the on-disk artifact schemas. Update this golden and
        re-export ``schemas/`` together.
        """
        from researcher_profiles.validate import schema_fingerprint

        assert schema_fingerprint() == (
            "9c4f79c3848fb83c7d1bbee413fde015c4b12908561c454aa6494081769f0b48"
        )

    # ---- Fixture parity: Pydantic + JSON Schema agree on fixtures ----

    def test_validation_module_schema_names_subset_of_build_schemas(self):
        """All schema names used in the validation module exist in build_schemas."""
        from researcher_profiles.validate import _SCHEMA_MODELS

        exported = set(build_schemas())
        for name in _SCHEMA_MODELS:
            assert name in exported, (
                f"validation module uses schema name {name!r} "
                f"not in build_schemas(); available: {sorted(exported)}"
            )


# --------------------------------------------------------------------------
# The provenance field
# --------------------------------------------------------------------------


ORCID = "0000-0002-1825-0097"


LOCAL = "local:charles-darwin-a3f19c"


URL = "https://profiles.example.org/darwin"


NOW = "2026-07-29T00:00:00Z"


def _provenance_doc(**kw):
    base = {"name": "Someone", "rid": ORCID}
    return ProfileDocument.model_validate({**base, **kw})


class TestProvenance:
    """`provenance` is the field that stops a profile being an unearned claim.

    Identity is over-constrained in the wrong place if a real ORCID is the only
    special case: the interesting cases this standard must carry are synthetic
    (an AI agent), historical (Charles Darwin), and third-party. What separates
    "someone built this about a researcher" from "this researcher endorsed it"
    is not the shape of ``rid``: it is ``provenance``, and it has no default.
    """

    def test_provenance_is_required(self):
        """An unlabeled published assertion is what this field exists to prevent."""
        with pytest.raises(ValidationError) as e:
            ProfileDocument.model_validate({"name": "X", "rid": ORCID})
        assert "provenance" in str(e.value)

    @pytest.mark.parametrize("value", ["self_published", "third_party", "synthetic", "historical"])
    def test_accepted_values(self, value):
        rid = LOCAL if value in ("synthetic", "historical") else ORCID
        assert _provenance_doc(provenance=value, rid=rid).provenance == value

    def test_domain_and_key_provenance_are_accepted(self):
        """The headline set is OPEN: the pluralistic identity tiers load."""
        assert _provenance_doc(provenance="domain_verified", url=URL).provenance == (
            "domain_verified"
        )
        assert _provenance_doc(provenance="key_signed", url=URL).provenance == "key_signed"

    def test_unknown_provenance_is_tolerated_not_rejected(self):
        """profile-document.md §4: consumers must ignore unknown enum values.

        An unrecognized headline label loads (with a warning) rather than failing:
        a stranger's newer tier must round-trip through this validator.
        """
        with pytest.warns(UserWarning, match="known values"):
            doc = _provenance_doc(provenance="probably_fine", url=URL)
        assert doc.provenance == "probably_fine"

    # ----------------------------------------------------------------------
    # orcid_verified: the only tier that claims the subject endorsed the profile
    # ----------------------------------------------------------------------

    def test_orcid_verified_happy_path(self):
        doc = _provenance_doc(
            provenance="orcid_verified",
            url=URL,
            verifiedAt=NOW,
            **{"@id": f"https://orcid.org/{ORCID}"},
        )
        assert doc.id_ == f"https://orcid.org/{ORCID}"
        assert doc.orcid == ORCID

    def test_orcid_verified_rejects_a_local_rid(self):
        with pytest.raises(ValidationError, match="requires an ORCID rid"):
            ProfileDocument.model_validate(
                {
                    "name": "X",
                    "rid": LOCAL,
                    "provenance": "orcid_verified",
                    "url": URL,
                    "verifiedAt": NOW,
                }
            )

    def test_orcid_verified_requires_the_orcid_iri_as_id(self):
        with pytest.raises(ValidationError, match="requires @id"):
            _provenance_doc(provenance="orcid_verified", url=URL, verifiedAt=NOW, **{"@id": URL})

    def test_orcid_verified_requires_a_url(self):
        """The claim is the round-trip itself; with no URL there is nothing to round-trip."""
        with pytest.raises(ValidationError, match="requires a url"):
            _provenance_doc(
                provenance="orcid_verified",
                verifiedAt=NOW,
                **{"@id": f"https://orcid.org/{ORCID}"},
            )

    def test_orcid_verified_requires_verified_at(self):
        with pytest.raises(ValidationError, match="requires verifiedAt"):
            _provenance_doc(
                provenance="orcid_verified",
                url=URL,
                **{"@id": f"https://orcid.org/{ORCID}"},
            )

    # ----------------------------------------------------------------------
    # synthetic / historical: fully supported, not degraded
    # ----------------------------------------------------------------------

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

    # ----------------------------------------------------------------------
    # @id resolution
    # ----------------------------------------------------------------------

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

    # ----------------------------------------------------------------------
    # license
    # ----------------------------------------------------------------------

    def test_license_round_trips_under_its_json_ld_name(self):
        doc = _provenance_doc(provenance="third_party", license="https://example.org/licence")
        data = doc.model_dump(mode="json")
        assert data["license"] == "https://example.org/licence"
        assert "license_" not in data
        assert ProfileDocument.model_validate(data).license_ == "https://example.org/licence"


# --------------------------------------------------------------------------
# Canonical JSON-LD serialization
# --------------------------------------------------------------------------


RID = "0000-0002-1825-0097"


def _canonical_doc(**kw) -> ProfileDocument:
    return ProfileDocument(name="Jane Doe", rid=RID, provenance="third_party", **kw)


class TestCanonicalSerialization:
    """Canonical JSON-LD serialization: stable order, clean diffs, no escaping.

    Version-controlled profiles are the point of this format, so re-serializing an
    unchanged document must be a byte-level no-op. Anything else turns every build
    into a spurious diff and makes review of a real change impossible.
    """

    def test_known_keys_come_first_in_declared_order(self):
        scrambled = {
            "zzz_drift": 1,
            "name": "Jane",
            "@type": "Person",
            "aaa_drift": 2,
            "@context": "ctx",
            "rid": RID,
        }
        keys = list(canonicalize(scrambled))
        assert keys[:4] == ["@context", "@type", "name", "rid"]
        # Unknown keys land after the known ones, alphabetically. That is where
        # drift keys from older writers go, deterministically.
        assert keys[4:] == ["aaa_drift", "zzz_drift"]

    def test_key_order_is_applied_recursively(self):
        nested = {"outer": {"zzz": 1, "@id": "x", "name": "n"}}
        assert list(canonicalize(nested)["outer"]) == ["@id", "name", "zzz"]

    def test_list_order_is_preserved(self):
        """A works list is meaningful; canonicalization must never re-sort it."""
        data = {"hasPart": [{"name": "b"}, {"name": "a"}]}
        assert [p["name"] for p in canonicalize(data)["hasPart"]] == ["b", "a"]

    def test_reserialization_is_a_byte_level_noop(self):
        doc = _canonical_doc(
            affiliation="Example University", expertise=["genomics", "epigenetics"]
        )
        first = canonical_dumps(doc.model_dump(mode="json"))
        reloaded = ProfileDocument.model_validate(json.loads(first))
        assert canonical_dumps(reloaded.model_dump(mode="json")) == first

    def test_papers_document_reserialization_is_a_noop(self):
        doc = PapersDocument(
            about=f"https://orcid.org/{RID}",
            has_part=[
                PaperRecord(title="Ünïcode Paper", year=2021, journal="Nature", doi="10.1/a"),
                PaperRecord(title="Second", year=2019),
            ],
        )
        first = canonical_dumps(doc.model_dump(mode="json"))
        reloaded = PapersDocument.model_validate(json.loads(first))
        assert canonical_dumps(reloaded.model_dump(mode="json")) == first

    def test_unicode_is_not_escaped(self):
        text = canonical_dumps({"name": "Jörg Müller — 北京"})
        assert "Jörg Müller — 北京" in text
        assert "\\u" not in text

    def test_output_has_fixed_indent_and_trailing_newline(self):
        text = canonical_dumps({"a": {"b": 1}})
        assert text.endswith("\n")
        assert '\n  "a": {\n    "b": 1\n  }\n' in text

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
        """A generated ``@id`` leads the node; it is not appended to the end.

        These records have no ``@id`` on input and their ``_jsonld_node`` hook
        computes one. Pruning runs AFTER the hook, so the key is still sitting
        in the slot the base model declared it in. Prune first and the ``None``
        placeholder would be dropped, and re-assigning would append, which
        ``canonical_dumps`` re-sorts away on disk but ``model_dump_json()``
        hands straight to an API caller.
        """
        assert list(record.model_dump(mode="json")) == expected_keys


# ---------------------------------------------------------------------------
# Python ergonomics
# ---------------------------------------------------------------------------


class TestJsonLdErgonomics:
    """JSON-LD shape lives in the serializer, never in the attribute types.

    ``p.affiliation`` is a ``str``. ``paper.year`` is an ``int``.
    ``paper.journal`` is a ``str``. If reading a profile in Python meant
    unwrapping node objects, the format change would have taxed every caller in
    three repos for the benefit of a crawler none of them are.
    """

    @staticmethod
    def _round_trip(model):
        return type(model).model_validate(json.loads(json.dumps(model.model_dump(mode="json"))))

    def test_year_serializes_as_an_xsd_gyear_string(self):
        paper = PaperRecord(title="T", year=2025)
        assert paper.model_dump(mode="json")["datePublished"] == "2025"
        assert self._round_trip(paper).year == 2025

    def test_journal_serializes_as_a_periodical_node(self):
        paper = PaperRecord(title="T", journal="Nature Methods")
        assert paper.model_dump(mode="json")["isPartOf"] == {
            "@type": "Periodical",
            "name": "Nature Methods",
        }
        assert self._round_trip(paper).journal == "Nature Methods"

    def test_authors_serialize_as_person_nodes(self):
        paper = PaperRecord(title="T", authors=["A Author", "B Author"])
        assert paper.model_dump(mode="json")["author"] == [
            {"@type": "Person", "name": "A Author"},
            {"@type": "Person", "name": "B Author"},
        ]
        assert self._round_trip(paper).authors == ["A Author", "B Author"]

    def test_affiliation_stays_a_string_but_gains_a_node_when_a_ror_exists(self):
        plain = ProfileDocument(
            name="X",
            rid="0000-0002-1825-0097",
            provenance="third_party",
            affiliation="Example University",
        )
        assert plain.model_dump(mode="json")["affiliation"] == "Example University"

        typed = ProfileDocument(
            name="X",
            rid="0000-0002-1825-0097",
            provenance="third_party",
            affiliation="Example University",
            affiliation_id="https://ror.org/0153tk833",
        )
        dumped = typed.model_dump(mode="json")
        assert dumped["affiliation"] == {
            "@type": "Organization",
            "@id": "https://ror.org/0153tk833",
            "name": "Example University",
        }
        # And it comes back as a plain string with the ROR kept aside.
        back = ProfileDocument.model_validate(dumped)
        assert isinstance(back.affiliation, str)
        assert back.affiliation_id == "https://ror.org/0153tk833"

    def test_paper_title_is_title_in_python_and_name_on_disk(self):
        paper = PaperRecord(title="A Paper")
        assert paper.title == "A Paper"
        assert paper.model_dump(mode="json")["name"] == "A Paper"
        assert PaperRecord.model_validate({"name": "A Paper"}).title == "A Paper"

    def test_paper_id_resolution_order(self):
        assert PaperRecord(title="T", doi="10.1/x").resolve_id() == "https://doi.org/10.1/x"
        assert (
            PaperRecord(title="T", doi="https://doi.org/10.1/x").resolve_id()
            == "https://doi.org/10.1/x"
        )
        assert (
            PaperRecord(title="T", openalex_id="W123").resolve_id() == "https://openalex.org/W123"
        )
        assert PaperRecord(title="T", paper_id="doe2020x").resolve_id() == "#paper/doe2020x"
