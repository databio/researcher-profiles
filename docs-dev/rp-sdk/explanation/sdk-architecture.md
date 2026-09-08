# SDK architecture

The narrative behind `researcher_profiles`'s module layout, storage layer,
capability managers, and export idempotency contract. For mechanical
signatures and parameter tables, see the
[Python API reference](../../../docs/rp-sdk/reference/python-api.md).

## SDK module surface

`researcher_profiles` is a read, validate, serve and publish package. These
modules are intended SDK surface, not a pending carve:

| Module | Role | Surface |
|---|---|---|
| `build_state.py` | The committed format of the `build_state.json` sidecar (`BuildState`, `PaperBuildState`); read by `payloads.py`, `export.py`, `publish`, `embeddings`, and `store/sql/` | SDK-public (format) |
| `manifest.py` | Build / check the `hasPart`+`subjectOf` manifest (`rp manifest`) | SDK-public |
| `citations.py` | Citation formatting (`Citation.to_bibtex()` / `.to_csl()`) | SDK-internal |
| `db.py` | The SQL profile store's tables: a peer backing store (`[sql]` extra) | SDK-public |
| `store/` | The `ProfileStore` protocol, `FilesystemProfileStore` (core), and `SqlProfileStore` + `SqlArtifactStorage` (`[sql]`) | SDK-public |
| `config.py` | Profiles-root and store-URL resolution used by every `rp` verb | SDK-internal |
| `resolve.py` | Authoritative name/rid resolution with deterministic mint-on-miss; the policy half of `schema.mint_local_rid`, served by `POST /identity/resolve` and called by `client.resolve_rid` | SDK-public |

`build_state.py` is the on-disk format of a sidecar file, not build
machinery: it stays even though the process that writes the sidecar lives
outside this package.

Beyond the extra, the `[llm]` persona verbs (`prof.persona.ask` / `.review` /
`.innovate` / `.riff`) need a persona-ready profile at call time: a `full` or
`deep` profile with non-empty `expertise` and `soul` (`has_persona` is
`True`). Called on any other profile they raise
`PersonaUnavailableError`.

## The storage layer: composed, not inherited

Persistence is a composed backend, not inheritance. `ResearcherProfile`
holds one `researcher_profiles.storage.ArtifactStorage`, and a backend
implements that class. It never subclasses `ResearcherProfile`. Four ship
in the SDK: `DirectoryArtifactStorage`, `SqlArtifactStorage`, `ApiArtifactStorage`, `StaticArtifactStorage`. Reach
it with `prof.storage`.

`.cache/` is out of scope: the serve-time derived caches (embeddings, topics,
calibration, the store-wide `.cache` blobs) are regenerable binary artifacts with
different semantics and are not covered. They reach a directory
through `prof.require_directory(...)`, which raises
`CapabilityUnavailableError` on a backend that has none.

The full contract, and how to write a hook that reacts to a write, is in
[the storage seam reference](../developer/storage-seam.md).

## Capability managers

Six sub-objects, each built lazily on first access, each with `get()` (or its
verb) as its primary read:

| Accessor | Manager | Methods |
|---|---|---|
| `prof.persona` | `profile.persona.PersonaManager` | `ask`, `review`, `innovate`, `riff`, `chat` |
| `prof.index` | `profile.index.IndexManager` | `build`, `search`, `search_similar`, `embedding` |
| `prof.cite` | `profile.cite.CitationManager` | `get`, `many`, `verify`, `export` |
| `prof.coverage` | `profile.coverage.CoverageManager` | `get`, `staleness`, `recent_work`, `last_updated` |
| `prof.topics` | `profile.topics.TopicManager` | `get`, `relevance` |
| `prof.edit` | `profile.edit.EditManager` | `patch_metadata`, `set_soul`, `set_visibility` |

`ResearcherProfile` lives in `researcher_profiles/profile/__init__.py` as a
package, mirroring `analytics/`: one file per capability manager sits beside
it (`profile/persona.py`, `profile/index.py`, `profile/cite.py`,
`profile/coverage.py`, `profile/topics.py`, `profile/edit.py`), each holding only
the thin manager class.

A backend may supply its own `persona` / `index`. `ApiArtifactStorage` does, because
the server owns the model and the index. The other four are always the local
classes: `cite`, `coverage`, and `topics` are pure functions of artifacts the
profile already has, or of the caches under `.cache/`, and `edit` writes
through the storage's `save_*` methods.

```python
prof.persona.ask("What is your work on chromatin accessibility?")
prof.index.search("region universes", k=5)
prof.cite.get("doe2023framework")
prof.coverage.get().topics
prof.topics.get(n=10)
```

A capability that needs a local directory and does not have one raises
`CapabilityUnavailableError`, both a `ProfileError` and a
`NotImplementedError`, naming the way out (export the directory, or install a
local copy).

## The export idempotency contract

`content_hash` covers everything except itself and `built_at`. That includes
`summary`, because an edited summary is a real content change. So two builds
seconds apart hash identically. Upsert when `content_hash` changes; do
nothing when it does not. That is what makes a first load and a later
refresh the same operation, and it is why re-running a backfill is a no-op.
When `EXPORT_VERSION` changes, every hash changes, so a plain re-run migrates
the KB. No migration script is needed.

```python
from researcher_profiles import ExportOptions, ResearcherProfile, build_export_bundle

profile = ResearcherProfile.from_files("~/researcher-profiles/jane-doe")
bundle = build_export_bundle(profile, ExportOptions(max_papers=25))

if kb.get(bundle.rid, {}).get("content_hash") != bundle.content_hash:
    kb.upsert(
        key=bundle.rid,
        text=bundle.text,  # chunk and embed however you like
        dois=bundle.dois,  # link to works the KB already holds
        source_url=bundle.explore_url,
        content_hash=bundle.content_hash,
    )
```

## The one filesystem admission

`store.root` is the single place the SDK admits a backend might be a
directory. It exists because the serve-time derived caches
(`.cache/embeddings.sqlite`, `<root>/.cache/centroids.npz`, `graph.sqlite`) are
outside the storage layer, so the features built on them
(`/api/v1/match`, the graph endpoints) still need somewhere to look. A store
that is not a directory returns `None` and those features degrade with an
actionable 503. Nothing else may branch on it.
