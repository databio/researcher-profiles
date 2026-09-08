# scholarcore

A small, dependency-free shared vocabulary for academic data. It defines the
common nouns of academic work as tolerant Pydantic models with 
canonical identifiers, so independent systems can agree on the shape of a
person, a paper, an award, or a funding opportunity without agreeing on
anything else (storage, API, UI).

This package ships from the
[researcher-profiles](https://github.com/databio/researcher-profiles) monorepo
as its own independent distribution: nothing here imports `researcher_profiles`,
and either package installs without the other. They share a repo because the
person record is the join point between them. See
[the docs](https://github.com/databio/researcher-profiles/tree/master/scholarcore/docs/index.md) for what it defines and how to extend it.

Not published to PyPI yet.

## Stability

Unstable: may change or remove model fields, enum values, and generated schemas without warning.

## Entities

| Entity | Canonical id | Module |
|---|---|---|
| `Person` / `Researcher` | `rid` | `scholarcore.person` |
| `Paper` | normalized DOI (primary), PMID (secondary) | `scholarcore.biblio` |
| `Award` | `application_id` | `scholarcore.funding` |
| `Opportunity` | `opportunity_number` | `scholarcore.funding` |
| `Organization` | none (optional entity) | `scholarcore.org` |

Each entity also has a lightweight `XRef` pointer (`PersonRef`, `PaperRef`,
`AwardRef`, `OpportunityRef`) carrying the join key plus a cached title or
name, for referencing a record without embedding it.

`scholarcore/__init__.py` imports every submodule eagerly, so `import
scholarcore.funding` pulls in the whole package, not only `Award` and
`Opportunity`. Importing from the submodule directly saves only a name
lookup, not an import cost.

## The four canonical identifiers

- `rid`: a person's identity, a canonical ORCID (`0000-0002-1825-0097`,
  validated by regex and ISO 7064 MOD 11-2 checksum) or an explicitly-prefixed
  `local:<slug>-<hex>` id for a person with no ORCID. There is no separate
  `orcid` field; it is derived from `rid` via `orcid_of()`.
- Normalized DOI: a paper's identity, with any `doi.org/` resolver prefix and
  scheme stripped. Case is preserved, because some sources (OpenAlex among
  them) carry the publisher's original case; DOIs are case-insensitive, so
  compare with `.lower()` when joining. See `normalize_doi()`.
- `application_id`: an award's identity, assigned by whatever external
  system of record tracks the application.
- `opportunity_number`: a funding opportunity's identity, assigned by
  the funding agency (e.g. `PA-25-168`).

## The contract: baseline required, extras allowed

Every model extends `ScholarModel`, which sets `extra="allow"`. A consumer
that owns richer domain data is expected to:

1. Extend these models with its own fields (or store its data alongside
   them, joined on the canonical id).
2. Tolerate fields it doesn't recognize: never raise on an unknown key
   or an unknown enum value it doesn't have a case for.

This package defines the baseline every consumer can agree on; it does not
try to be everyone's full data model.

## JSON Schema

Every model can also be exported as JSON Schema, so a non-Python consumer can
validate a document without installing this package:

```python
from scholarcore.schema_export import export_schemas

export_schemas("schemas")
```

The generated schemas are checked into `schemas/` next to this file, and a
test asserts they have not drifted from the models.

## Installing

Not on PyPI. Install from a checkout of the monorepo:

```bash
pip install -e ./scholarcore          # from the repo root
pip install -e "./scholarcore[test]"  # + pytest and jsonschema
```

`requires-python` is `>=3.12`, the same floor as rp-sdk.

## Development

Run these from the repo root, not from this directory: the test runner and
the lint config both live there and cover the whole tree.

```bash
pip install -e "./scholarcore[test]"
./scripts/test-all.sh scholarcore    # or `./scripts/test-all.sh` for every Python suite
ruff check .
ruff format .
```

There is no `[tool.ruff]` section in this package's `pyproject.toml` on
purpose. The monorepo's root `ruff.toml` is the single lint config, and a
local one would silently diverge from the rest of the repo.
