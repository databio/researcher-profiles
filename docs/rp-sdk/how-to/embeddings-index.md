# How to build and search the embeddings index

The `[vectors,st]` extras add a per-profile vector store and semantic search over
a researcher's corpus, plus cross-profile ranking via `store.match`.

!!! info "Prerequisites"
    - `researcher-profiles[vectors,st]`
    - A profile directory with `personality/` and `sources/summaries/` content

The index lives inside the profile at `.cache/embeddings.sqlite`, so it travels
with the profile directory.

## Build the index for one profile

`prof.index` is the per-profile embedding index
(`profile.index.IndexManager`), built lazily on first access:

```python
from researcher_profiles import ResearcherProfile

p = ResearcherProfile.from_files("path/to/jane-doe")
report = p.index.build()
print(report.added, report.updated, report.skipped, report.backend_name)
```

The index chunks and embeds the expertise sections, the SOUL document, and each
paper summary. `report` is an `IndexReport` with `added`, `updated`, `skipped`,
`removed`, `backend_name`, and `duration_s`.

Rebuilds are incremental: unchanged chunks are skipped by content hash. Pass
`force=True` to drop and rebuild from scratch:

```python
p.index.build(force=True)
```

### From the CLI

```bash
rp index path/to/jane-doe
rp index path/to/jane-doe --force
rp index path/to/jane-doe --backend st:all-MiniLM-L6-v2
```

Expected output:

```
backend=st:all-MiniLM-L6-v2 added=4 updated=0 skipped=0 removed=0 duration=5.17s
```

## Search one profile

`search` returns a list of `SearchHit` ranked by cosine similarity (0 to 1, higher is
better):

```python
hits = p.index.search("region set enrichment", k=3)
for h in hits:
    print(round(h.score, 3), h.source_type, h.source_id, h.section)
```

Expected output:

```
0.595 expertise expertise Region set analysis
0.568 paper_summary doe2016example None
0.344 soul soul None
```

You can filter by source type:

```python
hits = p.index.search("region set enrichment", k=5, filter={"source_type": "paper_summary"})
```

Which types exist depends on the profile's
[level](../profile-format.md#profile-depth-levels): `paper_summary`, `expertise`,
and `soul` on a `full` profile; `paper_abstract` on a `lite` one; plus `grant`,
`cv`, and `web` on a `deep` profile that carries those sources.

An empty or whitespace-only query returns an empty list rather than raising.

### From the CLI

```bash
rp search path/to/jane-doe "region set enrichment" -k 3
rp search path/to/jane-doe "chromatin" --type paper_summary
```

## Rank across many profiles

A store ranks every profile it holds against a query, using a centroid
prefilter, a chunk-level re-rank, and optional MMR diversification. Each profile
must already have a built index:

```python
from researcher_profiles.store import FilesystemProfileStore

store = FilesystemProfileStore("path/to/profiles")
matches = store.match.rank("region set enrichment analysis", k=5)
for m in matches:
    print(round(m.score, 3), m.profile.slug, m.evidence.top_papers)
```

`store.match.rank` returns `Match` objects carrying the matched `profile`, a
`score`, and a `MatchEvidence` record (top chunks, top papers, overlapping
topics, centroid score). A profile with no index is not an error: it gets a zero
centroid row, which scores 0 against every query, so it never ranks.

The same code ranks a database (`SqlProfileStore`) or a published site read over
HTTP (`HttpProfileStore`); ranking asks the store for vectors, never a
directory.

## Backends

The default backend is a sentence-transformers model (`st:all-MiniLM-L6-v2`). The
first build downloads the model. Override the backend per build with the
`backend=` argument / `--backend` flag, or globally with the
`RESEARCHER_PROFILES_EMBEDDING_BACKEND` environment variable. An index records the
backend it was built with; opening it with a different backend raises
`IndexBackendMismatchError` unless you rebuild with `force=True`.

Setting `RESEARCHER_PROFILES_DISABLE_INDEX=1` turns the index build into a no-op,
useful for search-only consumers or tests.

