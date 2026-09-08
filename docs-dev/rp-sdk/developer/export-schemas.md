# How to export JSON Schemas

The `schemas/` directory holds JSON Schema files generated from the package's
Pydantic models. Regenerate them after any model change; consumers validate
against the checked-in files without installing the package.

!!! info "Prerequisites"
    - `researcher-profiles` is installed (core is enough)

## Export from the CLI

Write one `<model>.schema.json` file per model into a directory:

```bash
rp schema export schemas/
```

Expected output:

```
wrote schemas/profile_jsonld.schema.json
wrote schemas/papers_jsonld.schema.json
wrote schemas/grants_jsonld.schema.json
wrote schemas/summary_file.schema.json
wrote schemas/embedding_index.schema.json
wrote schemas/profile_list.schema.json
wrote schemas/registry_bundle.schema.json
wrote schemas/topic_index.schema.json
wrote schemas/profile_export_bundle.schema.json
```

The command creates the output directory if it does not exist and overwrites any
existing schema files. Run it whenever you change a model in
`researcher_profiles.schema` so the checked-in files stay in sync.

A second subcommand writes a single combined HTTP wire-contract schema,
consumed by `rp-ui-lib/scripts/gen-types.mjs` to generate the browser's
TypeScript types:

```bash
rp schema export-wire rp-ui-lib/schemas/wire.schema.json
```

## Export programmatically

Use `export_schemas` to write the files, or `build_schemas` to get the schema
dicts in memory:

```python
from researcher_profiles.schema_export import export_schemas, build_schemas

written = export_schemas("schemas")  # returns list[Path]
print([p.name for p in written])

schemas = build_schemas()  # returns {name: json_schema_dict}
print(sorted(schemas))
```

## What each file validates

| File | Validates | Source model |
|---|---|---|
| `profile_jsonld.schema.json` | `profile.jsonld` | `ProfileDocument` |
| `papers_jsonld.schema.json` | `sources/papers.jsonld` | `PapersDocument` |
| `grants_jsonld.schema.json` | `sources/grants.jsonld` | `GrantsDocument` |
| `summary_file.schema.json` | `sources/summaries/*.summary.md` frontmatter | `SummaryFile` |

The published schemas emit the JSON-LD names (`@id`, `@context`, `conformsTo`,
`sameAs`, `hasPart`), not the Python attribute names, and they keep
`additionalProperties: true`: conformance means the baseline fields are present
and well-formed, never that no other keys exist.

See the [schema reference](../../../docs/rp-sdk/reference/schemas.md) for the fields in each model.

## Using the schemas without the package

A consumer can validate a profile against the checked-in files
without installing this package, using any JSON Schema validator in any language.
Load the schema file and the target artifact and validate. See
[Validate a profile](../../../docs/rp-sdk/how-to/validate-a-profile.md#validate-individual-files-against-json-schema)
for a Python example with the `jsonschema` package.
