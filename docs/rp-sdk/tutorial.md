# Getting started

In this tutorial you will build a small researcher profile by hand, load it with
the SDK, explore its contents in Python, and build a semantic index over it. By
the end you will have a working, conforming profile directory and will have
touched the main entry points of the package.

This tutorial assumes you can already install Python packages with `pip` and know
the basics of running Python and editing JSON and Markdown files. You do not need
to know anything about the profile format in advance: you will build one from
scratch.

!!! success "Learning objectives"
    - Create a conforming profile directory from plain JSON and Markdown files
    - Load the profile with `ResearcherProfile.from_files`
    - Read metadata, papers, and summaries in Python
    - Build a per-profile embedding index and run a semantic search

## Install the package

The embedding index needs the `[vectors,st]` extras. Install from a checkout
(see the [SDK overview](index.md#install) for the scholarcore-first step):

```bash
pip install -e "./rp-sdk[vectors,st]"
```

## Step 1: Create the profile directory

A profile is a directory named after a slug (a lowercase, hyphenated
identifier). Create one for a synthetic researcher named Jane Doe:

```bash
mkdir -p jane-doe/personality jane-doe/sources/summaries
cd jane-doe
```

## Step 2: Write the record

Create `profile.jsonld`. This one file is the whole structured record; there is
no second metadata file:

```json
{
  "@context": "https://profiles.databio.org/context/v1.jsonld",
  "@id": "https://orcid.org/0000-0002-1825-0097",
  "@type": "Person",
  "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
  "name": "Jane Doe",
  "rid": "0000-0002-1825-0097",
  "provenance": "third_party",
  "license": "https://creativecommons.org/licenses/by/4.0/",
  "level": "full",
  "affiliation": "Example University",
  "field": "Computational Biology",
  "subfields": ["epigenomics", "chromatin"],
  "summary": "Jane Doe develops methods for region-set analysis.",
  "expertise": ["region-set-analysis", "reproducible-workflows"]
}
```

Four keys matter most:

- `@context`: points at the hosted vocabulary, so every other key has a
  globally-resolvable meaning. The runtime never fetches it.
- `conformsTo`: the format gate. A wrong value fails to load; a missing value
  is filled in with the current format IRI.
- `rid`: the identity. It is either a canonical ORCID or a `local:` id for
  someone who has none. There is no separate `orcid` key; it is derived from
  `rid`.
- `provenance`: says who asserted this profile and on what basis, and it has
  no default. `third_party` is the honest label here: you are writing this
  about someone else, and nobody has verified anything.

## Step 3: Write the personality documents

Create `personality/expertise.md`. Sections describe what the researcher works
on and cite papers by their `paper_id` in square brackets:

```markdown
# Expertise

## Region set analysis
Jane developed methods for enrichment analysis of genomic region sets [doe2016example].
```

Create `personality/SOUL.md`, a short narrative of how the researcher thinks:

```markdown
# How Jane thinks
Jane sees analysis problems as metadata problems.
```

## Step 4: Add papers and a summary

Create `sources/papers.jsonld`. It is a [`Collection`](reference/schemas.md#papers_jsonld-sourcespapersjsonld)
whose `hasPart` holds the works; a bare top-level list is rejected by the loader:

```json
{
  "@context": "https://profiles.databio.org/context/v1.jsonld",
  "@type": "Collection",
  "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
  "about": { "@id": "https://orcid.org/0000-0002-1825-0097" },
  "hasPart": [
    {
      "@id": "#paper/doe2016example",
      "@type": "ScholarlyArticle",
      "name": "An example method for region set analysis",
      "paper_id": "doe2016example",
      "datePublished": "2016",
      "isPartOf": { "@type": "Periodical", "name": "Bioinformatics" },
      "first_author": "Doe"
    }
  ]
}
```

There is no `status` field here. Download status, contamination flags and
the rest are build bookkeeping, and they live in the build sidecar at
`.build/<slug>/meta/build_state.json`, a sibling tree outside the profile
directory. Publishing a profile publishes the bibliographic record, not the
build.

Create one summary file at `sources/summaries/doe2016example.summary.md`. The
filename stem must match the paper's `paper_id`:

```markdown
Jane developed an example method for enrichment analysis of genomic region sets.
```

Your directory now looks like this:

```
jane-doe/
├── profile.jsonld
├── personality/
│   ├── expertise.md
│   └── SOUL.md
└── sources/
    ├── papers.jsonld
    └── summaries/
        └── doe2016example.summary.md
```

### Write the manifest

`profile.jsonld` must list every attached file in its `hasPart` and
`subjectOf` arrays. Generate those entries from the directory, then check the
result (run both from the parent directory):

```bash
rp manifest jane-doe --write
rp validate jane-doe
```

`rp manifest --write` adds four entries (`sources/papers.jsonld`, the summary,
and the two personality documents) and `rp validate` reports `PASS`. Without
the manifest, validation fails with `manifest_drift`.

## Step 5: Load the profile

From the parent directory, load the profile in Python:

```python
from researcher_profiles import ResearcherProfile

p = ResearcherProfile.from_files("jane-doe")
print(p)
```

Expected output (the `key=` value is `file:` plus the absolute path you
loaded, so yours will differ):

```
ResearcherProfile(key='file:/home/you/jane-doe', slug='jane-doe', papers=?)
```

The `papers=?` marker means papers have not been read yet: the profile loads
files lazily, only when you access them. The repr grows `rid=` and `name=`
fields once metadata has been read, which happens by the time you reach
[Step 6](#step-6-explore-the-profile) below.

## Step 6: Explore the profile

Read the metadata and corpus. Each access reads and caches the underlying file:

```python
print(p.name)  # Jane Doe
print(p.orcid)  # 0000-0002-1825-0097
print(p.affiliation)  # Example University
print(p.field)  # Computational Biology
print(p.metadata.subfields)  # ['epigenomics', 'chromatin']
print(len(p.papers))  # 1
print(p.papers[0].title)  # An example method for region set analysis
print(list(p.summaries.keys()))  # ['doe2016example']
print(p.provenance)  # third_party
print(p.license)  # https://creativecommons.org/licenses/by/4.0/
```

`p.papers[0].year` is an `int` and `p.papers[0].journal` is a `str`,
even though on disk they are an `xsd:gYear` string and a `Periodical` node. The
JSON-LD shape lives in the serializer, not in the attribute types; reading a
profile in Python never means unwrapping node objects.

Serialize the whole profile to a JSON-ready dict:

```python
d = p.to_dict()
print(sorted(d.keys()))
```

Expected output:

```
['expertise', 'license', 'metadata', 'papers', 'path', 'provenance', 'rid', 'slug', 'soul', 'summary_ids']
```

Every profile also has a level (`lite`, `full`, or `deep`) that records how
deeply it was built; when unset it defaults to `full`, so this profile is `full`.
Because Jane Doe has `expertise.md` and `SOUL.md`, she is persona-ready
(`p.has_persona` is `True`). Only `full` and `deep` profiles can be persona-ready. See
[profile depth levels](profile-format.md#profile-depth-levels).

## Step 7: Build a semantic index

The embedding index lives inside the profile at `.cache/embeddings.sqlite`, so the
profile stays self-contained. `p.index` is the profile's index manager:

```python
report = p.index.build()
print(report.added, report.backend_name)
```

Expected output (the first run downloads the sentence-transformer model):

```
4 st:all-MiniLM-L6-v2
```

The count is the number of chunks indexed: the expertise sections, the SOUL
document, and each paper summary are chunked and embedded separately.

`build()` returns a fresh `IndexReport` each call; it does not store the report
anywhere. What is cached is the index connection. `p.index` is created on first
access; its first `build()` or `search()` opens `.cache/embeddings.sqlite` and
keeps that handle, so later calls reuse it instead of reopening the file. This
is the same lazy-cached pattern as `p.metadata` or `p.papers`.

## Step 8: Search the corpus

Query the index. Hits come back ranked by cosine similarity:

```python
hits = p.index.search("region set enrichment", k=3)
for h in hits:
    print(round(h.score, 3), h.source_type, h.source_id)
```

Expected output:

```
0.595 expertise expertise
0.568 paper_summary doe2016example
0.344 soul soul
```

The top hit is the expertise section that mentions region-set enrichment, then
the paper summary, then the SOUL document.

## Challenge

Add a second paper to `sources/papers.jsonld` (give it a new `paper_id`, title, and
year) and a matching `sources/summaries/<paper_id>.summary.md`. Rebuild the index
and search again. How does the new summary rank for a query about your new
paper's topic?

!!! tip
    After editing files on disk, call `p.index.build(force=True)` to drop and
    rebuild the index, or load a fresh `ResearcherProfile`; the in-memory
    profile caches file contents from the first access.

## Summary

!!! success ""
    - A profile is a directory keyed by a slug, holding `profile.jsonld`,
      `personality/` Markdown, and `sources/` papers and summaries
    - `ResearcherProfile.from_files(path)` loads a profile and reads its files
      lazily on first access
    - `profile.jsonld` carries `conformsTo` (a resolvable IRI) as the format gate
    - The `[vectors,st]` extras give `p.index.build()` and `p.index.search()`;
      the index is stored inside the profile at `.cache/embeddings.sqlite`

## Next steps

- [The profile format](profile-format.md): the full on-disk specification
- [How to create a profile](how-to/create-a-profile.md): other ways to
  produce a profile directory, by script or bootstrapped with an LLM
- [Build and search the embeddings index](how-to/embeddings-index.md): indexing
  in more depth
- [Python API reference](reference/python-api.md): every class and method
