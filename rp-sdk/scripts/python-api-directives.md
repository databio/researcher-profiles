# Python API reference

<!-- SOURCE TEMPLATE, not published output. scripts/render_python_api.py reads
     this file, resolves every `::: module.Class` directive with griffe, and
     writes the rendered result to docs/rp-sdk/reference/python-api.md. Only
     the hand-written framing (this note excluded) and the directive list
     below are edited here; the mechanical content comes from docstrings in
     src/researcher_profiles/. Add a directive when a new public class or
     function should appear in the reference; remove one when it stops being
     public API. Directive order is the page's heading order: keep it in
     the mirrored-codebase order below. See AGENTS.md. -->

Key classes and functions in `researcher_profiles`. The core install
(`pydantic` + `pyyaml`) provides the schema models, `ResearcherProfile`, and the
OpenAlex parser. Other surfaces attach or import only when their extra is present.

For the narrative behind the module layout, the storage interface, the capability
managers, and the export idempotency contract, see the developer notes in
`docs-dev/rp-sdk/explanation/sdk-architecture.md`.

## Capability by extra

| Surface | Import | Extra |
|---|---|---|
| Schema models, `ResearcherProfile`, OpenAlex parser | `researcher_profiles` | core |
| `resolve_person` | `researcher_profiles.resolve` | core |
| `prof.index` (`SqliteEmbeddingIndex`), `store.match` / `.centroids` / `.indexes` | `researcher_profiles.embeddings`, `researcher_profiles.analytics` | `[vectors,st]` |
| `prof.persona` (`ask` / `review` / `innovate` / `riff` / `chat`) | `researcher_profiles.generative` | `[llm]` |
| `ApiArtifactStorage`, `StaticArtifactStorage`, `.from_api()`, `.from_url()`, `rank_against()`, `resolve_rid()` | `researcher_profiles.client` | `[client]` |
| `ProfileStore` (protocol), `FilesystemProfileStore` | `researcher_profiles.store` | core |
| `create_app` | `researcher_profiles.api.app` | `[api]` |
| `ArtifactStorage` (ABC), `DirectoryArtifactStorage` | `researcher_profiles.profile.storage` | core |
| `SqlProfileStore`, `SqlArtifactStorage`, `.from_db()`, the `rp_*` tables | `researcher_profiles.store.db`, `researcher_profiles.store.sql` | `[sql]` |

Each capability manager is built lazily on first access, so a core-only install
still imports the package and a missing extra raises an `ImportError` naming it
at first use. The optional public names (`LLMClient`, `ApiArtifactStorage`,
`SqlProfileStore`, …) do the same when accessed.

---

## `ResearcherProfile`

`researcher_profiles.ResearcherProfile` is a profile loaded from a directory.
Persistence goes through a composed `ArtifactStorage`, and capability managers
(`prof.persona`, `prof.index`, `prof.cite`, `prof.coverage`, `prof.topics`,
`prof.edit`) hang off the aggregate. Both are described in
`docs-dev/rp-sdk/explanation/sdk-architecture.md`.

::: researcher_profiles.profile.ResearcherProfile
    options:
      heading_level: 3

### Owner edits

::: researcher_profiles.profile.edit.EditManager
    options:
      heading_level: 4

### Exceptions

::: researcher_profiles.errors.ProfileError
    options:
      heading_level: 4

::: researcher_profiles.errors.ProfileLoadError
    options:
      heading_level: 4

::: researcher_profiles.errors.ProfileWriteError
    options:
      heading_level: 4

::: researcher_profiles.errors.CapabilityUnavailableError
    options:
      heading_level: 4

::: researcher_profiles.errors.ProfileValidationError
    options:
      heading_level: 4

::: researcher_profiles.models.results.PersonaUnavailableError
    options:
      heading_level: 4

---

## Export for knowledge bases

`researcher_profiles.profile.export` turns one profile into a single block of prose plus
the metadata that travels with it, so a connector pushing profiles into a
knowledge base (KB) never has to open `profile.jsonld`, `expertise.md`, `SOUL.md`
and `papers.jsonld` itself, re-implement the privacy rule, or invent a change
detector. The idempotency contract behind `content_hash` is explained in
`docs-dev/rp-sdk/explanation/sdk-architecture.md`.

### Entry points

::: researcher_profiles.profile.export.render_export_text
    options:
      heading_level: 4

::: researcher_profiles.profile.export.select_export_papers
    options:
      heading_level: 4

::: researcher_profiles.profile.export.build_export_bundle
    options:
      heading_level: 4

::: researcher_profiles.profile.export.explore_url
    options:
      heading_level: 4

::: researcher_profiles.profile.export.export_content_hash
    options:
      heading_level: 4

::: researcher_profiles.profile.export.export_paper_body
    options:
      heading_level: 4

### `ExportOptions`

::: researcher_profiles.profile.export.ExportOptions
    options:
      heading_level: 4

### `ProfileExportBundle`

::: researcher_profiles.profile.export.ExportPaperRef
    options:
      heading_level: 4

::: researcher_profiles.profile.export.ProfileExportBundle
    options:
      heading_level: 4

### Exceptions

::: researcher_profiles.profile.export.ExportError
    options:
      heading_level: 4

::: researcher_profiles.profile.export.ExportVisibilityError
    options:
      heading_level: 4

---

## Cross-profile analytics (`store.match`, `store.centroids`, `store.indexes`)

There is no aggregate object over a store: a collection of profiles *is* the
store. The three cross-profile concerns hang off it as accessors, the same way
`prof.cite` hangs off a profile. They require the `[vectors,st]` extras (they
need `numpy`) and are imported on first access, so a core-only install still
uses the store for everything else.

Ranking and centroids also need the `VectorStore` capability; a store without
it raises `CapabilityUnavailableError` naming what to do instead.

::: researcher_profiles.store.DuplicateIdentityError
    options:
      heading_level: 3

### `store.centroids` (centroids and the query backend)

::: researcher_profiles.analytics.centroids.CentroidManager
    options:
      heading_level: 4

### `store.match` (ranking and analysis)

::: researcher_profiles.analytics.match.MatchManager
    options:
      heading_level: 4

### `store.indexes` (embedding-index maintenance)

The object is `analytics.IndexFleetManager`, distinct from the per-profile
`profile.index.IndexManager` that `prof.index` hands back.

::: researcher_profiles.analytics.indexes.IndexFleetManager
    options:
      heading_level: 4

---

## `ApiArtifactStorage` and `StaticArtifactStorage`

`researcher_profiles.client` provides the two HTTP backends. It requires the
`[client]` extra. Neither is a profile: build an ordinary `ResearcherProfile`
over one with `ResearcherProfile.from_api(url)` or `.from_url(url)`, and it
behaves like a local profile, with the same properties and method signatures.

::: researcher_profiles.client.ApiArtifactStorage
    options:
      heading_level: 3

::: researcher_profiles.client.StaticArtifactStorage
    options:
      heading_level: 3

### `rank_against` (module-level)

::: researcher_profiles.client.rank_against
    options:
      heading_level: 4

### `resolve_rid` (module-level)

::: researcher_profiles.client.resolve_rid
    options:
      heading_level: 4

---

## OpenAlex parser

`researcher_profiles.openalex` does pure record parsing, with no HTTP. It is
available in core.

::: researcher_profiles.openalex.parse_work
    options:
      heading_level: 3

::: researcher_profiles.openalex.decode_abstract_inverted_index
    options:
      heading_level: 3

::: researcher_profiles.openalex.to_work_dict
    options:
      heading_level: 3

::: researcher_profiles.openalex.to_normalized_dict
    options:
      heading_level: 3

---

## Embeddings store

`researcher_profiles.embeddings` is a per-profile vector store. It requires
the `[vectors,st]` extras.

::: researcher_profiles.embeddings.cache.SqliteEmbeddingIndex
    options:
      heading_level: 3

::: researcher_profiles.embeddings.build_index
    options:
      heading_level: 3

`researcher_profiles.profile.index.IndexManager` is what `prof.index` hands
back: `build`, `search`, `search_similar`, `embedding`. Distinct from
`analytics.IndexFleetManager` (above), which operates over every profile in a
root rather than one profile's own index.

::: researcher_profiles.profile.index.IndexManager
    options:
      heading_level: 3

::: researcher_profiles.embeddings.cache.SearchHit
    options:
      heading_level: 3

::: researcher_profiles.embeddings.cache.IndexReport
    options:
      heading_level: 3

### Exceptions

::: researcher_profiles.embeddings.backends.MissingEmbeddingBackendError
    options:
      heading_level: 4

::: researcher_profiles.embeddings.cache.IndexBackendMismatchError
    options:
      heading_level: 4

::: researcher_profiles.embeddings._sqlite.IndexNotBuiltError
    options:
      heading_level: 4

---

## Result dataclasses

`researcher_profiles.models.results` holds the return types for capability methods.

::: researcher_profiles.models.results.PersonaResponse
    options:
      heading_level: 3

::: researcher_profiles.models.results.Idea
    options:
      heading_level: 3

::: researcher_profiles.models.results.Riff
    options:
      heading_level: 3

::: researcher_profiles.models.results.CitationRef
    options:
      heading_level: 3

::: researcher_profiles.models.results.Citation
    options:
      heading_level: 3

::: researcher_profiles.models.results.Match
    options:
      heading_level: 3

::: researcher_profiles.models.results.MatchEvidence
    options:
      heading_level: 3

::: researcher_profiles.models.results.Topic
    options:
      heading_level: 3

::: researcher_profiles.models.results.Coverage
    options:
      heading_level: 3

::: researcher_profiles.models.results.GenerativeParseError
    options:
      heading_level: 3

`PersonaUnavailableError` is documented under
[`ResearcherProfile` exceptions](#exceptions) above.

---

## `ProfileStore` (`researcher_profiles.store`)

The collection-level interface: a *set* of profiles. Each profile's artifacts
live one level down in `ArtifactStorage`. It is what an HTTP app, an
ingest path, or a management host is handed, and it is what
[`create_app`](api.md#running-the-server) takes. The "one filesystem
admission" (`store.root`) is explained in
`docs-dev/rp-sdk/explanation/sdk-architecture.md`.

::: researcher_profiles.store.ProfileStore
    options:
      heading_level: 3

::: researcher_profiles.store.files.FilesystemProfileStore
    options:
      heading_level: 3

::: researcher_profiles.store.sql.SqlProfileStore
    options:
      heading_level: 3

Also exported from `researcher_profiles.store`:

::: researcher_profiles.store.IngestResult
    options:
      heading_level: 3

::: researcher_profiles.store.ProfileNotFoundError
    options:
      heading_level: 3

::: researcher_profiles.store.UploadError
    options:
      heading_level: 3

::: researcher_profiles.store.build_store
    options:
      heading_level: 3

---

## Identity resolution (`researcher_profiles.resolve`)

The mint-vs-bind policy behind `schema.mint_local_rid`: given a rid or a
free-text name, decide whether it names an existing profile, an undecided
one (deferral), or nobody yet (mint). It runs against any `ProfileStore`, so
it works the same whether the caller is the HTTP route
(`POST /identity/resolve`) or a script holding a store directly.

::: researcher_profiles.resolve.resolve_person
    options:
      heading_level: 3

::: researcher_profiles.resolve.ResolveResult
    options:
      heading_level: 3

::: researcher_profiles.resolve.Candidate
    options:
      heading_level: 3

::: researcher_profiles.resolve.ResolveError
    options:
      heading_level: 3

---

## SQL profile store (`researcher_profiles.store.db`, `researcher_profiles.store.sql`)

Requires the `[sql]` extra. See
[How to store profiles in a database](../how-to/sql-layer.md).

This is a peer backing store. A profile in the `rp_*`
tables is a profile: it round-trips back to a byte-identical directory, and
`SqlArtifactStorage` reads and writes it through the same `ArtifactStorage` contract the
filesystem backend implements.

### Tables (`researcher_profiles.store.db`)

::: researcher_profiles.store.db.ProfileRow
    options:
      heading_level: 4

::: researcher_profiles.store.db.PaperRow
    options:
      heading_level: 4

::: researcher_profiles.store.db.GrantRow
    options:
      heading_level: 4

::: researcher_profiles.store.db.ExpertiseTopicRow
    options:
      heading_level: 4

::: researcher_profiles.store.db.ArtifactRow
    options:
      heading_level: 4

::: researcher_profiles.store.db.ChunkVectorRow
    options:
      heading_level: 4

::: researcher_profiles.store.db.ProfileVectorRow
    options:
      heading_level: 4

::: researcher_profiles.store.db.BuildStateRow
    options:
      heading_level: 4

::: researcher_profiles.store.db.create_all
    options:
      heading_level: 4

::: researcher_profiles.store.db.get_engine
    options:
      heading_level: 4

::: researcher_profiles.store.db.reset_engine
    options:
      heading_level: 4

::: researcher_profiles.store.db.get_session
    options:
      heading_level: 4

::: researcher_profiles.store.db.content_hash_for
    options:
      heading_level: 4

### `SqlArtifactStorage` (`researcher_profiles.store.sql`)

`SqlArtifactStorage(engine_or_url)` accepts a SQLAlchemy `Engine` or a URL. It
implements the whole `ArtifactStorage` contract
for one profile's rows, and `SqlProfileStore` (above) implements the whole
[`ProfileStore`](#profilestore-researcher_profilesstore) protocol.

::: researcher_profiles.store.sql.SqlArtifactStorage
    options:
      heading_level: 4

### CLI

`rp db init | push | pull | list | rm`. The database URL resolves
`--database-url` → `$RP_DATABASE_URL`, with no built-in default; a missing URL
exits `2`.
