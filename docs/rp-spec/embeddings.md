# Embeddings

Researcher Profiles can include pre-computed vector embeddings for semantic search (finding
profiles by meaning rather than keywords). A profile with embeddings is called
a "Searchable Profile."

This document defines the embedding index format: the files in `embeddings/`,
how they're structured, and how consumers verify compatibility.

---

## 1. Why flat blobs

Vector search over HTTP means reading the entire vector set. SQLite is
optimized for random row access, which is the wrong pattern here. Flat blobs
are one contiguous read per model.

`.cache/embeddings.sqlite` is a derived local index at tier `restricted`.
The servable form lives in `embeddings/`.

---

## 2. Two embedding surfaces

| Surface | Location | Purpose |
|---------|----------|---------|
| Per-profile chunks | `<slug>/embeddings/` | One vector per text chunk |
| Collection centroids | `collection/embeddings/` | One aggregate vector per profile |

Both use the `index.json` and blob format below. They differ in these fields:

| Field | Per-profile chunks | Collection centroids |
|-------|--------------------|--------------------|
| `normalized` | `false` | `true` |
| `row_key` | `chunk_index` | `slug` |
| `rows` | integer chunk indexes | profile slugs |
| chunks file (section 5) | present | absent |
| `probe` | required | present when the contributing profiles supply one |

---

## 3. `embeddings/index.json`

| Field | Type | Description |
|-------|------|-------------|
| `backend_spec` | string | Model identifier (e.g. `st:all-MiniLM-L6-v2`) |
| `file` | string | Relative path to blob (e.g. `st-all-minilm-l6-v2.bin`) |
| `dtype` | string | `float32` |
| `byte_order` | string | `little` |
| `dim` | integer | Vector dimensionality |
| `count` | integer | Number of rows |
| `layout` | string | `row_major` |
| `normalized` | boolean | Whether rows are L2-normalized |
| `metric` | string | `cosine` |
| `row_key` | string | `chunk_index` or `slug` |
| `rows` | array | Row manifest in blob order |
| `sha256` | string | Hex digest of blob |
| `probe` | object | `{text, vector}` for model verification |

The blob filename is a lowercased slug of `backend_spec` with `:` and `/`
replaced by `-`. Consumers MUST resolve the blob by the `file` field rather
than rebuilding the slug.

### 3.1 Probe

Every per-profile index MUST include a probe. A collection centroid index
includes one when the contributing profiles provide it. A consumer embeds `probe.text` with its own
model and computes cosine similarity against `probe.vector`:
- Above 0.99: verified
- 0.9 to 0.99: degraded (usable but flagged)
- Below 0.9: incompatible

---

## 4. Blob format

Raw, headerless binary:
- Layout: row-major, contiguous
- Size: exactly `count * dim * 4` bytes
- Type: IEEE 754 float32
- Byte order: little-endian

No header, no padding, no magic bytes.

---

## 5. Chunk metadata

Per-profile indexes carry a chunks file next to the blob: the `file` path with
`.bin` replaced by `.chunks.json` (for example `st-all-minilm-l6-v2.chunks.json`).
It is a JSON array with one entry per row:

| Field | Type | Description |
|-------|------|-------------|
| `source_type` | string | `expertise`, `soul`, `paper_summary`, `paper_abstract`, `grant`, `cv`, `web` |
| `source_id` | string | Document identifier |
| `chunk_index` | integer | Zero-based index within source |
| `section` | string/null | Section heading |
| `char_count` | integer | Chunk text length |

The file does not contain the chunk text; fetch the source document to display a hit.

---

## 6. Privacy

Chunk tiers follow the derivation rule. A chunk from a `restricted` source is
itself `restricted` and MUST NOT appear in a public blob. `cv`, `web`, and
`grant` chunks resolve to `restricted` by their role default and are dropped
from a public blob accordingly.

Collection centroids MAY be computed over all chunks (including restricted)
because a single averaged vector cannot reconstruct individual inputs.

---

## 7. Cross-model comparison

Consumers MUST compare `backend_spec` strings for exact equality before
computing similarity. They MUST refuse to compute when the strings differ,
because cosine similarity across models is meaningless.
