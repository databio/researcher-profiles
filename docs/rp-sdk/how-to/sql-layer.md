# How to store profiles in a database

A profile does not have to be a directory. With the `[sql]` extra it can live in
SQL (Postgres in production, SQLite in tests) and behave the same: same
`ResearcherProfile` API, same properties, same persona methods, same exports.

The SQL store is a peer backend, not a lossy mirror. Profiles in `rp_*`
tables round-trip byte-identically to directories. Pick one backend per
deployment.

!!! info "Prerequisites"
    - `researcher-profiles[sql]` (add `[postgres]` for the driver)
    - A SQLAlchemy connection URL

## The tables

| Table | Class | One row per |
|---|---|---|
| `rp_profiles` | `ProfileRow` | profile (primary key: `rid`) |
| `rp_papers` | `PaperRow` | `sources/papers.jsonld` work |
| `rp_grants` | `GrantRow` | `sources/grants.jsonld` award |
| `rp_expertise_topics` | `ExpertiseTopicRow` | `profile.jsonld` `expertise` label |
| `rp_artifacts` | `ArtifactRow` | file the profile contains (the manifest) |
| `rp_chunk_vectors` | `ChunkVectorRow` | embedded chunk (the searchable index) |
| `rp_profile_vectors` | `ProfileVectorRow` | profile-level vector (the centroid) |
| `rp_build_state` | `BuildStateRow` | profile's build sidecar (not published) |

Every child table references its owner by `profile_rid`, a foreign key onto
`rp_profiles.rid`.

The schema has three properties worth knowing:

- `rid` is the primary key, and `slug` is a unique handle. `slug` is the display
  name and may be renamed; that rename is one `UPDATE` and touches zero child
  rows. Its uniqueness is a *store* constraint (one store cannot hold two
  profiles under one handle), not an identity claim, so join on `rid`.
- The JSON-LD document is stored whole and is the source of truth.
  `rp_profiles.document` holds the entire `profile.jsonld` payload; every scalar
  column beside it is a *derived projection*, rebuilt on every write and never
  read back. The document is not shredded into columns because the format
  promises unknown terms round-trip untouched, and shredding drops them. The
  projections exist so you can ask SQL questions:

    ```sql
    SELECT slug, name FROM rp_profiles
     WHERE level = 'deep' AND first_r01_equivalent_year IS NULL;
    ```

- `rp_artifacts` is the manifest: one row per file, keyed by
  `(profile_rid, content_url)` (the manifest's own address space), with the
  body in `text` or `data`. That makes privacy a `WHERE` clause:

    ```sql
    SELECT content_url FROM rp_artifacts
     WHERE profile_rid = :rid AND visibility = 'public';
    ```

- Vectors are rows, not a file. `rp_chunk_vectors` holds one embedded chunk each
  and `rp_profile_vectors` holds the profile centroid, both as little-endian
  float32 in a portable `LargeBinary` column. Plain columns, not `sqlite-vec`
  and not `pgvector`: the same table definitions have to load on SQLite in tests
  and on Postgres in production, and both of those extensions are one or the
  other. Cosine runs in numpy over a few hundred rows per profile, which is what
  the directory and published-site backends already do.

    Only chunks that are safe to publish are stored, the same subset the
    published `.bin` carries: embeddings are partially invertible, so a chunk
    from a restricted source stays on the build machine. The centroid is
    computed over the whole index, because a single averaged vector is not.

Build state is a separate *table* so that publishing can skip it. "Publish this store" means copy
everything except `rp_build_state`, and `DROP TABLE rp_build_state` must stay as
free as `rm -rf .build/`: every profile still loads after it.

## Create the tables

```python
from researcher_profiles import SqlProfileStore

store = SqlProfileStore("postgresql://user@host/db")
store.create_all()
```

`create_all` is a fresh-instance convenience. On a deployed Postgres, column
evolution is the deployer's responsibility; this package ships no migrations.

## Push profiles in

```python
from researcher_profiles import ResearcherProfile, SqlProfileStore

store = SqlProfileStore("postgresql://user@host/db")
rid = store.put(ResearcherProfile.from_files("path/to/jane-doe"))
```

`put` takes any `ResearcherProfile` (a directory, a published static or
remote one, or one from another store), writes every table in one transaction,
upserts on `rid`, and returns the rid. Binary artifact bodies are opt-in with
`include_binary=True`; without it the artifact keeps its manifest row and loses
only its bytes. Vectors are not gated by that flag: `put` shreds the profile's
built index into `rp_chunk_vectors` / `rp_profile_vectors` every time, because a
queryable vector is not a file body.

`store.import_directory(path)` is sugar for the same thing from a directory, and
`store.commit_directory(slug, staging)` is the ingest primitive an upload path
uses.

## Load a profile back

```python
prof = store.get("jane-doe")  # a rid or a slug; rid wins
prof = ResearcherProfile.from_db("postgresql://user@host/db", "jane-doe")

prof.name, prof.level, prof.papers, prof.soul, prof.summaries["doe2019methods"]
```

`store.get(...)` returns an ordinary `ResearcherProfile` over a `SqlArtifactStorage`. It is also
writable, and its writes are a real transaction:

```python
prof.edit.patch_metadata({"field": "Systems Biology"})
prof.edit.set_soul("# How I think\n...")
```

Everything inside one `write_unit` (the rows, the refreshed `content_hash`, and
every registered pre-commit hook) commits or rolls back together.

A DB-backed profile cannot run `validate()`, `status()`, or anything on
`prof.index`; those *build* an index or walk a directory. Each raises
`CapabilityUnavailableError` (a subclass of both `ProfileError` and
`NotImplementedError`, so either catch works) telling you to pull a directory
first. *Reading* vectors is different and needs no directory: ask the store.

```python
store.centroid("jane-doe")  # one row from rp_profile_vectors
store.centroids_matrix()  # the whole roster, one SELECT
store.vector_index("jane-doe").search("chromatin accessibility", k=5)
```

## Pull a directory back out

```python
store.export_directory("jane-doe", "./pulled/jane-doe")
```

This writes `profile.jsonld` from the stored document, every artifact body to
its own `contentUrl`, and re-renders `sources/papers.jsonld` /
`sources/grants.jsonld` from `rp_papers` / `rp_grants` inside their stored
collection envelope. The result is byte-identical to what went in, which is
what makes the store a peer backend rather than a lossy copy.

Build state is written only with `with_build=True`, and it goes into the build
root beside the directory, never inside it.

## Rename, delete, list

```python
store.rename(rid, "doe-jane")  # one UPDATE; no child row moves
store.list_slugs()  # just the handles
store.list_profiles()  # ProfileRow objects, ordered by slug
store.delete("doe-jane")  # cascades to every child table; returns the rid
store.manifest_from_rows(rid)  # (hasPart, subjectOf); the DB twin of build_manifest
```

## Serve it over HTTP

`create_app` takes a store, so the same server code serves either backend:

```python
from researcher_profiles.api.app import create_app
from researcher_profiles.store.sql import SqlProfileStore

app = create_app(SqlProfileStore("postgresql://user@host/db"), token="...")
```

or from the shell, `python -m researcher_profiles.api --database-url ...` (also
`$RESEARCHER_PROFILES_DATABASE_URL`, which wins over `$RESEARCHER_PROFILES_ROOT`).

Pre-commit hooks register on the store, so a management host's dependent state
stays consistent with every write. On this backend they also run inside the
transaction (`WriteContext.atomic` is `True`):

```python
store.add_pre_commit_hook(my_hook)
```

The graph routes (`POST /api/v1/coi/check`, `POST /api/v1/match/reviewers`,
`GET /api/v1/graph/neighbors/{ref}`) work on a SQL store: `get_graph`
materializes it to a temp directory and builds the graph from that. The graph
is the last feature that still needs a directory. The temp directory is dropped
on the next write, so a graph query after a write re-materializes it. 503 is
reached only if the graph subpackage fails to import or the materialized store
cannot be read, not merely because the backend is SQL.

`/api/v1/match` works on a SQL store directly, with no temp directory: the
match manager ranks over `rp_chunk_vectors` and `rp_profile_vectors`.

Ingest is where vectors enter. `commit_directory` shreds a staged
`.cache/embeddings.sqlite` into those tables and reports `indexed=True`; pass
`build_missing_index=True` to build one first when the staged profile has none.
A profile ingested without an index is hosted but unrankable, and `/match`
answers 503 saying exactly that rather than returning an empty list. The server
still has to embed the *query*, so a deployment with no encoder fails loudly
there.

## From the command line

```bash
export RESEARCHER_PROFILES_DATABASE_URL="postgresql://user@host/db"

rp db init                                # create the rp_* tables
rp db push --all                          # load every profile in the cache
rp db push jane-doe --include-binary
rp db list --json
rp db pull jane-doe --to ./pulled
rp db rm jane-doe
```

The URL resolves, highest precedence first: `--database-url`, then
`$RESEARCHER_PROFILES_DATABASE_URL`. There is no built-in default, because a guessed database
means a whole profile store written somewhere nobody meant. A command with no URL
exits `2` (invalid usage). Every command prints which database it acted on.

## Query the rows directly

```python
from sqlmodel import Session, select
from researcher_profiles.db import ProfileRow, PaperRow

with store.session() as s:
    for row in s.exec(select(ProfileRow).where(ProfileRow.level == "deep")).all():
        print(row.slug, row.name, row.orcid, row.content_hash)

    papers = s.exec(select(PaperRow).where(PaperRow.profile_rid == rid)).all()
```

Do not assume either of these:

- `paper_id` is not unique within a profile. It is a generated citekey, and
  real corpora produce collisions, so papers are keyed by `(profile_rid, ordinal)`.
- `date_modified` is often NULL, and is never defaulted. A profile whose
  content never changed legitimately has no vintage; see
  [`date_modified`](https://github.com/databio/researcher-profiles/blob/master/rp-sdk/src/researcher_profiles/date_modified.py).
