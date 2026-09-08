# How to link researcher connections

The `collaborators` field in `profile.jsonld` records who a researcher has
worked with. These declarations sit alongside the connections the SDK
computes automatically from co-authorship, shared institutions, and training
records. Conflict-of-interest (COI) checking and reviewer matching read only
the computed connections, not this field (see [COI checking](#coi-checking)
below). This guide shows how to declare them.

!!! info "Prerequisites"
    - A profile directory with `profile.jsonld`
    - The researchers to link (ideally with affiliation and relationship)

## The `collaborators` field

`collaborators` is a top-level array in `profile.jsonld`, mapped to
`schema:knows` in the JSON-LD context (see the field-by-field mapping table in
[the linked-data spec](../../rp-spec/index.md#vocabulary)). The same array
accepts plain strings (a name alone) or structured objects
(`{name, affiliation, relationship}`). Connections are one-way: if profile A
lists B as a collaborator, B does not automatically list A. Each profile
declares its own connections independently.

## String form (name only)

```json
"collaborators": ["Alice Smith", "Bob Jones"]
```

Use this when you only need to record names without detail. Name-only entries
can still be matched by name normalization, but structured entries are more
reliable: an affiliation disambiguates two people who share a name.

## Object form (name + affiliation + relationship)

```json
"collaborators": [
  {
    "name": "Alice Smith",
    "affiliation": "Example University",
    "relationship": "coauthor"
  },
  {
    "name": "Bob Jones",
    "affiliation": "Another Institute",
    "relationship": "advisor"
  }
]
```

Each field:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | The person's display name |
| `affiliation` | string | Their institutional affiliation |
| `relationship` | string | How they are connected, e.g. `coauthor`, `advisor` |

## Common relationship types

`relationship` is not a closed enum; the field accepts any descriptive
string. These are the common values:

| Value | Meaning |
|---|---|
| `coauthor` | Has co-authored papers with this researcher |
| `advisor` | Served as this researcher's advisor (doctoral, postdoctoral) |
| `advisee` | Was advised by this researcher |
| `colleague` | Works at the same institution or in the same group |
| `collaborator` | General research collaboration |

The COI system specifically recognizes three relationship types when it
computes edges on its own: `coauthor`, `shared_institution`, and `advised`.
See [COI checking](#coi-checking) below.

## Mixing both forms

An array can freely mix strings and objects:

```json
"collaborators": [
  "Alice Smith",
  {
    "name": "Bob Jones",
    "affiliation": "Another Institute",
    "relationship": "advisor"
  }
]
```

## How collaborators appear in profile.jsonld

`collaborators` sits alongside the rest of a profile's metadata:

```json
{
  "@context": "https://profiles.databio.org/context/v1.jsonld",
  "@id": "https://orcid.org/0000-0002-1825-0097",
  "@type": "Person",
  "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
  "name": "Jane Doe",
  "rid": "0000-0002-1825-0097",
  "provenance": "third_party",
  "level": "full",
  "affiliation": "Example University",
  "field": "Computational Biology",
  "collaborators": [
    { "name": "Alice Smith", "affiliation": "Example University", "relationship": "coauthor" }
  ]
}
```

In the canonical key order the SDK writes on serialization, `collaborators`
appears after `researchOutputs` and before `anchor`.

## COI checking

The `ProfileGraph` (`researcher_profiles.graph`) computes conflict-of-interest
edges (`coauthor`, `shared_institution`, and `advised`) automatically, from
already-published bibliometric data: paper author lists, affiliation/career/
training history, and `training[].advisor` records. This is what
`ProfileGraph.coi_edges()` and the `POST /coi/check` API endpoint use; it does
not currently read the `collaborators` field.

Declaring `collaborators` on a profile is documentary: it is part of the
published record (mapped to `schema:knows`), visible to any consumer reading
the profile. It is useful to external tools, or to a future version of the
graph, that want a researcher's own account of their connections alongside the
computed data. It supplements what the automated graph can see, for example
a mentorship or collaboration that predates any co-authored paper and left no
institutional trace.

