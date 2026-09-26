# Dynamic API

The dynamic API extends the [static API](static-api.md) with endpoints for
listing, searching, matching, and interacting with profiles programmatically.
It is an OPTIONAL tier. A server that only fulfills the static API is
conforming.

A server that provides the dynamic API MUST also satisfy all
[static API](static-api.md) requirements.

---

## 1. URL prefix

This specification defines endpoints as bare paths (e.g. `GET /profiles`). A
deployment SHOULD mount them under a versioned prefix such as `/api/v1/`, but
the prefix is a deployment decision, not part of the specification. The
reference implementation uses `/api/v1/`.

---

## 2. Authentication

See [Authentication](authentication.md) for the bearer token mechanism and
viewer tiers.

The dynamic API categorizes routes by authentication requirement:

| Route category | Token required |
|----------------|----------------|
| Health check | No |
| Read endpoints (list, detail, papers, summaries, content, registry) | No |
| Search, match, persona, upload, archive, identity resolution | Yes |
| Owner edit endpoints | Yes (owner-level) |
| Command-line login (`/api/auth/*`) | No (see [section 14.2](#142-command-line-login)) |
| Management endpoints (`/api/manage/*`) | Varies by endpoint (see [section 14](#14-management-api)) |

A server MAY run in **open mode** (no token configured), in which case all
routes accept any request.

Read endpoints resolve a [viewer tier](authentication.md#viewer-tiers) from
the caller's credentials and project each response accordingly.

A read endpoint MAY accept a preview query parameter `?as=anonymous|lab|owner`
that caps the resolved tier at `public`, `internal`, or `restricted`
respectively. The cap MUST only narrow the caller's tier, never widen it. An
unknown value returns `400`.

---

## 3. Profile slugs

A profile slug is a short identifier (e.g. `jane-doe`) used in URL paths.
Slugs MUST match `^[a-z0-9][a-z0-9-]*$`.

Any route that accepts a `{slug}` also accepts a **rid** (researcher ID) in
its place. The server resolves both to the same profile.

---

## 4. Health check

### GET /health

Liveness probe. Unauthenticated. Lives at the root, outside any versioned
prefix. A server providing the dynamic API MUST expose this endpoint.

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `"ok"`, or `"degraded"` |
| `store` | string | Display locator for the backing store |
| `profile_count` | integer | Number of visible profiles |
| `detail` | string \| null | Set only when degraded: why |

**Response 503:** the same body with `status: "degraded"`, when the server's
own startup checks (for example a query-embedding preflight) have failed. A
server MAY never return this.

---

## 5. Read endpoints

These endpoints require no authentication. Responses are projected through the
caller's viewer tier.

### GET /profiles

List all visible profiles in
[`rp:profileList`](static-api.md#8-profile-lists) format, the same envelope
used by static servers, so consumers can treat both interchangeably.

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `rp:profileList` | string | Format version string |
| `name` | string | Server or organization name |
| `url` | string | Canonical URL of this listing |
| `updated` | string | ISO 8601 timestamp of last change |
| `profiles` | array | Profile entries (see below) |

Each entry in `profiles` is an object with `url` (the profile's base URL)
and optional enrichment fields that a dynamic server MAY include:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | REQUIRED | Profile base URL, from which `profile.jsonld` and every relative `contentUrl` resolve |
| `name` | string | RECOMMENDED | Display name |
| `slug` | string | | Profile identifier |
| `rid` | string \| null | | The researcher ID this profile describes |
| `level` | string | | `lite`, `full`, or `deep` |
| `affiliation` | string \| null | | |
| `field` | string \| null | | |
| `paper_count` | integer | | Number of papers |
| `summary_count` | integer | | Papers with a summary |
| `fulltext_pct` | float | | Percentage with downloaded full text |
| `contaminated_count` | integer | | Papers flagged as contaminated |

A static server serves the same format as a `profiles.json` file with entries
as plain URL strings or `{url, name}` objects. A dynamic server enriches
entries with summary fields. Consumers MUST accept both forms.

Nested lists (`{list: ...}`) are also valid entries, per the
[profile list](static-api.md#8-profile-lists) format.

### GET /profiles/{slug}

Full profile detail.

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `slug` | string | |
| `rid` | string \| null | The researcher ID this profile describes |
| `metadata` | object | See [Profile metadata](#profile-metadata) |
| `expertise` | string \| null | Raw `personality/expertise.md` content. `null` when the viewer's tier does not reach that artifact (its `contentUrl` then appears in `withheld`); `""` when the file is empty |
| `soul` | string \| null | Raw `personality/SOUL.md` content. `null` when withheld, as for `expertise` |
| `manifest` | list[object] | The profile manifest entries (`hasPart` plus `subjectOf`) as a flat list, each with an added `effective_visibility` key, so a client knows what the profile contains without walking the directory. Not an envelope: there is no `entries` key. |
| `withheld` | list[string] | The `contentUrl`s this viewer's tier did not reach |
| `content_hash` | string \| null | `"sha256:<hex>"` for optimistic concurrency (see [Owner edit endpoints](#11-owner-edit-endpoints)) |

### GET /profiles/{slug}/profile.jsonld

Serve the stored `profile.jsonld` verbatim: the bytes the server persisted,
not a re-serialization. This is what makes the document's `conformsTo` claim
retrievable. The response carries a strong `ETag` and `Last-Modified`; a
matching `If-None-Match` returns `304`.

**Status codes:** `200`, `304`, `400` (malformed slug), `404`.

### GET /profiles/{slug}/content/{artifact}

Serve one manifest artifact, projected through the caller's viewer tier.

`{artifact}` MUST be a `contentUrl` from the profile's manifest, or
`profile.jsonld` itself. Any other path returns `404`. Path traversals return
`404`.

The response body is the artifact's raw bytes. The `Content-Type` header
matches the manifest entry's `encodingFormat`. The `X-RP-Effective-Tier`
header reports the artifact's effective privacy tier.

**Status codes:**

- `200`: artifact served
- `404`: profile not visible, artifact not in manifest, or artifact's
  effective tier is above the caller's viewer tier
- `403`: build-local hard-floor artifacts (`.cache/`, `.keys/`) that are
  withheld from all callers

### GET /profiles/{slug}/papers

List all papers attached to a profile.

**Response 200:** array of paper entries:

| Field | Type | Description |
|-------|------|-------------|
| `paper_id` | string \| null | Citation key |
| `title` | string | |
| `year` | integer \| null | |
| `journal` | string \| null | |
| `first_author` | string \| null | |
| `authors` | list[string] \| null | Full author list when the record carries one |
| `doi` | string \| null | |
| `pmid` | string \| null | |
| `openalex_id` | string \| null | |
| `full_text_link` | string \| null | |
| `summary_available` | boolean | Whether a summary exists for this paper and the caller's tier may fetch it |

### GET /profiles/{slug}/summary/{paper_id}

Fetch the markdown summary for one paper.

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `paper_id` | string | Echoes the request |
| `summary` | string | Full summary markdown |

Returns `404` if the profile or paper summary does not exist.

### GET /collection.json

List all visible profiles in collection bundle format, a static-site-shaped
document so a browser client can consume a dynamic server and a static file
host interchangeably. This dynamic bundle carries `artifacts: []` and no
centroids; a client ranks against it by calling `/match` on the server. The
static `collection.jsonld` a published site writes carries the centroids inline
instead.

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `@context` | string | The profile context IRI |
| `@id` | string | The request URL |
| `generated_at` | string | ISO 8601 timestamp |
| `generator` | string | Software identifier and version |
| `count` | integer | Number of profile cards |
| `cards` | array | One per visible profile (same fields as profile summary, plus `base`) |

Each card's `base` is the absolute content URL prefix
(`.../profiles/{slug}/content/`) from which `profile.jsonld` and all relative
`contentUrl` paths resolve.

`Cache-Control: private, no-store`: the list depends on who asked.

---

## 6. Profile upload and archive

### PUT /profiles/{slug}

Upload a profile. The server dispatches on `Content-Type`: `application/json`
carries a bare profile document (below); any other content type carries a
gzipped tar archive of the profile directory's contents, with `profile.jsonld`
at the tar root.

**Request (archive):** `Content-Type: application/gzip`

The server validates the archive, stages it, and commits it atomically. On
failure the existing profile is untouched. The uploaded profile is immediately
visible to all read endpoints.

**Validation rules:**

- Slug must match `^[a-z0-9][a-z0-9-]*$`
- Archive must contain `profile.jsonld` at its root
- Only regular files and directories allowed (no symlinks, hardlinks, device
  nodes, absolute paths, or `..` traversal)
- The staged profile must load successfully before the swap
- Maximum archive size: 50 MB (configurable)

By default, `sources/papers/` (full paper text) is stripped on ingest. The
server may be configured to accept it.

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `slug` | string | |
| `rid` | string \| null | The stored profile's researcher ID |
| `name` | string | From the uploaded `profile.jsonld` |
| `level` | string | Profile depth tier |
| `indexed` | boolean | Whether the upload included a search index (always `false` for a JSON body) |

**Status codes:** `200`, `400` (validation failure), `401`, `403` (see
below), `413` (size cap exceeded).

**Uploads by an app or account key.** A caller holding `push`, or `push_own`
on a profile its person may write, replaces the profile outright. A server
implementing [per-part access](authentication.md#per-part-access) MUST also
accept an upload from an app or account key that holds neither, when the
target profile exists, the caller may write it, and the caller's table holds
`write` on at least one part. Such an upload:

- MUST NOT create a profile;
- MUST be compared with the profile it would replace, part by part, before
  anything is written: a document field changed counts against the part that
  governs it, and a file added, removed, or whose bytes changed counts against
  its role's part (so does a change to its manifest entry). A file with no
  bytes on either side (full text the server never held) is unchanged. A
  change of what the public sees, a field no part covers, or an
  infrastructure file counts against no part;
- lands only if every part it changes is `write` and nothing it changes is
  outside every part. Otherwise the server MUST return `403` with the
  [`insufficient_access`](authentication.md#insufficient-access) body,
  `needs_replace` included, and MUST change nothing.

To change one file without restating the rest, the caller sends only that file
with `?mode=merge`. The document still travels, so its sections must match the
server's. A caller that may write nothing of the target gets
[`insufficient_scope`](authentication.md#insufficient-scope) for `push`, the
same answer whether or not the profile exists.

**Request (JSON document):** `Content-Type: application/json`

The body is one `profile.jsonld` document. The server validates it against the
profile document schema and stores it as a document-only profile (identity
plus metadata); papers, summaries, and indexes are added later by an archive
upload or a build.

- The document MUST carry a `rid`, or the request MUST ask the server to mint a
  `local:` identity by sending `"mintLocalRid": true` in the body or
  `?mint=local` on the URL. Minting requires a non-empty `name`. Minting when
  the document already carries a `rid` is a `400`.
- `If-Match: <content_hash>` makes the write conditional. When the profile
  exists and its current `content_hash` differs, the server returns `409` with
  the current hash in `X-RP-Content-Hash`. `If-Match` is ignored when the
  profile does not exist yet.

**Response 200:** the same shape as the archive upload, with `indexed: false`.

**Status codes:** `200`, `400` (invalid JSON, a body that is not an object, a
missing `rid` without minting, or a minting conflict), `401`, `403`, `409`
(`If-Match` mismatch, or the store refused the write), `422` (the body is not
a valid profile document). The part-by-part rule above applies to a JSON
upload too; a file it does not carry keeps the bytes it has.

### GET /profiles/{slug}/archive

Download a profile as a gzipped tar archive, projected through the caller's
privacy tier. Full text is never included in the download regardless of caller.

---

## 7. Semantic search

### POST /profiles/{slug}/search

Search over one profile's vector index.

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | yes | none | Search query |
| `k` | integer | no | `5` | Number of results |
| `filter` | object \| null | no | `null` | Filter by `source_type` (string or list) |

**Response 200:** object with `hits` array:

| Field | Type | Description |
|-------|------|-------------|
| `text` | string | Chunk text |
| `source_type` | string | `paper_summary`, `expertise`, `soul`, `paper_abstract`, `grant`, `cv`, `web` |
| `source_id` | string | Paper ID, grant ID, or document name |
| `chunk_index` | integer | 0-based within the source |
| `section` | string \| null | Section heading |
| `score` | float | Cosine similarity, rounded to 4 decimals |
| `meta` | object | Free-form metadata |

**Status codes:** `200`, `401`, `404`, `500` (`search failed: <message>`, for
example when the profile's index is not built), `501` (the profile's backend
has no local directory, so no index can exist; the body names the operation
and a remedy).

---

## 8. Cross-profile match

### POST /match

Rank all indexed profiles against a free-text query.

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | yes | none | Free-text query |
| `k` | integer | no | `5` | Number of matches |
| `prefilter` | integer | no | `10` | Centroid-prefilter width |
| `require_topics` | list[string] \| null | no | `null` | Restrict to profiles with these topics |
| `diversify` | boolean | no | `true` | Apply MMR diversification |
| `lambda_` | float | no | `0.5` | Relevance/diversity trade-off |
| `topk_chunks` | integer | no | `5` | Chunks per profile in re-rank |
| `normalize` | boolean | no | `true` | Per-profile score calibration |
| `include_chunks` | boolean | no | `false` | Include chunk-level evidence |

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `matches` | array | Ranked results (below) |
| `ranked_profiles` | integer | How many profiles were ranked, after privacy-tier filtering |
| `total_profiles` | integer | Size of the indexed corpus the query ran against |

The two counts let a caller tell "0 of 47 ranked" from "47 of 47 ranked, none
above threshold"; an empty `matches` list alone cannot.

Each entry in `matches`:

| Field | Type | Description |
|-------|------|-------------|
| `slug` | string | Profile slug (a display handle, not a join key) |
| `name` | string | Profile name |
| `rid` | string \| null | The researcher ID: the key to map a match onto a consumer's own users |
| `orcid` | string \| null | ORCID when the profile carries one |
| `score` | float | Match score |
| `evidence` | object | See below |

Each `evidence` object:

| Field | Type | Description |
|-------|------|-------------|
| `centroid_score` | float | Query-to-centroid cosine similarity |
| `top_papers` | list[string] | Paper IDs of top-matching chunks |
| `overlapping_topics` | list[string] | Topic overlap with query |
| `top_chunks` | list | Populated only when `include_chunks=true` |

**Status codes:** `200`, `401`, `500`, `503` (no profiles indexed, search not
available, or the ranked fraction fell below a server-configured floor).

### Further ranking and graph routes

The reference implementation also serves `POST /coi/check`,
`POST /match/reviewers`, `GET /graph/neighbors/{ref}`, and
`POST /profiles/{slug}/rank-works`. They are outside this specification; the
[HTTP API reference](../rp-sdk/reference/api.md) documents them.

---

## 9. Identity resolution

### POST /identity/resolve

Resolve a person descriptor (a rid, or a free-text name) to the rid that
identifies them in this registry. Deterministic and cautious: the same
person, resolved the same way twice, converges on the same rid. When the
evidence cannot decide, the server defers rather than guessing, because an identity
system's worst failure is a silent merge. A true miss (no existing profile
plausibly matches) MINTS a new identity: this is a write route, not a query,
and is gated accordingly. It never merges two existing profiles into one.

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `rid` | string \| null | no | `null` | An ORCID, or a previously minted `local:` id (the disambiguation round-trip) |
| `name` | string \| null | no | `null` | A free-text name to resolve |
| `affiliation` | string \| null | no | `null` | Stamped on a minted stub; corroborates or vetoes a name match |
| `create_new` | boolean | no | `false` | Skip matching and mint a fresh identity for `name`, the explicit answer to a deferral whose candidates are all wrong |

At least one of `rid`/`name` MUST be supplied.

**Response 200 or 201:**

| Field | Type | Description |
|-------|------|-------------|
| `rid` | string \| null | The resolved rid, or `null` when the request defers |
| `created` | boolean | Whether this call minted a new profile |
| `confidence` | string | `"exact"` (a rid identity), `"high"` (a corroborated name match or a fresh mint), or `"low"` (a deferral) |
| `candidates` | array | Present only when non-empty: the profiles an undecidable name could mean, each `{rid, name, affiliation}` |

`201` on a true miss (a new identity was minted); `200` otherwise, including a
deferral.

A rid that a registry retired by merging two profiles about one person
resolves to its successor: the response carries the successor's `rid`, and the
server MUST NOT mint a stub for a retired rid.

**Status codes:** `200`, `201`, `400` (neither `rid` nor `name`, a malformed
rid, or an unknown `local:` rid), `401`, `403`.

---

## 10. Persona endpoints

Four endpoints generate text in the researcher's voice. Each injects the
profile's `expertise.md` and `SOUL.md` into the system prompt and grounds the
response in retrieved evidence from the profile's corpus.

**Precondition:** The profile must be persona-ready: a `full` or `deep`
profile with non-empty `expertise.md` and `SOUL.md`. A persona call against a
profile that is not persona-ready returns `409` with no LLM call. Clients
should check the `level` field from `GET /profiles` to avoid this.

### POST /profiles/{slug}/ask

Question answering in the researcher's voice.

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `question` | string | yes | none | The question |
| `k` | integer | no | `5` | Evidence chunks to retrieve |
| `model` | string \| null | no | `null` | Model override |
| `strict_corpus` | boolean | no | `false` | Refuse if top retrieval score is below threshold |
| `refusal_threshold` | float \| null | no | `null` | Score threshold (default `0.4`) |
| `history` | array \| null | no | `null` | Prior conversation turns as `{role, content}` objects |

**Response 200:** see [LLM text response](#llm-text-response).

When the refusal gate fires, the response is still `200` with `refused=true`,
the refusal message in `text`, `model="<none>"`, zeroed `usage`, and empty
`citations`.

### POST /profiles/{slug}/review

The researcher reviews supplied material.

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `material` | string | yes | none | Text to review |
| `focus` | string \| null | no | `null` | Focus hint: seeds retrieval and shapes the review |
| `k` | integer | no | `5` | Evidence chunks |
| `model` | string \| null | no | `null` | Model override |
| `strict_corpus` | boolean | no | `false` | Same refusal semantics as `ask` |
| `refusal_threshold` | float \| null | no | `null` | Same as `ask` |

**Response 200:** see [LLM text response](#llm-text-response).

### POST /profiles/{slug}/innovate

Propose novel research directions on a topic.

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `topic` | string | yes | none | Topic area |
| `n` | integer | no | `3` | Number of ideas |
| `k` | integer | no | `12` | Evidence chunks |
| `model` | string \| null | no | `null` | Model override |
| `temperature` | float | no | `0.7` | Sampling temperature |

**Response 200:** object with `items` array:

| Field | Type | Description |
|-------|------|-------------|
| `hypothesis` | string | Testable claim |
| `approach` | string | Data, method, comparison |
| `rationale` | string | Why this researcher specifically |
| `related_works` | list[string] | Citation keys (not guaranteed to match real paper IDs) |

**Status codes:** `200`, `401`, `404`, `409` (not persona-ready), `502`
(LLM failed to produce valid JSON after retries), `500`.

### POST /profiles/{slug}/riff

Generate divergent brainstorm fragments.

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `seed` | string | yes | none | Phrase or paragraph to riff on |
| `n` | integer | no | `5` | Number of riffs |
| `k` | integer | no | `4` | Evidence chunks |
| `model` | string \| null | no | `null` | Model override |
| `temperature` | float | no | `1.0` | Higher than `innovate` to encourage divergence |

**Response 200:** object with `items` array:

| Field | Type | Description |
|-------|------|-------------|
| `angle` | string | Short label |
| `text` | string | 2-5 sentences in the researcher's voice |
| `related_work` | string \| null | Optional citation key |

**Status codes:** same as `innovate`.

---

## 11. Owner edit endpoints

These endpoints let a profile's owner, or an agent acting for them, edit the
profile. They require owner-level authorization. A server MAY satisfy that with
an operator bearer token, a signed-in person's session, or an app or account
key whose [parts table](authentication.md#per-part-access) holds `write` on
every part the edit touches: each metadata field's part, `soul` for the SOUL,
`works` for a works edit. A key missing one gets `403` with the
[`insufficient_access` body](authentication.md#insufficient-access). A metadata
field no part covers, and every visibility change, is
[not delegable](authentication.md#acts-no-app-or-key-may-hold): `403` with
`detail.error` `"not_delegable"`.

### PATCH /profiles/{slug}/metadata

Patch owner-editable metadata fields.

**Editable fields:** `name`, `affiliation`, `job_title`, `field`, `subfields`,
`summary`, `expertise` (the label list), `interests`, `not_interests`,
`training`, `career`, `same_as`.

A key outside this set returns `400`. Fields like `rid`, `provenance`,
`collaborators`, and `visibility` are not editable through this endpoint.

**Optimistic concurrency:** Send `base_hash` (from the `content_hash` returned
by `GET /profiles/{slug}`) to detect concurrent edits. If the profile changed
since the hash was read, the server returns `409` with the current hash.
Omitting `base_hash` is last-writer-wins. Successful edits return the new
`content_hash`.

### PUT /profiles/{slug}/soul

Replace `personality/SOUL.md` entirely. Supports the same `base_hash`
optimistic concurrency as metadata edits (the hash spans both the document and
the SOUL).

### GET /profiles/{slug}/visibility

Report the effective visibility of the profile and of every artifact in its
manifest, with the reason for each.

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `slug` | string | |
| `rid` | string \| null | |
| `profile_visibility` | string | The document-level tier |
| `profile_floor` | string \| null | A host-imposed ceiling on this profile, or null |
| `profile_floor_reason` | string \| null | The sentence to show a human when a floor applies |
| `artifacts` | array | One entry per manifest artifact (below) |
| `counts` | object | Items each viewer class can see, e.g. `{"anonymous": 0, "lab": 12, "you": 63}` |

Each `artifacts` entry:

| Field | Type | Description |
|-------|------|-------------|
| `content_url` | string | Identifies the artifact |
| `role` | string \| null | Manifest role |
| `name` | string \| null | Display name |
| `paper_id` | string \| null | Set for per-paper artifacts |
| `declared` | string | The tier written on the manifest entry |
| `effective` | string | What governs after the profile default and the derivation rule |
| `raised_by` | list[string] | Causes holding `effective` above `declared` |
| `visible_to` | list[string] | Subset of `["anonymous", "lab", "you"]` |

### PATCH /profiles/{slug}/visibility

Set the profile default tier, per-artifact tiers, or both.

**Request body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `profile_visibility` | string \| null | no | New document-level tier |
| `artifacts` | array | no | Per-artifact changes (below) |
| `base_hash` | string \| null | no | Concurrency token, as on the metadata patch |

Each `artifacts` entry supplies exactly one selector plus the target tier:

| Field | Type | Description |
|-------|------|-------------|
| `content_url` | string \| null | Selector: one artifact |
| `paper_id` | string \| null | Selector: every artifact for one paper |
| `role` | string \| null | Selector: every artifact with this manifest role |
| `visibility` | string | REQUIRED. `public`, `internal`, or `restricted` |

Artifacts not selected keep their current tier. Supports the same `base_hash`
optimistic concurrency as metadata edits.

**Response 200:**

| Field | Type | Description |
|-------|------|-------------|
| `slug` | string | |
| `rid` | string \| null | |
| `updated` | list[string] | Names of what changed |
| `artifacts_changed` | integer | How many manifest artifacts were re-tiered |
| `content_hash` | string \| null | The hash AFTER this write, usable as the next `base_hash` |

No app or account key may call this endpoint: it answers `403` with
`detail.error` `"not_delegable"`, whatever the key's table. What the public
sees is the owner's decision alone.

---

## 12. Common response shapes

### Profile metadata

The following fields appear in profile detail responses (e.g. `GET /profiles/{slug}`):

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Required |
| `level` | string | `lite`, `full`, or `deep` |
| `rid` | string \| null | The researcher ID: an ORCID or a `local:` id. There is no `orcid` key |
| `provenance` | string \| null | Who asserted this profile and on what basis |
| `license` | string \| null | Reuse terms for the published record, an IRI |
| `url` | string \| null | The published profile URL |
| `affiliation` | string \| null | |
| `field` | string \| null | |
| `subfields` | list[string] | |
| `summary` | string \| null | |
| `job_title` | string \| null | |
| `expertise` | list[string] | Topic labels (distinct from the `expertise` markdown) |
| `interests` | list[string] | |
| `not_interests` | list[string] | Authoritative non-interests |
| `training` | list[object] | Educational history |
| `career` | list[object] | Career history |
| `collaborators` | list[string \| object] | Declared connections (see [spec](index.md#the-collaborators-array)) |
| `same_as` | list[string] | Other URLs for this person |
| `visibility` | string | Document-level privacy tier |

Additional keys round-trip unchanged.

### LLM text response

Shared by `ask` and `review`.

| Field | Type | Description |
|-------|------|-------------|
| `text` | string | The model's response |
| `citations` | list | Retrieved evidence chunks (built from retrieval, not parsed from text) |
| `model` | string | Model ID used (`"<none>"` on refusal) |
| `usage` | object | Token counts: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` |
| `request_id` | string \| null | Provider request ID |
| `refused` | boolean | Whether the `strict_corpus` gate fired |
| `refusal_reason` | string \| null | Populated only when `refused=true` |
| `grounded` | boolean | `false` if the model cited a paper ID not in the profile |

Each citation:

| Field | Type | Description |
|-------|------|-------------|
| `paper_id` | string | |
| `relevance` | float \| null | Cosine similarity from retrieval |

---

## 13. Error format

Errors use the format `{"detail": "<message>"}` with the appropriate HTTP
status code. Two exceptions: the command-line login endpoints use the OAuth
error body of [section 14.2](#142-command-line-login), and a `403` refusing an
app or key carries an object in `detail` whose `error` is
`"insufficient_access"`, `"insufficient_scope"`, or `"not_delegable"` (see
[refusal bodies](authentication.md#refusal-bodies)). Elsewhere there is no
machine-readable error code beyond the status.

| Status | Meaning |
|--------|---------|
| `400` | Bad request (invalid slug or ref, unreadable archive, unknown metadata field, unknown `?as=` value) |
| `401` | Invalid or missing bearer token |
| `403` | Hard-floor artifact (withheld from all callers), a credential that lacks the required scope, a key without `write` on a part the write touches, or an act no key may perform |
| `404` | Profile not found, artifact not in manifest, or access denied (indistinguishable) |
| `409` | Profile not persona-ready (persona endpoints), concurrent edit detected (owner endpoints), or `If-Match` mismatch (JSON upload) |
| `413` | Archive exceeds size cap |
| `422` | Request body fails schema validation (JSON upload) |
| `500` | Server error (index not built, LLM failure, etc.) |
| `501` | Search not available on this backend |
| `502` | LLM produced invalid output after retries, or an upstream service failed |
| `503` | Match unavailable (no profiles indexed), or the server reports itself degraded |

---

## 14. Management API

An OPTIONAL tier for servers that host profiles on behalf of the people they
describe. It covers three things a file server has no need of: getting a
credential onto a command line, and telling a caller what their credential is
and what it may read and write.

A server MAY implement the management tier without the dynamic API, and vice
versa. A server MAY offer further management endpoints beyond these four; they
are outside this specification.

### 14.1. The paths are normative

Unlike the `/api/v1/` prefix in [section 1](#1-url-prefix), the paths in this
section are fixed. Login endpoints live under `/api/auth/`; everything else
lives under `/api/manage/`. A client discovers whether a server offers
command-line login by calling `POST /api/auth/device` and reading the status.
A server that mounts these endpoints elsewhere is not conforming.

The fixed `/api/auth/` segment lets an operator protect every login route with
one proxy rule (for example a rate limit). A server SHOULD keep any other route
that signs a person in, such as its browser login, under the same prefix.

Absence is the signal. A server that does not implement command-line login MUST
let `POST /api/auth/device` answer `404`. A client that gets `404` there treats
the server as offering no command-line login, reports that, and MUST NOT retry.

A server MAY also publish
[RFC 8414](https://www.rfc-editor.org/rfc/rfc8414) authorization server
metadata at `/.well-known/oauth-authorization-server` naming these endpoints as
`device_authorization_endpoint` and `token_endpoint`, with
`urn:ietf:params:oauth:grant-type:device_code` in `grant_types_supported`. A
client MUST NOT depend on it: the fixed paths are authoritative, and on many
hosts an unknown path answers `200` with an HTML page, so a missing metadata
document is not a reliable absence signal.

### 14.2. Command-line login

The OAuth 2.0 Device Authorization Grant,
[RFC 8628](https://www.rfc-editor.org/rfc/rfc8628). The client cannot receive a
browser redirect, so the server issues two codes: a secret the client polls
with, and a short code the person types or confirms in a browser. The wire
format is RFC 8628's, so a stock OAuth device-flow client works unchanged. This
section fixes the endpoint paths, pins the choices RFC 8628 leaves open, and
adds a few fields. Where this section is silent, RFC 8628 and
[RFC 6749](https://www.rfc-editor.org/rfc/rfc6749) apply.

What the client receives is not a short-lived access token but a long-lived
`rpk_` API key with the `push_own` scope (see
[the key model](authentication.md#the-key-model)), returned in the standard
`access_token` field. It has no refresh token and does not expire until the
person revokes it.

```
client                          server                      person's browser
  |  POST /api/auth/device         |                                |
  |------------------------------->|                                |
  |  200 {device_code, user_code,  |                                |
  |       verification_uri,        |                                |
  |       verification_uri_complete,                                |
  |       expires_in, interval}    |                                |
  |<-------------------------------|                                |
  |  (print URI + user_code)       |    person opens the URI        |
  |                                |<-------------------------------|
  |                                |    person signs in, approves   |
  |  POST /api/auth/token          |                                |
  |------------------------------->|                                |
  |  400 {"error": "authorization_pending"}                         |
  |<-------------------------------|                                |
  |  ... wait `interval` seconds, repeat ...                        |
  |  200 {"access_token": "rpk_...", "token_type": "Bearer", ...}   |
  |<-------------------------------|                                |
```

Both endpoints are **unauthenticated**: this is how a caller with no credential
gets one. Requests use `application/x-www-form-urlencoded` bodies (RFC 8628
§3.1, §3.4). A server MAY also accept the same fields as a JSON object. All
responses are JSON and MUST carry `Cache-Control: no-store` (RFC 6749 §5.1).

#### POST /api/auth/device

Start a login (the device authorization endpoint, RFC 8628 §3.1).

**Request parameters:**

| Field | Required | Description |
|-------|----------|-------------|
| `client_id` | yes | Names the client software, e.g. `rp`. Grants nothing. A server MUST accept any non-empty value without prior registration (RFC 6749 §2.4) and MAY show it on the approval page. |
| `scope` | no | Ignored by servers that mint only `push_own`. |
| `label` | no | Extension. Display name for this machine on the approval page. A client SHOULD send the hostname. Servers SHOULD trim it and MAY truncate it. |

**Response 200** (RFC 8628 §3.2):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `device_code` | string | REQUIRED | Opaque polling secret. Never displayed to the person. |
| `user_code` | string | REQUIRED | Short code the person sees. |
| `verification_uri` | string | REQUIRED | Absolute URL of the page where the person enters `user_code`. |
| `verification_uri_complete` | string | RECOMMENDED | `verification_uri` with the code already filled in. |
| `expires_in` | integer | REQUIRED | Seconds until the request expires. |
| `interval` | integer | RECOMMENDED | Seconds a client MUST wait between polls. Default `5` when absent. |

Servers MAY include further fields; clients MUST ignore what they do not know.
A client SHOULD open `verification_uri_complete` when present, and MUST also
print `user_code` so the person can check it matches the one on the page.

The `device_code` MUST be unguessable, MUST be stored hashed rather than in the
clear, and MUST NOT be an API key: it grants nothing but the right to collect
the result of this one request. The `user_code` SHOULD avoid characters people
confuse (`I`, `O`, `0`, `1`), SHOULD be short enough to read aloud, and SHOULD
be matched ignoring case and punctuation (RFC 8628 §6.1).

The verification page MUST be served by the server. What it looks like, and how
the person signs in, is the server's business and is not specified here. A
server MUST require an authenticated person to approve, and MUST NOT approve on
the strength of the `user_code` alone: the person has to press an explicit
approve control. The page SHOULD show the requesting `label` and the
`user_code` so the person can spot a request they did not start (RFC 8628
§5.4). It MAY offer a deny control. Servers SHOULD rate-limit code entry
(RFC 8628 §5.1).

**Status codes:** `200`; `400` with an OAuth error body for a malformed request;
`404` (server does not offer command-line login); `429` or `503` when the
server cannot open another request right now (the client SHOULD retry after
`Retry-After`).

#### POST /api/auth/token

Collect the result (the token endpoint, RFC 8628 §3.4). The `device_code` is
the credential.

**Request parameters:**

| Field | Required | Description |
|-------|----------|-------------|
| `grant_type` | yes | The literal `urn:ietf:params:oauth:grant-type:device_code` |
| `device_code` | yes | From the start response |
| `client_id` | yes | The same value sent to `/api/auth/device` |

**Response 200, approved** (RFC 6749 §5.1):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `access_token` | string | REQUIRED | The minted `rpk_` key. See [the key model](authentication.md#the-key-model). |
| `token_type` | string | REQUIRED | `"Bearer"` |
| `scope` | string | RECOMMENDED | Granted scopes, space-separated, e.g. `"push_own"` |
| `orcid` | string \| null | RECOMMENDED | Extension. The approving person's identifier |
| `name` | string \| null | RECOMMENDED | Extension. The approving person's display name |
| `url` | string \| null | | Extension. The server's canonical base URL |

The response carries no `expires_in` and no `refresh_token`, because the key
does not expire on a schedule.

**The approved response is returned exactly once.** The server MUST mint the key
and invalidate the `device_code` in the same operation. A later request with
the same `device_code` MUST get `expired_token`, not the key again.

**Response 400, not approved** (RFC 8628 §3.5) carries
`{"error": "<code>", "error_description": "<optional text>"}`:

| `error` | Meaning | Client action |
|---------|---------|---------------|
| `authorization_pending` | Not yet approved | Wait `interval` seconds, poll again |
| `slow_down` | Polling too fast | Add 5 seconds to `interval` for all later polls, then poll again |
| `access_denied` | The person refused, or the approving identity has been removed | Stop; report that the login was refused |
| `expired_token` | The `device_code` is unknown, expired, or already collected | Stop; tell the person to log in again |
| `invalid_request`, `unsupported_grant_type`, `invalid_grant` | Malformed request (RFC 6749 §5.2) | Stop; report the error |

A server MUST answer unknown, expired, and already-collected codes with the same
`expired_token` response, so the three are indistinguishable. A client MUST
treat `invalid_grant` like `expired_token`, since stock OAuth servers use it for
the same cases.

A server that throttles a polling client SHOULD answer `slow_down` rather than
`429`. A client MUST treat `429` like `slow_down`. A client MUST treat any
other `error` value it does not recognize as fatal, and MUST stop polling once
`expires_in` has passed.

Servers SHOULD purge expired requests.

These two endpoints do not use the `{"detail": ...}` body of
[section 13](#13-error-format); they use the OAuth error body above.

### 14.3. Identity echo

Two endpoints answer "what is this credential". They are split by key family
(see [the key model](authentication.md#the-key-model)) because the answers have
different shapes.

#### GET /api/manage/whoami

Describe an app-family (`rpk_`) key. Requires
`Authorization: Bearer <key>`.

**Response 200:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `consumer` | string | REQUIRED | Stable name of the application or session holding the key |
| `scopes` | list[string] | REQUIRED | Granted app scopes, sorted |
| `owner` | object \| null | REQUIRED | `{orcid, name}` of the person the key was minted for, or `null` for an application key with no owning person |
| `profiles` | array | REQUIRED | Profiles this key may write. Empty when `owner` is `null`. |

Each `profiles` entry:

| Field | Type | Description |
|-------|------|-------------|
| `slug` | string | |
| `rid` | string | |
| `role` | string | `owner` or `editor` |

**Status codes:** `200`; `400` when the credential belongs to the account family
(the response SHOULD name the correct endpoint); `401` when the header is
missing, malformed, or the key is unknown, revoked, or expired.

#### GET /api/manage/agent/whoami

Describe an account-family (`rpa_`) key: everything it may read and write.
Requires `Authorization: Bearer <key>`. A key is expected to call this at the
start of every session, before attempting any write, and again after a
refusal.

**Response 200:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `principal` | object | REQUIRED | The key itself (below) |
| `owner` | object \| null | REQUIRED | `{orcid, name}` of the person whose account the key acts for |
| `profiles` | array | REQUIRED | Every profile the account reaches (below) |
| `parts` | object | REQUIRED | The key's [parts table](authentication.md#per-part-access): `{part: "read" \| "write"}`. A part left out is `none` |
| `replace_profiles` | boolean | REQUIRED | Whether the key holds [Replace whole profiles](authentication.md#replace-whole-profiles) |
| `never_delegable` | array | RECOMMENDED | [Acts no app or key may hold](authentication.md#acts-no-app-or-key-may-hold), as `{act, why}` objects, worded accurately for this key |

`principal`:

| Field | Type | Description |
|-------|------|-------------|
| `kind` | string | `consumer` |
| `label` | string \| null | Human label given when the key was minted |
| `handle` | string | Stable machine identifier for this key |
| `created_at` | string \| null | ISO 8601 |
| `last_used_at` | string \| null | ISO 8601 |
| `expires_at` | string \| null | ISO 8601, or null for no expiry |

A server MAY add fields (for example numeric ids).

Each `profiles` entry:

| Field | Type | Description |
|-------|------|-------------|
| `slug` | string \| null | |
| `rid` | string | |
| `role` | string | How the account reaches it: `owner`, `co-owner`, `editor`, or `permission` (a person granted the account a permission on their data) |
| `writes` | list[string] | The parts the key may write here: the table's `write` parts where the account may write, else empty |
| `published` | boolean | Present when the server tracks a publication decision |

The table is the whole of the key's authority. It is not stored in the key and
the account holder may change it at any time, so a client MUST NOT cache it
across sessions.

**Status codes:** `200`; `400` when the credential belongs to the app family;
`401` when the header is missing or malformed, or the key is unknown, revoked,
or expired.
