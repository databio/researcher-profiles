# scholarcore

A shared vocabulary for academic data. scholarcore defines the common nouns of
academic work (Person, Researcher, Paper, Award, Opportunity,
Organization) as
Pydantic models keyed on canonical identifiers, so independent systems can
agree on the shape of a person or a grant without sharing a database, an API,
or a UI.

Use it when two systems need to describe the same researcher, publication, or
grant and then join those records later. Each system stores its own data and
extends the models with its own fields; the shared identifier is what makes the
records line up.

## Install

```bash
pip install -e ./scholarcore          # from a checkout
pip install -e "./scholarcore[test]"  # + pytest and jsonschema
```

The only dependency is `pydantic>=2.6`. `requires-python` is `>=3.12`.

!!! info "Not on PyPI"
    scholarcore is installed from a checkout for now. Pin the version tightly:
    any release before `1.0.0` may change or remove model fields, enum values,
    and generated schemas without a deprecation period. rp-sdk is released
    alongside it and declares `scholarcore>=0.1.0`.

## Entities

| Entity | Canonical identifier | Module |
|---|---|---|
| `Person` / `Researcher` | `rid` | `scholarcore.person` |
| `Paper` | normalized DOI, or PMID | `scholarcore.biblio` |
| `Award` | `application_id` | `scholarcore.funding` |
| `Opportunity` | `opportunity_number` | `scholarcore.funding` |
| `Organization` | none | `scholarcore.org` |
| `Affiliation` | none | `scholarcore.affil` |

Each entity also has a lightweight pointer type (`PersonRef`, `PaperRef`,
`AwardRef`, `OpportunityRef`) holding the join key plus a cached name or
title, so one record can reference another without embedding it.

`scholarcore/__init__.py` imports every submodule eagerly, so `import
scholarcore.funding` pulls in the whole package, not only `Award` and
`Opportunity`. Importing from the submodule directly saves only a name
lookup, not an import cost.

## Quick example

```python
from scholarcore import Award, AwardStatus, PersonRef

award = Award(
    application_id="1R01GM123456",
    title="Chromatin accessibility across cell types",
    funder="NIH",
    activity_code="R01",
    pi=PersonRef(rid="0000-0002-1825-0097", name="Josiah Carberry"),
    status="active",
)

print(award.pi.rid)  # 0000-0002-1825-0097
print(award.status)  # AwardStatus.active
```

Another system holding the same award stores its own fields alongside
`application_id`, and the two records join on that value.

## Documentation map

- [Canonical identifiers](identifiers.md): what identifies each entity, how
  values are normalized, and when to mint a local researcher id.
- [Extend the models](how-to/extend-the-models.md): add your own fields to a
  core model, and read records written by a system you do not control.
- [Model reference](reference/models.md): every field of every model, the
  enum vocabularies, and the generated JSON Schemas.

`Person` carries identity and contact fields; `Researcher` adds training,
career, and field of study. For the full researcher profile (expertise,
publications, privacy tiers, provenance), see
the [researcher-profiles SDK](https://github.com/databio/researcher-profiles/tree/master/docs/rp-sdk).

## License

MIT.
