# How to create a profile

A profile is a directory of static files conforming to [the RP Format](../profile-format.md).
The spec defines the format, not how to produce it. Any process that emits a
conforming directory works, whether you write the files by hand, generate them
with a script, or bootstrap them with an LLM.

!!! info "Prerequisites"
    - Familiarity with JSON
    - See [the tutorial](../tutorial.md) for a full walkthrough

## What a profile directory looks like

At minimum, a profile is a directory containing two files:

```
jane-doe/
├── profile.jsonld
└── sources/papers.jsonld
```

`profile.jsonld` is the record and the manifest; `sources/papers.jsonld` is the
works list, and it may be an empty `Collection` for a researcher with no
publications. `profile.jsonld` lists it in `hasPart` with role `works`. A
directory with those two files, where `profile.jsonld` carries `conformsTo`,
is a conforming `lite` profile. Everything else is optional and additive:

```
jane-doe/
├── profile.jsonld
├── personality/
│   ├── expertise.md
│   └── SOUL.md
└── sources/
    ├── papers.jsonld
    └── summaries/
        └── <paper_id>.summary.md
```

`personality/` and `sources/` enable deeper features (persona methods, search,
citation-backed answers) but are not required to have a valid profile. See
[the tutorial](../tutorial.md) for a full walkthrough of building one of these
directories by hand, file by file.

## The required fields

`profile.jsonld` has three top-level keys with no default. Omit any one and
the document fails to load (see
[the profile document spec](../../rp-spec/index.md#31-the-profilejsonld-document)):

| Key | What it is |
|---|---|
| `name` | The researcher's display name |
| `rid` | A canonical ORCID, or a `local:` id if they have none |
| `provenance` | Who asserted this profile and on what basis (no default) |

Five more keys are required on the wire. Every conforming published document
carries them, but the reference implementation fills them in when absent, so
you can omit them while authoring by hand:

| Key | What it is | Default when absent |
|---|---|---|
| `@context` | Exact string: `https://profiles.databio.org/context/v1.jsonld` | filled in with this value |
| `@id` | The subject IRI: `https://orcid.org/<rid>` for an ORCID `rid`, else the profile's `url`, else `#me` | filled in from `url` or `#me` |
| `@type` | `"Person"` | filled in |
| `conformsTo` | Equals the `@context` IRI and acts as the format gate. A present but wrong value fails to load. | filled in with the current format IRI |
| `level` | `"lite"`, `"full"`, or `"deep"` | filled in as `"full"` |

A minimal valid `profile.jsonld`:

```json
{
  "@context": "https://profiles.databio.org/context/v1.jsonld",
  "@id": "https://orcid.org/0000-0002-1825-0097",
  "@type": "Person",
  "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
  "name": "Jane Doe",
  "rid": "0000-0002-1825-0097",
  "provenance": "third_party",
  "level": "lite",
  "hasPart": [
    {
      "@type": "Collection",
      "name": "Works",
      "role": "works",
      "contentUrl": "sources/papers.jsonld",
      "encodingFormat": "application/ld+json"
    }
  ]
}
```

Everything else (personality documents, summaries, career history,
collaborators) is optional. A `lite` profile with this document and the works
list it names is conforming; it cannot run persona methods or feed a
full-corpus search until it has more. `rp manifest <dir> --write` fills in
`hasPart` from the files on disk, so you do not have to type it.

## Ways to create a profile

- By hand: follow [the tutorial](../tutorial.md) step by step. It builds
  a small profile from scratch and explains each file.
- By script: any code that writes valid JSON (and, optionally, Markdown)
  into the directory works. There is no required tool; validate the output
  against the schema (see below).
- By LLM: paste the prompt below into ChatGPT, Claude, or any other model
  to generate a starter `profile.jsonld`.
- By pipeline: a profile directory can also be produced by an automated
  build tool. The format does not care which tool produced it; only the
  resulting directory is specified. The SDK defines and validates the format; it does not ship a
  build pipeline. See
  [Creation is out of scope](../profile-format.md#creation-is-out-of-scope).

## Generate a starter profile with an LLM

Paste a prompt like this into any chat model, filling in the researcher's name
and ORCID:

```text
Generate a profile.jsonld file for the researcher-profiles format
(https://profiles.databio.org/context/v1.jsonld). The researcher is
<Full Name>, ORCID <0000-0000-0000-0000>.

Return a single JSON object with these required keys:
- "@context": "https://profiles.databio.org/context/v1.jsonld"
- "@id": "https://orcid.org/<their ORCID>"
- "@type": "Person"
- "conformsTo": "https://profiles.databio.org/context/v1.jsonld"
- "name": their full name
- "rid": their ORCID (no URL, just the identifier)
- "provenance": "third_party"
- "level": "lite"

Also fill in whatever of the following you can determine from public
information: "affiliation", "field", "subfields" (array), "summary"
(one paragraph), "expertise" (array of short topic labels).

Output only the JSON object, no commentary.
```

Example output:

```json
{
  "@context": "https://profiles.databio.org/context/v1.jsonld",
  "@id": "https://orcid.org/0000-0002-1825-0097",
  "@type": "Person",
  "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
  "name": "Jane Doe",
  "rid": "0000-0002-1825-0097",
  "provenance": "third_party",
  "level": "lite",
  "affiliation": "Example University",
  "field": "Computational Biology",
  "subfields": ["epigenomics", "chromatin"],
  "summary": "Jane Doe develops methods for region-set analysis.",
  "expertise": ["region-set-analysis", "reproducible-workflows"]
}
```

Save this as `<slug>/profile.jsonld`. LLM-generated metadata should be reviewed
for accuracy before you treat it as authoritative: the model may guess at
affiliation, field, or summary details it has not verified.

## Validate the result

Run the conformance check. It validates every artifact against its schema and
checks the cross-file invariants (manifest drift, summary ids that match a
paper, and so on):

```bash
rp validate jane-doe
```

The same check from Python returns a `ProfileValidationReport`:

```python
from researcher_profiles import ResearcherProfile

report = ResearcherProfile.from_files("jane-doe").validate()
print(report.ok)
```

Loading with `from_files` alone is not a conformance check: it reads each
file lazily and only parses the ones you touch.

See [How to validate a profile](validate-a-profile.md) for schema validation
without installing the package, and for the deeper quality-audit checks.

