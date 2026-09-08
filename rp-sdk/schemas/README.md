# schemas

JSON Schema files exported from the SDK's Pydantic models, one per wire type
(`profile_jsonld`, `papers_jsonld`, `grants_jsonld`, `summary_file`,
`embedding_index`, and the export/registry bundles).

These files are **generated and committed**. Do not hand-edit them. They are the
schema definition in a portable, language-neutral form, for external tooling and
for the browser validator (`rp-browser`, via Ajv). The Python validator does not
read them: it validates a profile directly against the Pydantic models, and
these exports are how that same shape is published for everything that is not
Python.

Regenerate them after changing a model:

```bash
rp schema export schemas/
```
