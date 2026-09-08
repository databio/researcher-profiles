# How to validate a profile

!!! info "Prerequisites"
    - `researcher-profiles`
    - A profile directory to check

## Schema validation

Schema validation checks whether a profile is well-formed: whether the files
parse and satisfy the Pydantic models. This is available in a core install and
happens automatically when you load a profile.

Load the profile and force every file to be read by passing `eager=True`:

```python
from researcher_profiles import ResearcherProfile, ProfileLoadError

try:
    p = ResearcherProfile.from_files("path/to/jane-doe", eager=True)
    print(f"OK: {p.name}, {len(p.papers)} papers")
except ProfileLoadError as e:
    print(f"invalid: {e}")
    print("offending location:", e.location)
```

`ProfileLoadError` is raised when a file is unreadable, is not valid JSON, or
fails its schema. The exception carries the offending artifact's `location`
and, where available, the `original` underlying exception. `location` is
untyped: the filesystem backend passes a `Path`, a static host
passes a URL, and a database backend passes a table/row reference.

Without `eager=True`, files are read lazily on first access, so a malformed
`papers.jsonld` would not surface until you touch `p.papers`. Use `eager=True` when
you want validation up front.

### Validate individual files against JSON Schema

To validate a profile *without installing this package* (for example from
another language or in CI), use the JSON Schema files in `schemas/` with any JSON
Schema validator. In Python, with the third-party `jsonschema` package
(`pip install jsonschema`):

```python
import json, jsonschema

schema = json.load(open("schemas/profile_jsonld.schema.json"))
data = json.load(open("path/to/jane-doe/profile.jsonld"))
jsonschema.validate(data, schema)  # raises ValidationError on mismatch
```

Every published artifact is JSON, so there is no YAML step.

See [the schema reference](../reference/schemas.md) for which file validates
which artifact.

## Validate a whole directory

`rp validate` walks every artifact in a profile directory, runs the per-artifact
schema check, and then checks the cross-artifact invariants (manifest drift,
orphaned summary files). It exits `0` when the directory conforms and `4` on a
conformance violation.

```bash
rp validate path/to/jane-doe
rp validate path/to/jane-doe --json
```

`ResearcherProfile.validate()` is the same check from Python, and
`from_files(..., validate=True)` runs it at load time and raises
`ProfileValidationError` when the report is not ok:

```python
p = ResearcherProfile.from_files("path/to/jane-doe")
report = p.validate()  # ProfileValidationReport
print(report.ok, len(report.cross_artifact))
```

See the [CLI reference](../reference/cli.md) for the other verbs.

