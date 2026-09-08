"""JSON Schema drift guard and fixture validation.

Two things are asserted:

1. The schemas checked into ``schemas/`` match what
   :func:`scholarcore.schema_export.build_schemas` produces right now. A
   model change that isn't accompanied by regenerating ``schemas/`` fails
   here instead of shipping a stale contract.
2. Those schemas validate the canonical fixtures, and reject malformed
   input.
"""

import json
from pathlib import Path

import jsonschema
import pytest

from scholarcore.schema_export import build_schemas

PACKAGE_ROOT = Path(__file__).parent.parent
SCHEMAS_DIR = PACKAGE_ROOT / "schemas"
FIXTURES_DIR = Path(__file__).parent / "fixtures"

_CURRENT = build_schemas()


def test_schemas_directory_has_no_extra_or_missing_files():
    on_disk = {p.stem.removesuffix(".schema") for p in SCHEMAS_DIR.glob("*.schema.json")}
    expected = set(_CURRENT)
    assert on_disk == expected


@pytest.mark.parametrize("name", sorted(_CURRENT))
def test_committed_schema_matches_current_model(name):
    committed = json.loads((SCHEMAS_DIR / f"{name}.schema.json").read_text())
    assert committed == _CURRENT[name], (
        f"schemas/{name}.schema.json is stale; regenerate with "
        "scholarcore.schema_export.export_schemas('schemas')"
    )


@pytest.mark.parametrize(
    "name,fixture",
    [
        ("person", "person"),
        ("paper", "paper"),
        ("award", "award"),
        ("opportunity", "opportunity"),
    ],
)
def test_schema_validates_canonical_fixture(name, fixture):
    schema = _CURRENT[name]
    data = json.loads((FIXTURES_DIR / f"{fixture}.json").read_text())
    jsonschema.validate(instance=data, schema=schema)


def test_schema_rejects_malformed_paper_missing_title():
    schema = _CURRENT["paper"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={"doi": "10.1234/example"}, schema=schema)


def test_schema_rejects_malformed_award_wrong_type():
    schema = _CURRENT["award"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={"title": 12345}, schema=schema)


def test_schema_rejects_malformed_opportunity_missing_number():
    schema = _CURRENT["opportunity"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={"agency": "NIH"}, schema=schema)
