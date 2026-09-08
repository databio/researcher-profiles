# Model reference

Every field of every scholarcore model.

All models extend `ScholarModel`, which sets `extra="allow"`,
`populate_by_name=True`, and `str_strip_whitespace=True`. Every model therefore
also accepts and preserves fields not listed here.

Import from the top level (`from scholarcore import Paper`) or from the
submodule (`from scholarcore.biblio import Paper`). `scholarcore/__init__.py`
imports every submodule eagerly, so both forms load the whole package; the
submodule form only saves a name lookup.

## The `Ref` pattern

Each core entity has a matching `XRef` model (`PersonRef`, `PaperRef`,
`AwardRef`, `OpportunityRef`) holding the join key plus one cached display
field. Use a ref when one record points at another without needing the full
record: a paper with twenty authors carries twenty `PersonRef`s, not twenty
`Researcher` records. It is the foreign-key pattern, with one denormalized
field so a display does not need a lookup.

## Identifier functions (`scholarcore.identity`)

| Function | Returns |
|---|---|
| `validate_rid(v)` | The rid unchanged. Raises `ValueError` naming what failed: the ORCID pattern, the ORCID check digit, or the `local:` pattern. |
| `is_rid(v)` | `True` when `v` is a well-formed rid of either form. Never raises. |
| `is_local(rid)` | `True` when the rid is a locally-minted id rather than an ORCID. |
| `orcid_of(rid)` | The ORCID carried by the rid; `None` for a local id or an empty value. |
| `mint_local_rid(name)` | A new `local:<slug>-<6 hex>` rid, e.g. `local:josiah-carberry-a3f19c`. |
| `normalize_doi(value, *, lowercase=False)` | The bare DOI, with `https://doi.org/`, `http://dx.doi.org/`, or `doi:` stripped. Case is preserved unless `lowercase=True`. `None` for an empty value. |
| `normalize_pmid(value)` | The bare numeric PMID as a string, with an older `ncbi.nlm.nih.gov/pubmed/` URL prefix or a `pmid:` prefix stripped. `None` for an empty value or one carrying no digits. |

See [Canonical identifiers](../identifiers.md) for what each identifier is and
when to mint a local rid.

## `PersonRef` (`scholarcore.person`)

A pointer to a person: the join key plus a cached name. Carried by systems that
do not own person records.

| Field | Type | Notes |
|---|---|---|
| `rid` | `str` | Required. Validated by `validate_rid`. |
| `name` | `str \| None` | A cached display name, for rendering without a lookup. Not authoritative. |

## `Person` (`scholarcore.person`)

`PersonRef` plus biographical and contact fields.

| Field | Type | Notes |
|---|---|---|
| *(inherits `rid`, `name`)* | | |
| `given_name` | `str \| None` | |
| `family_name` | `str \| None` | |
| `affiliations` | `list[Affiliation] \| None` | Appointments and roles, past and current. |
| `email` | `str \| None` | |

## `Researcher` (`scholarcore.person`)

`Person` plus the fields specific to someone who does research. It is the
shared shape a researcher record takes across systems. researcher-profiles
reuses `Training` and `CareerEntry` from here directly.

| Field | Type | Notes |
|---|---|---|
| *(inherits `rid`, `name`, and the `Person` fields)* | | |
| `training` | `list[Training]` | Default `[]`. Degrees, postdocs, clinical training. |
| `career` | `list[CareerEntry]` | Default `[]`. Positions held. |
| `field` | `str \| None` | Primary research field or discipline. |
| `subfields` | `list[str]` | Default `[]`. More specific areas within the field. |
| `summary` | `str \| None` | A brief biographical summary. |

### `Training`

One training or education entry.

| Field | Type | Notes |
|---|---|---|
| `kind` | `"degree" \| "postdoc" \| "clinical_training"` | Required. A postdoc is a span, never a degree. |
| `degree` | `str` | Required. The degree or training title. |
| `institution` | `str` | Required. |
| `year_start` | `int \| None` | Optional for a degree. |
| `year_end` | `int \| None` | For a degree, the completion year. |
| `advisor` | `str \| None` | |
| `field` | `str \| None` | Field of study. |

### `CareerEntry`

One career or employment entry.

| Field | Type | Notes |
|---|---|---|
| `role` | `str` | Required. The position title. |
| `institution` | `str` | Required. |
| `start_year` | `int \| None` | `None` when the start is genuinely unknown. |
| `end_year` | `int \| None` | `None` means the position is still held. |

## `Organization` (`scholarcore.org`)

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | Required. |
| `ror_id` | `str \| None` | [Research Organization Registry](https://ror.org) id. |
| `type` | `str \| None` | Free text, e.g. `"university"`, `"funder"`, `"nonprofit"`. |
| `address` | `str \| None` | Free text. |

## `Affiliation` (`scholarcore.affil`)

One person's relationship to an organization. Serves both a `Person`'s
appointment (with title and dates) and an `Authorship`'s institution (usually
organization and department only).

| Field | Type | Notes |
|---|---|---|
| `organization` | `Organization` | Required. |
| `department` | `str \| None` | Free text. Most departments have no ROR id, so this is not a nested `Organization`. |
| `title` | `str \| None` | The role, e.g. `"Professor"`. |
| `start` | `date \| None` | |
| `end` | `date \| None` | Absent means current. |

`Affiliation.current` is a property, `True` when `end` is unset. It is derived,
never stored.

A bare string is accepted in place of the whole object and loads as a
low-fidelity affiliation: `"MIT"` becomes `{"organization": {"name": "MIT"}}`.

## `PaperRef` (`scholarcore.biblio`)

A pointer to a paper: the DOI plus a cached title.

| Field | Type | Notes |
|---|---|---|
| `doi` | `str` | Required. The join key. Prefix stripped on load; case preserved. |
| `title` | `str \| None` | A cached title, for rendering without a lookup. |

## `Paper` (`scholarcore.biblio`)

| Field | Type | Notes |
|---|---|---|
| `title` | `str` | Required. |
| `authors` | `list[Authorship] \| None` | |
| `year` | `int \| None` | |
| `venue` | `str \| None` | Journal or conference. |
| `doi` | `str \| None` | Normalized on load: resolver prefix stripped, case preserved. |
| `pmid` | `str \| None` | Normalized on load. A value carrying no digits loads as `None`. |
| `pmcid` | `str \| None` | |
| `openalex_id` | `str \| None` | |
| `arxiv_id` | `str \| None` | |
| `abstract` | `str \| None` | |

There is no full-text field. Full text is a storage and retrieval concern for
the system that keeps it.

## `Authorship` (`scholarcore.biblio`)

One person's authorship of one work.

| Field | Type | Notes |
|---|---|---|
| `person` | `PersonRef` | Required. |
| `position` | `int \| None` | Byline order. Pick 0- or 1-based numbering and use it consistently; scholarcore does not impose one. |
| `corresponding` | `bool` | Default `False`. |
| `equal_contribution` | `bool` | Default `False`. |
| `affiliations` | `list[Affiliation]` | Default `[]`. The institutions for this work. |
| `credit_roles` | `list[CreditRole]` | Default `[]`. |

Byline rendering (superscript numbering, affiliation dedup across authors,
per-author verification flags) is not modeled here.

### `CreditRole`

The 14 [NISO CRediT](https://credit.niso.org) contributor roles. Values are
NISO's hyphenated slugs, not the Python member names:

`conceptualization`, `data-curation`, `formal-analysis`,
`funding-acquisition`, `investigation`, `methodology`,
`project-administration`, `resources`, `software`, `supervision`,
`validation`, `visualization`, `writing-original-draft`,
`writing-review-editing`.

## `AwardRef` (`scholarcore.funding`)

A pointer to an award: the join key plus a cached title.

| Field | Type | Notes |
|---|---|---|
| `application_id` | `str` | Required. The join key. |
| `title` | `str \| None` | A cached title, for rendering without a lookup. |

## `Award` (`scholarcore.funding`)

A funded or proposed award of financial support.

| Field | Type | Notes |
|---|---|---|
| `application_id` | `str \| None` | The join key, assigned by the external system of record. |
| `title` | `str` | Required. |
| `funder` | `str \| None` | |
| `number` | `str \| None` | The award or grant number. Distinct from `application_id`. |
| `activity_code` | `str \| None` | The funder's mechanism code, e.g. `"R01"`, `"U01"`, `"K99"`. |
| `pi` | `PersonRef \| None` | |
| `co_investigators` | `list[PersonRef]` | Default `[]`. |
| `role` | `GrantRole \| None` | Set when the record represents one person's participation rather than the award as a whole. |
| `status` | `AwardStatus \| None` | An unrecognized value loads as `None`. |
| `submission_type` | `SubmissionType \| None` | An unrecognized value loads as `None`. |
| `start` | `date \| None` | |
| `end` | `date \| None` | |
| `effort` | `float \| None` | FTE fraction, 0-1. |
| `directs` | `int \| None` | Direct costs, whole US dollars. |
| `indirects` | `int \| None` | Indirect costs, whole US dollars. |
| `total` | `int \| None` | Total costs, whole US dollars. |
| `abstract` | `str \| None` | |

`role`, `status`, and `submission_type` load an unrecognized value as `None`
instead of raising. See
[Extend the models](../how-to/extend-the-models.md) for how to keep a
finer-grained vocabulary alongside them.

### `GrantRole`

`pi`, `co_pi`, `co_i`, `other`.

### `AwardStatus`

`planning`, `submitted`, `pending`, `funded`, `active`, `completed`,
`rejected`, `withdrawn`.

### `SubmissionType`

`new`, `renewal`, `resubmission`, `supplement`.

## `OpportunityRef` (`scholarcore.funding`)

A pointer to a funding opportunity: the join key plus a cached title.

| Field | Type | Notes |
|---|---|---|
| `opportunity_number` | `str` | Required. The join key. |
| `title` | `str \| None` | A cached title, for rendering without a lookup. |

## `Opportunity` (`scholarcore.funding`)

A published funding opportunity, a call for applications.

| Field | Type | Notes |
|---|---|---|
| `opportunity_number` | `str` | Required. The join key, e.g. `"PA-25-168"`. |
| `agency` | `str \| None` | |
| `title` | `str \| None` | |
| `url` | `str \| None` | The published announcement. |
| `document_type` | `str \| None` | e.g. `"NOFO"`, `"RFA"`, `"PA"`. |
| `activity_codes` | `list[str]` | Default `[]`. |
| `posted_date` | `date \| None` | |
| `expiration_date` | `date \| None` | |
| `budget_max` | `int \| None` | |
| `purpose` | `str \| None` | |
| `keywords` | `list[str]` | Default `[]`. |

## JSON Schemas

`scholarcore/schemas/` holds one committed JSON Schema per model, generated by
`scholarcore.schema_export.export_schemas()`. `build_schemas()` returns the same
schemas as a `{name: dict}` mapping without writing files:

`affiliation.schema.json`, `authorship.schema.json`, `award.schema.json`,
`opportunity.schema.json`, `organization.schema.json`, `paper.schema.json`,
`person.schema.json`, `person_ref.schema.json`.

A test compares the committed files against the models and fails when they
differ. The `XRef` models and `Researcher` have no separate schema file: a ref
is two fields, and `person.schema.json` covers the person hierarchy.
