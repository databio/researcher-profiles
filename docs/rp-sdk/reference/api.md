# HTTP API reference

> **Conformance requirements** for the API are defined in the
> [Dynamic API specification](../../rp-spec/dynamic-api.md). This document is the
> wire-detail reference for the SDK's implementation. The reference implementation
> mounts all endpoints under `/api/v1/` (see
> [URL prefix](../../rp-spec/dynamic-api.md#1-url-prefix)).

The `researcher_profiles.api` package (the `[api]` extra) exposes a FastAPI server
over a `ProfileStore`: a directory of on-disk profiles or a SQL store. On a
directory, each profile is a slug-named subdirectory containing a
`profile.jsonld`, paper metadata, expertise / SOUL markdown, and optionally a
`.cache/embeddings.sqlite` vector index. The API surfaces:

- Profile listing and metadata (`/profiles`, `/profiles/{slug}`)
- Paper inventories and per-paper summaries (`/profiles/{slug}/papers`, `.../summary/{paper_id}`)
- Semantic search over a researcher's corpus (`POST .../search`)
- Cross-profile ranking (`POST /api/v1/match`)
- Four persona-grounded LLM endpoints: `ask`, `review`, `innovate`, `riff`

**Base URL convention.** All resource routes are mounted under `/api/v1/`. The
`/health` route lives at the root and is unauthenticated. The default bind is
`127.0.0.1:8109`.

The `[api]` extra needs the `[vectors,st]` tier at runtime to answer `/search` and
`/api/v1/match`. Install the server with the `api,vectors,st` extras; see the
[SDK overview](../index.md#install) for the checkout install.

## Running the server

CLI entry point:

```bash
python -m researcher_profiles.api \
    --profiles-dir /path/to/profiles \
    --host 127.0.0.1 \
    --port 8109
```

| Flag | Env var | Default | Meaning |
|---|---|---|---|
| `--profiles-dir` | `RESEARCHER_PROFILES_ROOT` | (one of the two required) | Directory holding one subdirectory per profile, each with a `profile.jsonld`. Same variable as the SDK/CLI's local profiles root below: one directory, one name, for every command and every host. |
| `--database-url` | `RESEARCHER_PROFILES_DATABASE_URL` | (one of the two required) | Serve a [SQL profile store](../how-to/sql-layer.md) instead of a directory. Needs the `[sql]` extra, and **wins** over `--profiles-dir` when both are given. |
| `--host` | `RESEARCHER_PROFILES_HOST` | `127.0.0.1` | Bind address. |
| `--port` | `RESEARCHER_PROFILES_PORT` | `8109` | Bind port. |
| `--verbose` / `-v` | none | off | Sets logging to `DEBUG`. |

Alternate uvicorn entry (env-driven only):

```bash
RESEARCHER_PROFILES_ROOT=/path/to/profiles \
    uvicorn researcher_profiles.api.app:app --host 127.0.0.1 --port 8109
```

In Python, `create_app` takes a **store**, not a path:

```python
from researcher_profiles.api.app import create_app
from researcher_profiles.store import FilesystemProfileStore

app = create_app(FilesystemProfileStore("/path/to/profiles"), token="...")
```

```python
from researcher_profiles.store.sql import SqlProfileStore  # [sql]

app = create_app(SqlProfileStore("postgresql://user@host/db"))
```

### Environment variables

| Var | Required | Purpose |
|---|---|---|
| `RESEARCHER_PROFILES_ROOT` | required for the uvicorn entry (or pass `--profiles-dir`) | Profile root directory. |
| `RESEARCHER_PROFILES_DATABASE_URL` | alternative to the above | SQL profile store URL. Wins over `RESEARCHER_PROFILES_ROOT`. |
| `RESEARCHER_PROFILES_TOKEN` | optional | Bearer token. If unset/empty the server runs in **open mode** and logs a warning. |
| `RESEARCHER_PROFILES_HOST` | optional | Default bind address. |
| `RESEARCHER_PROFILES_PORT` | optional | Default port. |
| `ANTHROPIC_API_KEY` | required for `ask`/`review`/`innovate`/`riff` | Read by the Anthropic SDK at call time. Not validated at startup; a missing key surfaces as a 500 from the affected endpoint. |
| `RESEARCHER_PROFILES_REFUSAL_THRESHOLD` | optional | Float in `[0, 1]`, default `0.4`. Top-search-score below this makes `ask`/`review` refuse when `strict_corpus=true`. |
| `RESEARCHER_PROFILES_EMBEDDING_BACKEND` | optional | Embedding backend spec used when opening or rebuilding an index. |
| `RESEARCHER_PROFILES_DISABLE_INDEX` | optional | If `"1"`, disables index builds. |
| `RESEARCHER_PROFILES_MAX_UPLOAD_MB` | optional | Size cap (MB) for `PUT /api/v1/profiles/{slug}` pushes. Default 50. |
| `RESEARCHER_PROFILES_MATCH_MIN_RANKED_FRACTION` | optional | Float, default `0` (disabled). Minimum `ranked_profiles / total_profiles` `/match` must clear before it fails with a 503 instead of returning a suspiciously thin result. |
| `RESEARCHER_PROFILES_ACCEPT_FULLTEXT` | optional | If unset/false, `PUT /api/v1/profiles/{slug}` strips any `paper_fulltext`-role artifact from the uploaded archive instead of storing it. |
| `RESEARCHER_PROFILES_LLM_TIMEOUT` | optional | Seconds. Timeout for the Anthropic SDK call behind `ask`/`review`/`innovate`/`riff`. |
| `RESEARCHER_PROFILES_EMBEDDING_DEVICE` | optional | Overrides the auto-detected device (`cpu`/`cuda`/`mps`) for the `st` local encoder backend. |
| `OPENAI_API_KEY` | required for the `openai` embedding backend | Read at call time; unset raises `MissingEmbeddingBackendError`. |
| `VOYAGE_API_KEY` | required for the `voyage` embedding backend | Read at call time; unset raises `MissingEmbeddingBackendError`. |
| `SOURCE_DATE_EPOCH` | optional | Unix timestamp. When set, every generated `dateModified` and `generated_at` uses it instead of the current time, for reproducible builds. |

### SDK and CLI environment variables

These are read by the SDK and `rp` CLI rather than the HTTP server above.
`RESEARCHER_PROFILES_ROOT` and `RESEARCHER_PROFILES_DATABASE_URL` are the same
two variables the server reads above, not a second pair with a similar name:
one directory setting and one database setting, each with a single name, used
by every command and every host that means it.

| Var | Purpose |
|---|---|
| `RESEARCHER_PROFILES_ROOT` | The local profiles root (`rp install`, `rp list`, `rp seek`, `rp where`, ...), and the server's `--profiles-dir` default above. |
| `RESEARCHER_PROFILES_REGISTRY_URL` | Default registry base URL(s) for `rp install` / `rp listr`. |
| `RESEARCHER_PROFILES_DATABASE_URL` | Default SQL profile store URL for `rp db`, and the server's `--database-url` default above. |
| `RESEARCHER_PROFILES_AGENT_KEY` | An agent's `rpa_` credential, for `rp agent` / `rp profile`. |
| `RESEARCHER_PROFILES_API_URL` | The server an agent credential targets, alongside `RESEARCHER_PROFILES_AGENT_KEY`. |
| `RESEARCHER_PROFILES_AUTH_HOST` | Which `[hosts.<name>]` block in `credentials.toml` to use, for `rp agent` / `rp profile`. |
| `XDG_CONFIG_HOME` | Overrides where `credentials.json` / `credentials.toml` are read and written (default `~/.config`). |

See the [CLI reference](cli.md) for the commands that read these.

### Profile discovery and slugs

- The server is built over a [`ProfileStore`](python-api.md#profilestore-researcher_profilesstore), and
  every lookup goes through `store.resolve_slug(ref)`. Every route that
  takes a `{slug}` accepts a **rid** there too.
- `FilesystemProfileStore` lists every direct subdirectory of its root that
  contains a `profile.jsonld`; the directory name becomes the slug. It loads
  profiles lazily via `ResearcherProfile.from_files(<dir>)` and holds them in an
  LRU (capacity 32 by default: `FilesystemProfileStore(root, capacity=...)`, not
  exposed on the CLI). `SqlProfileStore` reads `rp_profiles.slug` and caches
  nothing.
- New profile directories added after startup appear in `GET /api/v1/profiles`
  (the filesystem is re-scanned each call) but each profile is constructed only on
  first request. Restart to pick up out-of-band edits to already-loaded
  profiles, or push the profile via `PUT /api/v1/profiles/{slug}`, which
  evicts the cached object and roster snapshot itself.

## Authentication

- A single static bearer token, configured via `RESEARCHER_PROFILES_TOKEN`.
- If the token is unset/empty when the app is built, the server runs in **open
  mode** and accepts any request.
- Auth gates the **write / heavy / LLM** routes and the interactive edit routes.
  `/health` is not authenticated.
- The **read** routes have no credential gate. They resolve a *viewer tier* and
  project each response through it. A caller with no credential is the viewer
  whose tier is `public`. The operator token widens that tier to `restricted`.
- Header format: `Authorization: Bearer <token>`.
- Failed auth returns `401` with body `{"detail": "invalid or missing bearer token"}`.
- A read refusal is `404`, indistinguishable from a nonexistent profile. Every
  read response carries `X-RP-Viewer-Tier`.
- Any read route accepts `?as=anonymous|lab|owner`, a preview cap that maps to
  the `public`, `internal`, and `restricted` tiers. It can only narrow the tier
  the caller already holds, never widen it, so it needs no credential of its
  own. An unknown value is a `400`
  `{"detail": "unknown viewer '<value>' (anonymous | lab | owner)"}`.

```bash
curl -H "Authorization: Bearer $RESEARCHER_PROFILES_TOKEN" \
    http://127.0.0.1:8109/api/v1/profiles
```

## Concepts

### Profile slug

A profile's display handle (for example `jane-doe`, `john-smith`): a directory
name on a filesystem store, or the `rp_profiles.slug` column on a SQL one. Slugs
are used verbatim in URL paths with no server-side normalization, and a **rid**
is accepted anywhere a slug is (`store.resolve_slug` tries both).
Path-traversal characters produce a 404 because the store's lookup finds no
matching profile.

### The four persona endpoints

All four role-play **as the researcher** by injecting `expertise.md` + `SOUL.md`
into the Anthropic system prompt. They differ in the mode instruction, how
retrieved evidence is rendered, and the response shape.

| Endpoint | Purpose | Default `k` | Returns |
|---|---|---|---|
| `POST .../ask` | Single-shot Q&A as the persona, grounded in retrieved chunks. Supports a `history` list. | 5 | `LLMTextResponse` |
| `POST .../review` | Persona reviews provided `material`. Retrieval seeded from `focus` or the first lines of `material`. | 5 | `LLMTextResponse` |
| `POST .../innovate` | Persona proposes `n` research directions on `topic`, returned as structured `Idea`s. Retries once on parse failure. | 12 | `IdeaList` |
| `POST .../riff` | Persona generates `n` divergent brainstorm fragments on `seed`. | 4 | `RiffList` |

`ask` and `review` support a `strict_corpus` refusal gate: when the top retrieval
score is below `refusal_threshold` (default from
`RESEARCHER_PROFILES_REFUSAL_THRESHOLD`, else `0.4`), the endpoint returns a
refusal with `refused=true` and makes no LLM call. `innovate` and `riff` have no
refusal gate.

`ask` and `review` also compute a `grounded` flag: any `[paper_id]` citation the
model emits that does not match a known paper id flips `grounded` to `false`.

**Persona precondition (409).** All four endpoints require a persona-ready
profile: a `full`/`deep` [profile](../profile-format.md#profile-depth-levels)
with a synthesized persona (non-empty `expertise.md` and `SOUL.md`). When the
profile is not persona-ready (for example a `lite` profile, or one missing SOUL or
expertise), the endpoint returns **409** with
`{"detail": "profile '<slug>' has no synthesized persona ..."}` and makes no LLM
call. This is distinct from the `500`/`502` failure modes, which mean an LLM or
JSON-parse failure on a persona-ready profile. Clients should branch on the
`level` returned by `GET /api/v1/profiles` to avoid calling persona endpoints on
`lite` profiles.

## Endpoints

Route authentication varies. `GET /health` and the read routes are
unauthenticated. Search, match, persona, upload, and archive require a bearer
token when one is configured. The edit routes are owner-gated. The handlers
live in `api/routes_read.py`, `routes_search.py`, `routes_generative.py`,
`routes_push.py`, `routes_identity.py`, and `routes_edit.py`; `GET /health` is
defined in `api/app.py`.

---

### GET /health

Liveness + profile-count probe. Unauthenticated.

**Response 200** (`HealthResponse`):

| Field | Type | Notes |
|---|---|---|
| `status` | string | `"ok"`, or `"degraded"` on a 503. |
| `store` | string | Display locator for the store being served: an absolute directory path, or a database URL. |
| `profile_count` | integer | Number of profiles visible (re-counted each call). |
| `detail` | string \| null | Set only when degraded, explaining why. |

```bash
curl http://127.0.0.1:8109/health
```

```json
{"status": "ok", "store": "/path/to/profiles", "profile_count": 7}
```

**Response 503**: the same body with `status: "degraded"`. Bare rp-sdk never
returns this: it is an override point for a host that runs its own startup checks and sets
`app.state.embedding_healthy = False` (with `app.state.embedding_health_detail`)
when, say, the query-embedding backend is missing. A container HEALTHCHECK then
stops routing traffic to a deployment whose `/match` would otherwise return an
empty list forever.

---

### GET /api/v1/profiles

List every profile the caller may see, in the `rp:profileList` envelope a
static site publishes as `profiles.json`.

**Response 200** (`ProfileListResponse`):

| Field | Type | Notes |
|---|---|---|
| `rp:profileList` | string | Envelope version, `"0.1"`. |
| `name` | string \| null | The store's display name, when it has one. |
| `url` | string | The request URL without its query string. |
| `updated` | string | ISO 8601, when this response was built. |
| `profiles` | list[`ProfileListEntry`] | One entry per visible profile. |

Each `ProfileListEntry`:

| Field | Type | Notes |
|---|---|---|
| `url` | string | Absolute `.../api/v1/profiles/{slug}/content/`, the base URL `profile.jsonld` and every relative `contentUrl` resolve from. |
| `slug` | string | Directory name. |
| `rid` | string \| null | The profile's identity: an ORCID or a `local:` id. |
| `name` | string | `profile.jsonld` `name`. |
| `level` | string | Depth tier: one of `lite`, `full`, `deep`. Default `"full"`. See [profile depth levels](../profile-format.md#profile-depth-levels). |
| `affiliation` | string \| null | |
| `field` | string \| null | |
| `paper_count` | integer | Number of `sources/papers.jsonld` entries. `0` when absent. |
| `summary_count` | integer | Papers that have a summary file. |
| `fulltext_pct` | float | Percentage of papers with `status == "downloaded"`. |
| `contaminated_count` | integer | Papers flagged `contaminated`. |

Profiles that fail to load are logged and skipped. The response carries
`Cache-Control: private, no-store` and `Vary: Authorization, Cookie`: who is in
the list depends on who asked.

```json
{
  "rp:profileList": "0.1",
  "name": null,
  "url": "http://127.0.0.1:8109/api/v1/profiles",
  "updated": "2026-09-06T21:10:38+00:00",
  "profiles": [
    {
      "url": "http://127.0.0.1:8109/api/v1/profiles/jane-doe/content/",
      "slug": "jane-doe",
      "rid": "0000-0002-1825-0097",
      "name": "Jane Doe",
      "level": "full",
      "affiliation": "Example University",
      "field": "Computational Biology",
      "paper_count": 8,
      "summary_count": 5,
      "fulltext_pct": 62.5,
      "contaminated_count": 0
    }
  ]
}
```

The SDK's `list_remote()` unwraps the envelope and returns the entries.

**Status codes:** `200`.

---

### GET /api/v1/profiles/{slug}

Full profile detail: metadata + raw `expertise.md` + raw `SOUL.md`.

**Response 200** (`ProfileDetail`):

| Field | Type | Notes |
|---|---|---|
| `slug` | string | |
| `rid` | string \| null | The profile's identity: an ORCID or a `local:` id. |
| `metadata` | `ProfileMetadataPayload` | See [Common response shapes](#common-response-shapes). |
| `expertise` | string \| null | Raw markdown body of `personality/expertise.md`. `null` when this viewer's tier does not reach that artifact (its `contentUrl` then appears in `withheld`). `""` means the file is empty. |
| `soul` | string \| null | Raw markdown body of `personality/SOUL.md`. `null` when withheld, as for `expertise`. |
| `manifest` | list[object] | The profile manifest (`hasPart` plus `subjectOf` entries), served whole at every tier. Each entry carries the on-disk fields plus `effective_visibility`, the tier that governs it after the derivation rule. |
| `withheld` | list[string] | The `contentUrl`s this viewer did not receive. |
| `content_hash` | string \| null | `"sha256:<hex>"` over the document and the SOUL together. Send it back as `base_hash` on the next edit to get a `409` instead of overwriting somebody else's write. |

```json
{
  "slug": "jane-doe",
  "rid": "0000-0002-1825-0097",
  "metadata": {
    "name": "Jane Doe",
    "level": "full",
    "rid": "0000-0002-1825-0097",
    "affiliation": "Example University",
    "field": "Computational Biology",
    "subfields": ["epigenomics", "chromatin"]
  },
  "expertise": "## Region set analysis\n\n...",
  "soul": "## How I think\n\n...",
  "manifest": [
    {
      "@type": "DigitalDocument",
      "name": "Expertise",
      "encodingFormat": "text/markdown",
      "contentUrl": "personality/expertise.md",
      "role": "expertise",
      "visibility": "public",
      "effective_visibility": "public"
    }
  ],
  "withheld": ["sources/papers/doe2016example.md"],
  "content_hash": "sha256:744853cb..."
}
```

**Status codes:** `200`; `404` `{"detail": "profile '<slug>' not found"}`.

---

### GET /api/v1/profiles/{slug}/profile.jsonld

Serve the stored `profile.jsonld` **verbatim**: the exact bytes the store
persisted, not a re-serialization from the loaded model. This is what makes
the `conformsTo` claim retrievable: a crawler or agent fetching this route
gets the published document byte for byte.

Supports conditional requests: the response carries a strong `ETag` (a
SHA-256 over the served bytes) and `Last-Modified`; a matching
`If-None-Match` gets a `304`.

**Status codes:** `200`; `304` (conditional hit); `400` bad slug; `404`
`{"detail": "profile '<slug>' not found"}`.

---

### PUT /api/v1/profiles/{slug}

Upload (create or replace) a profile. The route dispatches on `Content-Type`:
`application/json` takes a bare profile document (see
[JSON body](#json-body) below); any other content type is read as a tarball.

**Tarball body.** A (gzipped) tar archive of one profile directory's
contents: `profile.jsonld` at the tar root, not nested inside a directory. The
archive is validated and staged, then committed into the store: an atomic
directory swap on a filesystem store, one transaction on a SQL one. On any
failure the existing profile is left untouched.

Client-side, `push_profile` / `rp push` builds the archive
with the whole-record `build_profile_archive`, and by default omits
`sources/papers/` (lite push). Pass `--include-fulltext` (CLI) or
`include_fulltext=True` (Python) to include extracted paper text.
Server-side, `sources/papers/` members are **stripped on ingest** unless the
server runs with `RESEARCHER_PROFILES_ACCEPT_FULLTEXT=true` (default false).
The registry refuses to store copyrighted paper text merely because a client
sent it.

`GET /api/v1/profiles/{slug}/archive` uses a different builder,
`build_viewer_archive`, which projects the profile through the caller's privacy
tier and never ships full text to anyone. See
[Privacy](../../rp-spec/privacy.md).

After a successful push the server drops the cached profile object, the
in-memory roster snapshot, and the on-disk `<root>/.cache/` caches, so the
pushed profile is immediately visible to `GET /api/v1/profiles` and (when it
ships a built `.cache/embeddings.sqlite` index) to `POST /api/v1/match`,
without a restart.

**Validation:**

- `slug` must match `^[a-z0-9][a-z0-9-]*$`.
- The archive must contain `profile.jsonld` at its root.
- Members may only be regular files and directories: symlinks, hardlinks,
  device nodes, absolute paths, and `..` traversal are all rejected.
- The staged profile must load as a `ResearcherProfile` before the swap.
- Size cap: 50 MB by default (`RESEARCHER_PROFILES_MAX_UPLOAD_MB`, or
  `create_app(..., max_upload_bytes=...)`).

**Response 200** (`PushResponse`):

| Field | Type | Notes |
|---|---|---|
| `slug` | string | |
| `rid` | string \| null | The stored profile's identity. |
| `name` | string | From the pushed `profile.jsonld`. |
| `level` | string | Profile depth tier (`lite`, `full`, ...). |
| `indexed` | boolean | True when the push carried `.cache/embeddings.sqlite`, i.e. the profile is immediately matchable. Always `false` for a JSON body. |

```bash
tar -C /path/to/profiles/jane-doe -czf - . | curl -X PUT \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/gzip" \
    --data-binary @- \
    http://127.0.0.1:8109/api/v1/profiles/jane-doe
```

Prefer the CLI (`rp push`) or `client.push_profile(...)`,
which build the tarball for you (excluding dotfiles like `.archive/`).

**Status codes:**

- `200` on success.
- `400` `{"detail": "<reason>"}`: bad slug, empty body, unreadable archive,
  unsafe member, missing `profile.jsonld`, or a staged profile that fails to
  load.
- `401` as elsewhere.
- `413` `{"detail": "archive exceeds size cap"}`.

#### JSON body

With `Content-Type: application/json` the body is one `profile.jsonld`
document. The server parses it as a `ProfileDocument` and writes it with
`store.put_document`, creating a document-only profile (identity plus
metadata) or replacing the document of an existing one. Papers, summaries,
and indexes are added later by a tarball push or a build.

- The document must carry a `rid`. To mint a `local:` identity instead, send
  `"mintLocalRid": true` in the body or `?mint=local` on the URL; the name must
  be non-empty. Minting when the document already has a `rid` is a `400`.
- `If-Match: <content_hash>` makes the write conditional: when the profile
  exists and its current `content_hash` differs, the response is a `409`
  `{"detail": "content hash mismatch (concurrent edit)"}` with the current
  hash in `X-RP-Content-Hash`. `If-Match` is ignored when the profile does not
  exist yet.

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary @profile.jsonld \
    "http://127.0.0.1:8109/api/v1/profiles/jane-doe?mint=local"
```

**Response 200** (`PushResponse`), with `indexed: false`.

**Status codes:**

- `200` on success.
- `400` `{"detail": "<reason>"}`: bad slug, invalid JSON, a body that is not
  an object, a missing `rid` without minting, or a minting conflict.
- `401`/`403` missing or insufficient scope.
- `409` on an `If-Match` mismatch, or when the store refuses the write.
- `422` `{"detail": "<pydantic errors>"}` when the body is not a valid
  `ProfileDocument`.

---

### GET /api/v1/profiles/{slug}/archive

Download a profile as a gzipped tarball, the inverse of `PUT`. Requires the
`push` scope. The archive is projected through the caller's viewer tier: it
contains exactly what the JSON read surface would serve that caller. Nothing
above their tier is included, and the hard floors (copyrighted full text,
`.cache/`, `.keys/`) never are.

The response carries `X-RP-Archive-Digest` (MD5 of the body, so the client
can verify the transfer before committing it to its cache),
`X-RP-Archive-Tier` (naming the tier the archive was built for), and
`X-RP-Profile-Level` (the profile's depth tier).

**Status codes:** `200` (`Content-Type: application/gzip`); `400` bad slug;
`401`/`403` missing or insufficient scope; `404` profile not found.

---

### POST /api/v1/identity/resolve

Resolve a person descriptor (a rid, or a free-text name) to the rid that
identifies them in this registry. Requires the `resolve` scope: a write
scope, not `match`, because a true miss mints a stub profile (`level=lite`,
`visibility=internal`, `provenance=third_party`). Putting this behind the
read-tier match scope would let every match-keyed consumer create people.
Resolution is deterministic and cautious: the same person resolved the same
way twice converges on the same rid, and undecidable evidence defers rather
than guessing. See [`researcher_profiles.resolve`](python-api.md#identity-resolution-researcher_profilesresolve)
for the pipeline this route calls, and
[`client.resolve_rid`](python-api.md#resolve_rid-module-level) for the client-side half.

**Request body** (`ResolveRequest`):

| Field | Type | Notes |
|---|---|---|
| `rid` | string \| null | An ORCID, or a previously minted `local:` id (the disambiguation round-trip). |
| `name` | string \| null | A free-text name to resolve. |
| `affiliation` | string \| null | Stamped on a minted stub; corroborates or vetoes a name match. |
| `create_new` | boolean | Skip matching and mint a fresh identity for `name`, the explicit answer to a deferral whose candidates are all wrong. Default `false`. |

At least one of `rid`/`name` is required.

**Response 200 or 201** (`ResolveResponse`):

| Field | Type | Notes |
|---|---|---|
| `rid` | string \| null | The resolved rid, or `null` when the request defers. |
| `created` | boolean | Whether this call minted a new profile. |
| `confidence` | string | `"exact"` (a rid identity), `"high"` (a corroborated name match or a fresh mint), or `"low"` (a deferral). |
| `candidates` | list[object] | Present only when non-empty: the profiles an undecidable name could mean, each `{rid, name, affiliation}`. |

`201` on a true miss (a new identity was minted); `200` otherwise, including a
deferral.

```bash
curl -X POST http://127.0.0.1:8109/api/v1/identity/resolve \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"name": "Grace Hopper"}'
```

Prefer `client.resolve_rid(...)`, which builds the request and unpacks the
response into a `ResolveResult` for you.

**Status codes:**

- `200` on a rid identity, a corroborated name match, or a deferral.
- `201` on a true miss (a new identity was minted).
- `400` `{"detail": "<reason>"}`: neither `rid` nor `name`, a malformed rid,
  or an unknown `local:` rid.
- `401`/`403` missing or insufficient scope.

---

### GET /api/v1/collection.json

The same listing as `GET /api/v1/profiles`, in the **collection bundle** shape a
static site publishes. It exists for a browser client whose home view fetches a
*document* rather than calling an API, so one build reads a hosted registry and
a rendered directory of files without knowing which it is talking to. It is not
identical to the static `collection.jsonld`: this dynamic bundle carries no
centroids (see the `artifacts` row below) and a client ranks against it by
calling `/match`, while the static file carries the centroids inline.

**Response 200**, a collection bundle:

| Field | Type | Notes |
|---|---|---|
| `@context` / `@id` | string | The profile context IRI; the request URL. |
| `generated_at` | string | ISO 8601, when this response was built. |
| `generator` | string | `researcher-profiles/<version>`. |
| `count` | integer | Number of cards. |
| `cards` | list | One per visible profile: the `ProfileSummary` fields plus `base`. |
| `backend_spec` / `dim` / `artifacts` | null / null / `[]` | No centroid blob is served over HTTP: it is derived from `.cache/`, a hard floor. Clients fall back to server-side `/match`. |

Each card's `base` is the absolute `.../api/v1/profiles/{slug}/content/`, the
URL that `profile.jsonld` and every relative `contentUrl` resolve from. It is
absolute rather than root-relative because a client parses it with a bare URL
constructor, which has no document to resolve a relative path against.
`X-Forwarded-Proto` and `X-Forwarded-Host` win over what the ASGI server saw,
so TLS terminated at a proxy does not produce `http://` bases on an `https://`
page.

Membership is exactly the membership of `GET /api/v1/profiles` for the same
caller, computed by the same walk. `Cache-Control: private, no-store`: who is in
the list depends on who asked.

**Status codes:** `200`.

---

### GET /api/v1/profiles/{slug}/content/{artifact}

Serve one manifest artifact, projected through the caller's viewer tier.

`/api/v1/profiles/{slug}/content/` is a **base URL**: the literal
`profile.jsonld` resolves out of it, and so does every relative `contentUrl` the
manifest names. That is what lets a client point at one base and follow the
document, exactly as against a static site.

`{artifact}` must be a `contentUrl` present in the manifest (or `profile.jsonld`
itself); any other path, including a traversal, is `404`.

**Response 200**: the artifact bytes, with the manifest's `encodingFormat` as
the content type. `X-RP-Effective-Tier` reports the artifact's effective tier
with the derivation rule applied, so a derivative never advertises a looser tier
than its sources. `Cache-Control: private, no-store`.

**Status codes:** `200`; `404` when the profile is not visible to this caller,
the artifact is not in the manifest, or the artifact's effective tier is above
this caller's viewer tier, all with indistinguishable bodies; `403` for the
[hard floors](../../rp-spec/privacy.md) (`paper_fulltext`, `.cache/`,
`.keys/`), which are withheld from every caller including the owner.

---

### GET /api/v1/profiles/{slug}/papers

List every paper attached to the profile.

**Response 200** (`list[PaperEntry]`):

| Field | Type | Notes |
|---|---|---|
| `paper_id` | string \| null | Citation key (e.g. `doe2016example`). |
| `title` | string | Required. |
| `year` | integer \| null | |
| `journal` | string \| null | |
| `first_author` | string \| null | |
| `authors` | list[string] \| null | Full author list when the record carries one. |
| `doi` | string \| null | |
| `pmid` | string \| null | |
| `openalex_id` | string \| null | |
| `full_text_link` | string \| null | |
| `summary_available` | boolean | True iff `paper_id` is non-null, a summary file exists, and this viewer's tier may fetch it. |

**Status codes:** `200`, `404`.

---

### GET /api/v1/profiles/{slug}/summary/{paper_id}

Fetch the markdown summary for one paper.

**Response 200** (`PaperSummary`):

| Field | Type | Notes |
|---|---|---|
| `paper_id` | string | Echoes the request. |
| `summary` | string | Full summary markdown. |

**Status codes:** `200`; `404` if the profile is missing or `paper_id` has no
summary: `{"detail": "summary '<paper_id>' not found for profile '<slug>'"}`.

---

### POST /api/v1/profiles/{slug}/search

Semantic search over the profile's chunk-level sqlite-vec index.

**Request body** (`SearchRequest`):

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | yes | none | Whitespace-only queries return an empty hit list. |
| `k` | integer | no | `5` | Number of hits. |
| `filter` | object \| null | no | `null` | Only `source_type` is recognized (string or list). Candidates are fetched at `k*4` then post-filtered. |

**Known `source_type` values**, which depend on the profile's
[level](../profile-format.md#profile-depth-levels): `paper_summary`, `expertise`,
`soul` (`full` and `deep`); `paper_abstract` (`lite` only); `grant`, `cv`, `web`
(`deep` only, and only for sources that profile actually carries).

**Response 200** (`SearchResponse`, with `hits: list[SearchHitPayload]`):

| Field | Type | Notes |
|---|---|---|
| `text` | string | Raw chunk text. |
| `source_type` | string | See the known values above. |
| `source_id` | string | Paper id for summaries and abstracts; grant `id` for grants; page filename stem for web pages; `expertise`, `soul`, or `cv` for those documents. |
| `chunk_index` | integer | 0-based chunk index within the source. |
| `section` | string \| null | Section heading for markdown chunks; null otherwise. |
| `score` | float | Cosine similarity in `[0, 1]`, rounded to 4 decimals. |
| `meta` | object | Free-form. May contain `{"abstract_only": true}`. |

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"query": "region set enrichment", "k": 3, "filter": {"source_type": "paper_summary"}}' \
    http://127.0.0.1:8109/api/v1/profiles/jane-doe/search
```

**Status codes:**

- `200` on success (including empty `hits`).
- `401`, `404` as elsewhere.
- `500` `{"detail": "search failed: <message>"}` if `.search` raises. For
  example, a profile whose `.cache/embeddings.sqlite` has not been built raises
  `IndexNotBuiltError`, surfaced as `500` `{"detail": "search failed: No index
  at <path>. Call build_index() first."}`. Treat 5xx as "search not available
  for this profile" and fall back.
- `501` when the profile's backend has no local directory at all (a SQL store
  or a static host), so no index can exist there. The body is the
  `CapabilityUnavailableError` message:
  `{"detail": "search needs a local profile directory (<what>); <remedy>"}`.

---

### POST /api/v1/match

Rank all indexed profiles against a free-text query. Runs the store's centroid
prefilter + chunk re-rank + optional MMR diversification (`store.match.rank`).
This route is **not** profile-scoped.

**Request body** (`MatchRequest`):

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | yes | none | Free-text query. |
| `k` | integer | no | `5` | Number of matches to return. |
| `prefilter` | integer | no | `10` | Centroid-prefilter width before chunk re-rank. |
| `require_topics` | list[string] \| null | no | `null` | Restrict candidates to profiles carrying these topics. |
| `diversify` | boolean | no | `true` | Apply MMR diversification. |
| `lambda_` | float | no | `0.5` | MMR relevance/diversity trade-off. |
| `topk_chunks` | integer | no | `5` | Chunks used per profile in the re-rank. |
| `normalize` | boolean | no | `true` | Apply per-profile score calibration. |
| `include_chunks` | boolean | no | `false` | Include chunk-level evidence in each result. |

**Response 200** (`MatchResponse`):

| Field | Type | Notes |
|---|---|---|
| `matches` | list[`MatchResult`] | The ranked results. |
| `ranked_profiles` | integer | How many profiles were actually ranked, after privacy-tier filtering. |
| `total_profiles` | integer | Size of the indexed corpus this query ran against. |

The two counts let a caller tell "0 of 47 ranked" (a broken embedding path)
from "47 of 47 ranked, none above threshold." An empty `matches` list alone
cannot make that distinction. Setting `RESEARCHER_PROFILES_MATCH_MIN_RANKED_FRACTION` makes the route
fail loud with a `503` when `ranked_profiles / total_profiles` falls below that
fraction; it defaults to `0` (disabled), since a narrow result is usually
legitimate.

Each `MatchResult`:

| Field | Type | Notes |
|---|---|---|
| `slug` | string | Matched profile slug. Useful for links; not a join key. |
| `name` | string | Profile name. |
| `rid` | string \| null | The join key: an ORCID or a `local:` id. Map matches onto your own users by this, never by name. |
| `orcid` | string \| null | ORCID when the profile carries one; `null` for a `local:` rid. |
| `score` | float | Match score (calibrated when `normalize=true`). |
| `evidence` | `MatchEvidencePayload` | See below. |

`MatchEvidencePayload`:

| Field | Type | Notes |
|---|---|---|
| `centroid_score` | float | Cosine of query to the profile centroid. |
| `top_papers` | list[string] | Paper ids of the top-matching summary chunks. |
| `overlapping_topics` | list[string] | Profile topic labels that overlap the query. |
| `top_chunks` | list[`SearchHitPayload`] | Populated only when `include_chunks=true`; otherwise `[]`. |

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"query": "region set enrichment analysis", "k": 5}' \
    http://127.0.0.1:8109/api/v1/match
```

```json
{
  "matches": [
    {
      "slug": "jane-doe",
      "name": "Jane Doe",
      "rid": "0000-0002-1825-0097",
      "orcid": "0000-0002-1825-0097",
      "score": 0.969,
      "evidence": {
        "centroid_score": 0.71,
        "top_papers": ["doe2016example"],
        "overlapping_topics": [],
          "top_chunks": []
      }
    }
  ],
  "ranked_profiles": 7,
  "total_profiles": 7
}
```

**Status codes:**

- `200` on success.
- `401` if auth fails.
- `500` `{"detail": "match failed: <message>"}` if ranking raises.
- `503` `{"detail": "matching unavailable: ..."}` when the server lacks the
  `vectors`/`st` extras, the roster failed to build, or no profile is
  indexed; also `503` `{"detail": "match ranked <n>/<total> profiles, below
  the configured floor ..."}` when `RESEARCHER_PROFILES_MATCH_MIN_RANKED_FRACTION`
  is set and the ranked fraction falls below it. Callers should degrade
  gracefully on 503.

The Python client for this endpoint is the module-level
`researcher_profiles.client.rank_against`; see
[How to access profiles over HTTP](../how-to/access-profiles-over-http.md#match-profiles-over-http).

---

### POST /api/v1/coi/check

Conflict-of-interest check between a candidate reviewer and a manuscript's
author set. Requires the `match` scope. Returns every COI edge between them:
coauthorship within `years`, a shared institution, or an advising
relationship, with the reason and parameters that fired. An author supplied
with only a name and affiliation (no profile) still trips a same-institution
COI.

**Request body:**

| Field | Type | Default | Description |
|---|---|---|---|
| `author_set` | list of author descriptors | `[]` | Each: `name`, `orcid`, `rid`, `affiliation`, `affiliation_id`, any subset. |
| `candidate` | string | none | The reviewer to check: a slug or rid. |
| `years` | integer | `4` | Coauthorship recency window. |

**Response 200:** `{candidate, rid, has_coi, reasons: [...]}`, where each
reason names its `type` (`coauthor` \| `shared_institution` \| `advised`),
the author it fired against, and supporting detail.

**Status codes:** `200`; `401`/`403` missing or insufficient scope; `404`
`{"detail": "candidate '<candidate>' not found in graph"}`.

---

### POST /api/v1/match/reviewers

Expertise ranking (as `/match`) composed with a COI filter. This is a thin
wrapper over the same ranking code. Requires the `match` scope.

**Request body:** every `/match` field, plus:

| Field | Type | Default | Description |
|---|---|---|---|
| `author_set` | list of author descriptors | `[]` | The manuscript's authors, for COI filtering. |
| `years` | integer | `4` | Coauthorship recency window. |
| `mode` | `"drop"` \| `"annotate"` | `"drop"` | `drop` removes conflicted candidates; `annotate` keeps them and attaches a `coi` block. |

**Response 200:** `{matches: [...]}`, each a `/match` result plus a `coi`
block (present when a candidate has a COI: always in `annotate` mode, only
on retained-but-flagged candidates otherwise; `drop` mode omits conflicted
candidates entirely).

**Status codes:** `200`; `400` `mode` is neither `drop` nor `annotate`; `500`
`{"detail": "match failed: <message>"}`.

---

### GET /api/v1/graph/neighbors/{ref}

Neighborhood of one person: coauthors and collaborators-of-collaborators.
Requires the `match` scope. Backs team assembly and the collaboration
recommender.

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `types` | all types | Comma-separated edge types. |
| `since_year` | none | Recency filter on coauthor edges. |
| `max_hops` | `1` | `1` for direct neighbors, `2`-`3` for the reachable-but-not-direct frontier. |

**Response 200:** `{center, neighbors: [{node, hops, edges}, ...]}`.

**Status codes:** `200`; `400` an invalid `types` value, or `since_year` /
`max_hops` not an integer, or `max_hops` outside `1`-`3`; `404`
`{"detail": "ref '<ref>' not found in graph"}`.

---

### POST /api/v1/profiles/{slug}/rank-works

Rank candidate works against ONE profile, the inverse of `/match`. Requires
the `match` scope.

Two modes: supply candidate `works` (`PaperRecord`-shaped dicts), or set
`use_openalex=true` to fetch works published since `since` (default: the last
30 days) from OpenAlex, seeded by the profile's own topics and citation
neighborhood.

**Request body:**

| Field | Type | Default | Description |
|---|---|---|---|
| `since` | string \| null | 30 days back | `YYYY-MM-DD`, for `use_openalex`. |
| `k` | integer | `10` | Number of ranked works to return. |
| `kind` | `"centroid"` \| `"summary"` \| `"expertise"` | `"centroid"` | Which profile vector to rank against. |
| `diversify` | boolean | `true` | MMR-style diversification. |
| `lambda_` | float | `0.5` | Diversification tradeoff. |
| `threshold` | float \| null | none | Drop works scoring below it. |
| `use_openalex` | boolean | `false` | Fetch candidates from OpenAlex instead of `works`. |
| `works` | list of objects \| null | none | Candidate works, `PaperRecord`-shaped. |
| `mailto` | string \| null | none | OpenAlex polite-pool contact. |
| `max_pages` | integer | `5` | OpenAlex pagination cap. |

**Response 200:** `{slug, rid, works: [...]}`, each a ranked work with its
score.

**Status codes:** `200`; `400` an invalid candidate work in `works`, neither
`works` nor `use_openalex` supplied, or a profile with no subfields, interests,
or OpenAlex work ids to query with; `502` `{"detail": "OpenAlex fetch failed:
<message>"}`; `500` `{"detail": "work ranking failed: <message>"}`; `503`
`{"detail": "matching unavailable: ..."}` when the server lacks the
`vectors`/`st` extras or the profile has no built index.

---

### POST /api/v1/profiles/{slug}/ask

Single-shot question answering in the researcher's voice, grounded by retrieval.

**Request body** (`AskRequest`):

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `question` | string | yes | none | The question. |
| `k` | integer | no | `5` | Evidence chunks retrieved. |
| `model` | string \| null | no | `null` | Anthropic model override. Default `claude-sonnet-4-6`. |
| `strict_corpus` | boolean | no | `false` | Refuse (no LLM call) if top retrieval score is below `refusal_threshold`. |
| `refusal_threshold` | float \| null | no | `null` | Overrides the env default (`0.4`). Only consulted when `strict_corpus` is true. |
| `history` | array of `{role, content}` \| null | no | `null` | Prior turns. Trimmed from the head when total content exceeds ~12000 chars. |

**Response 200**: `LLMTextResponse` (see [Common response shapes](#common-response-shapes)).

On a tripped refusal gate the response is still `200` with `text` set to the
refusal message, `refused=true`, `refusal_reason` populated, `model="<none>"`,
zeroed `usage`, and empty `citations`.

**Status codes:** `200`, `401`, `404`, `409` (profile has no synthesized persona),
`500` (`{"detail": "ask failed: <message>"}`).

---

### POST /api/v1/profiles/{slug}/review

Persona reviews the supplied material. Retrieval is seeded from `focus` if
provided, otherwise from the first lines of `material`.

**Request body** (`ReviewRequest`):

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `material` | string | yes | none | Text to review. |
| `focus` | string \| null | no | `null` | Focus hint; seeds retrieval and shapes the review. |
| `k` | integer | no | `5` | Evidence chunks retrieved. |
| `model` | string \| null | no | `null` | Model override. |
| `strict_corpus` | boolean | no | `false` | Same refusal semantics as `ask`. |
| `refusal_threshold` | float \| null | no | `null` | Same as `ask`. |

**Response 200**: `LLMTextResponse`. On refusal, `text` is the review-refusal
message and `refused=true`.

**Status codes:** `200`, `401`, `404`, `409` (profile has no synthesized persona),
`500` (`{"detail": "review failed: <message>"}`).

---

### POST /api/v1/profiles/{slug}/innovate

Generate `n` novel research directions on a topic, as the researcher. The model is
told to return strict JSON; retries once on parse failure.

**Request body** (`InnovateRequest`):

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `topic` | string | yes | none | Topic area. |
| `n` | integer | no | `3` | Ideas requested. Returned `items` may be fewer. |
| `k` | integer | no | `12` | Evidence chunks retrieved as prior-work context. |
| `model` | string \| null | no | `null` | Model override. |
| `temperature` | float | no | `0.7` | Sampling temperature. |

**Response 200** (`IdeaList`, with `items: list[IdeaPayload]`):

| Field | Type | Notes |
|---|---|---|
| `hypothesis` | string | One-sentence testable claim. |
| `approach` | string | Data, method, comparison. |
| `rationale` | string | Why this researcher specifically. |
| `related_works` | list[string] | Citation keys. Not guaranteed to match the profile's real `paper_id`s. |

**Status codes:**

- `200` on success.
- `401`, `404` as elsewhere.
- `409` (profile has no synthesized persona): no LLM call is made.
- `502` `{"detail": "LLM parse error: <message>"}` if the model fails to produce
  valid JSON on both attempts (`GenerativeParseError`).
- `500` `{"detail": "innovate failed: <message>"}` for any other error (e.g. a
  missing `ANTHROPIC_API_KEY`).

To map `related_works` to real papers, intersect the keys with
`GET /api/v1/profiles/{slug}/papers`.

---

### POST /api/v1/profiles/{slug}/riff

Generate `n` short divergent brainstorm fragments on a seed. JSON-constrained;
retries once on parse failure.

**Request body** (`RiffRequest`):

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `seed` | string | yes | none | Phrase or short paragraph to riff on. |
| `n` | integer | no | `5` | Riffs requested. |
| `k` | integer | no | `4` | Evidence chunks retrieved (sparser than `innovate`). |
| `model` | string \| null | no | `null` | Model override. |
| `temperature` | float | no | `1.0` | Higher than `innovate` to encourage divergence. |

**Response 200** (`RiffList`, with `items: list[RiffPayload]`):

| Field | Type | Notes |
|---|---|---|
| `angle` | string | Short free-form label. |
| `text` | string | 2 to 5 sentences in the researcher's voice. |
| `related_work` | string \| null | Optional single citation key; `""` and `"null"` are normalized to `null`. |

**Status codes:** `200`, `401`, `404`, `409` (profile has no synthesized persona),
`502` (parse error), `500`. Same semantics as `innovate`.

## The owner edit surface

Four routes, gated by `require_owner` rather than by the consumer token (see
[Authentication](#authentication)). On bare rp-sdk that falls back to the
operator bearer token. A host that runs the
[management tier](../../rp-spec/dynamic-api.md#14-management-api) sets
`app.state.owner_verifier` and these become per-person and per-profile.

| Method | Path | Description |
|---|---|---|
| `PATCH` | `/api/v1/profiles/{slug}/metadata` | Patch owner-editable metadata. |
| `PUT` | `/api/v1/profiles/{slug}/soul` | Replace `personality/SOUL.md` whole. |
| `GET` / `PATCH` | `/api/v1/profiles/{slug}/visibility` | Read / set artifact tiers. |

### What an owner may edit

`researcher_profiles.edit.EDITABLE_METADATA_FIELDS`, exactly:

`name`, `affiliation`, `job_title`, `field`, `subfields`, `summary`,
`expertise` (the label list), `interests`, `not_interests`, `training`,
`career`, `same_as`.

A key outside that set (`rid`, `provenance`, `visibility`, and everything a
build tool generates) is a hard `400`, never a silent drop. `slug`
is popped by the route, so renaming is not an edit. `personality/expertise.md`
is unrouted: it is synthesized from the paper corpus and cites
paper ids, and it is a different thing from the editable `expertise` labels
despite the shared name.

`training` and `career` arrive as lists of objects and are validated against
`schema.Training` / `schema.CareerEntry`. A malformed entry is a `400` naming
the index (`career[0] is not a valid CareerEntry: ...`) and nothing is written.

### Optimistic concurrency (`base_hash` -> 409)

`GET /api/v1/profiles/{slug}` returns `content_hash`. Send it back as
`base_hash` on a metadata patch or a soul write and a concurrent change becomes
a `409` carrying the current digest in the body and in `X-RP-Content-Hash`:

```json
{"detail": "this profile changed since you loaded it (current content_hash sha256:...); reload it and re-apply your edit"}
```

Omit `base_hash` and the write is last-writer-wins. A
single-owner CLI does not need a token. Every successful edit returns the new
`content_hash` on `EditResult`, so a form held open can chain writes without
re-reading. The digest spans the document and the SOUL together, so the two
routes share one clock rather than each keeping a private one.

---

## Common response shapes

### `ProfileMetadataPayload`

Wire shape of `profile.jsonld`. `extra="allow"`: additional keys round-trip
unchanged.

| Field | Type | Notes |
|---|---|---|
| `name` | string | Required. |
| `level` | string | Depth tier: one of `lite`, `full`, `deep`. Default `"full"`. See [profile depth levels](../profile-format.md#profile-depth-levels). |
| `rid` | string \| null | Identity: an ORCID or a `local:` id. There is no `orcid` key; derive it from `rid` with `orcid_of(rid)`. |
| `provenance` | string \| null | Who asserted this profile and on what basis (`schema.Provenance`). |
| `license` | string \| null | Reuse terms for the published record, an IRI. |
| `url` | string \| null | The published profile URL. |
| `affiliation` | string \| null | |
| `scholar_url` | string \| null | |
| `openalex_id` | string \| null | |
| `field` | string \| null | |
| `subfields` | list[string] | Default `[]`. |
| `summary` | string \| null | |
| `job_title` | string \| null | |
| `training` | list[dict] | Authored history; entries match `schema.Training`. |
| `career` | list[dict] | Authored history; entries match `schema.CareerEntry`. |
| `expertise` | list[string] | Default `[]`. Distinct from the `expertise` markdown on `ProfileDetail`. |
| `interests` | list[string] | Default `[]`. |
| `not_interests` | list[string] | Default `[]`. Authoritative: a consumer must not improvise around them. |
| `same_as` | list[string] | Default `[]`. Other URLs for the same person. |
| `visibility` | string | The document's OWN declared tier. Read-only here. Set it through `PATCH .../visibility`, never a metadata patch. |

### `LLMTextResponse` (shared by `ask` and `review`)

| Field | Type | Notes |
|---|---|---|
| `text` | string | The model's prose. |
| `citations` | list[`CitationRefPayload`] | One entry per retrieved evidence chunk (built from retrieval, not parsed from the text). Empty on refusal. |
| `model` | string | Resolved model id. `"<none>"` on a refusal with no LLM call. |
| `usage` | object | Token usage: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`. Zeroed on refusal. |
| `request_id` | string \| null | Anthropic request id when available. |
| `refused` | boolean | True when the `strict_corpus` gate fired. |
| `refusal_reason` | string \| null | Populated only when `refused=true`. |
| `grounded` | boolean | False if the model cited a `[paper_id]` not in the profile. Always true for refusals and when no citation was emitted. |

### `CitationRefPayload`

| Field | Type | Notes |
|---|---|---|
| `paper_id` | string | |
| `relevance` | float \| null | Cosine similarity from retrieval. |
| `span` | string \| null | Reserved; typically null. |

### Error envelope

Errors follow the FastAPI default: `{"detail": "<string>"}` with the appropriate
HTTP status. There is no global error wrapper or machine-readable error code.

## Limits and gotchas

The server applies no rate limiting or concurrency caps. Throttle on the
client side for multi-profile workflows.

Every successful `ask`/`review`/`innovate`/`riff` call makes at least one
Anthropic API call (`innovate`/`riff` up to two on a JSON retry). The persona
prefix is sent with prompt caching; check `usage.cache_read_input_tokens` to
confirm cache hits.

`ANTHROPIC_API_KEY` is not checked at startup. A missing key surfaces only
when an LLM endpoint is invoked, as a `500`.

The profile cache is in-process. Loaded profiles live until LRU eviction
(`capacity=32`) or process exit. Restart to pick up edits to
`profile.jsonld`, `expertise.md`, `SOUL.md`, or paper files.

`POST .../search` returns `500` (sometimes `501`) for profiles without a
built `.cache/embeddings.sqlite`.

`/api/v1/match` requires embeddings and indexes. It returns `503` on a
core-only server or when no profile is indexed.

`ask`/`review`/`innovate`/`riff` call `prof.index.search(...)` for evidence.
If search raises, the exception is swallowed and the LLM call proceeds with
no evidence. The response is less grounded.

`ask`/`review`/`innovate`/`riff` return `409` (no LLM call) when the profile
has no synthesized persona, for example a `lite` profile. Branch on the
`level` field in `GET /api/v1/profiles` to avoid calling them.

`strict_corpus` only applies to `ask` and `review`. `innovate` and `riff`
always call the model.

`citations` in `LLMTextResponse` reflects the retrieved chunk set (cosine
scores as `relevance`). Parse `[paper_id]` tokens out of `text` to know what
the model actually cited.

`ask` has no streaming variant in v1.

`/health` works without auth and reveals `store` (a directory path or a
database URL, which may carry a username).

## Multi-profile workflows

For cross-profile ranking, prefer `POST /api/v1/match`, which runs `store.match`'s
centroid prefilter + chunk re-rank + MMR server-side and returns ranked profiles
with evidence. For richer signals you can still combine per-profile `/search` or
`/ask` calls and synthesize externally.

Because the LLM endpoints share the persona-prefix prompt cache *per profile*,
alternating between profiles defeats the cache for the swapped-out persona. Batch
all calls to one persona before switching where possible.

## The management tier is not implemented here

`rp login`, `rp whoami`, `rp agent`, and `rp profile` call endpoints under
`/api/manage/`, specified in
[Management API](../../rp-spec/dynamic-api.md#14-management-api). The server in
this package does not implement them: it has no accounts, no identity provider,
and no key store, so `POST /api/manage/cli-auth` answers `404` and the client
reports that the server offers no command-line login. Point those commands at a
server that implements the management tier.
