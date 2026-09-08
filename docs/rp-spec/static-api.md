# Static API

A Researcher Profile is a set of files, so for many uses, all that is needed
to serve them is a plain file server. For advanced cases, such as server-side
searching, further capabilities can be added through a programmatic API.
This gives the Researcher Profile HTTP interface three tiers:

1. Static API (this document): REQUIRED. The HTTP contract for serving
   profile files. Any file server (S3, R2, GitHub Pages) can fulfill this.
2. [Dynamic API](dynamic-api.md): OPTIONAL. Additional endpoints for advanced use, like
   listing, searching, matching, and interacting with profiles programmatically.
3. [Management API](dynamic-api.md#14-management-api): OPTIONAL. Endpoints for
   obtaining and introspecting credentials: command-line login, identity echo,
   and the scope catalog. A server that hosts profiles on behalf of the people
   they describe needs these; a file server does not.

All servers MUST satisfy the static API. The dynamic and management tiers are
independent of each other: a server MAY implement either, both, or neither.
This division lets the basic HTTP service be met cheaply, without requiring
any server setup.

---

## 1. Transport

Servers providing the static API MUST meet these transport requirements:

- SHOULD serve over HTTPS in production
- MUST support HTTP/1.1 or later
- MUST support GET and HEAD methods on all artifacts

---

## 2. CORS

Servers SHOULD include the following header on every artifact response so that
browser apps can fetch cross-origin data:

```
Access-Control-Allow-Origin: *
```

Servers that require [authentication](authentication.md) MUST instead set an
appropriate `Access-Control-Allow-Origin` (matching the requesting origin) with
`Access-Control-Allow-Credentials: true`, since the wildcard `*` is incompatible
with credentialed requests.

For range requests, servers SHOULD also expose:
```
Access-Control-Expose-Headers: Content-Length, Content-Range, ETag
```

---

## 3. Content types

Servers SHOULD serve artifacts with the `Content-Type` that matches their file
extension. Consumers use this header to decide how to parse a response, so an
incorrect type (e.g. `application/octet-stream` for a JSON-LD manifest) may
cause failures.

| Extension | Content-Type |
|-----------|--------------|
| `.jsonld` | `application/ld+json` |
| `.json` | `application/json` |
| `.md` | `text/markdown; charset=utf-8` |
| `.bin` | `application/octet-stream` |
| `.html` | `text/html; charset=utf-8` |

Note: some platforms (notably S3 and R2) serve `.jsonld` and `.md` as
`application/octet-stream` by default. See
[Appendix A](#appendix-a-content-type-configuration-by-platform) for
per-platform configuration instructions.

---

## 4. Caching

- SHOULD include `ETag` on all responses
- SHOULD include `Last-Modified`

Recommended `Cache-Control`:

| Artifact | Value |
|----------|-------|
| `profile.jsonld` | `public, max-age=300` |
| Other | `public, max-age=3600` |

Servers that implement [viewer tiers](authentication.md#viewer-tiers) SHOULD
use `private` instead of `public` for tier-restricted responses, so shared
caches (CDNs, proxies) do not serve a restricted artifact to a lower-tier
caller.

---

## 5. Range requests

Servers MUST support byte-range requests for `embeddings/` files at Searchable
conformance level. (Trivially satisfied by S3, R2, GCS.)

---

## 6. Status codes

Servers MUST return accurate HTTP status codes so consumers can distinguish a
present artifact from a missing one:

- `200`: artifact exists and is returned
- `404`: artifact does not exist
- `3xx`: redirect (see [§9 Consumer requirements](#9-consumer-requirements))

A `200` with an HTML error page (e.g. an SPA catch-all) instead of the
requested artifact is a conformance violation. Consumers cannot tell it apart
from a real response.

---

## 7. The HTML landing page

`<base>/index.html` is the human-readable entry point. When present, it MUST
contain:

- A `<script type="application/ld+json">` block with the same content as
  `profile.jsonld`
- `<link rel="alternate" type="application/ld+json" href="profile.jsonld">`
- `<link rel="canonical" href="...">` (explicit, including `index.html`)

This lets search crawlers extract structured data from the HTML page and lets
browser apps discover the JSON-LD manifest via the `<link>` tag.

---

## 8. Profile lists

A profile list is a JSON document that enumerates profile base URLs. Any person
or organization MAY host one. There is no central registry; each host that
serves the API is itself a registry.

```json
{
  "rp:profileList": "0.1",
  "name": "Example Lab",
  "url": "https://example.org/profiles.json",
  "updated": "2026-07-29T00:00:00Z",
  "profiles": [
    "https://example.org/p/jane-doe",
    {"url": "https://other.example.org/me", "name": "John Smith"},
    {"list": "https://partner.example.org/profiles.json"}
  ]
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `rp:profileList` | REQUIRED | Format version string |
| `name` | RECOMMENDED | Human-readable label for this list |
| `url` | RECOMMENDED | Canonical URL of this list |
| `updated` | RECOMMENDED | ISO 8601 timestamp of last update |
| `profiles` | REQUIRED | Array of profile entries |

Each entry in `profiles` is one of:

- A string: the profile's base URL
- An object with `url` (base URL) and optional `name` (display hint, not
  authoritative)
- An object with `list`: a nested profile list URL

Object entries MAY include additional fields beyond `url` and `name`.
The [dynamic API](dynamic-api.md#get-profiles) uses this same format with
enriched entries (e.g. `slug`, `level`, `paper_count`). Consumers MUST
ignore fields they do not recognize.

The format above is for a hand-authored list or one a dynamic server returns.
It is not what `rp site` writes; that tool publishes a different, narrower set
of files, listed below.

### What `rp site` actually publishes

| File | Real shape |
|---|---|
| `index.json` | A bare JSON array of relative profile base paths, `["profiles/<slug>/", ...]`, sorted. No top-level keys. |
| `by-rid.json` | A flat object mapping rid to `profiles/<slug>/profile.jsonld`. |
| `index.jsonld` | A `DataCatalog` (`@context`, `@type`, `name`, `conformsTo`, `dataset`; plus `@id` and `url` only when `base_url` is given). |
| `.well-known/researcher-profiles.json` | `{"version": 1, "profiles_index": "index.json", "by_rid": "by-rid.json"}` plus `base_url` when given. This is the discovery entry point. |

`rp site` also writes `style.css`, `SKILL.md`, `index.html`, `_headers`,
`collection.jsonld`, `collection/topics.json`, `context/v1.jsonld`, and, when
`base_url` is set, `sitemap.xml` and `robots.txt`. When at least one profile
is Searchable it also writes `collection/embeddings/index.json` and
`collection/embeddings/<backend>.bin` (see [Embeddings](embeddings.md)).

`collection.jsonld` is a separate document from the discovery index. The
discovery entry point is `.well-known/researcher-profiles.json`, which points
at `index.json` and `by-rid.json`; those name the profiles a consumer walks.
`collection.jsonld` is the ranking bundle: one card per profile plus the
stacked centroids, for a client that ranks the whole collection at once.
Nothing in `.well-known` points at `collection.jsonld`; a consumer fetches it
by its known path.

---

## 9. Consumer requirements

Consumers (clients, crawlers, UIs) MUST or SHOULD behave as follows.

A consumer resolves a profile URL to a `profile.jsonld` document:

1. If the URL ends in `/profile.jsonld`, fetch it directly
2. Otherwise: strip any trailing slash, append `/profile.jsonld`, fetch
3. If `404`: stop; it is not a conforming profile

Consumers MAY attempt a fallback: fetch the original URL as HTML and look for
`<link rel="alternate" type="application/ld+json">` to discover the manifest.

Beyond resolution, consumers:

- MAY refuse plain HTTP connections
- SHOULD follow HTTP redirects (up to 5 hops)
- A `200` with `Content-Type: text/html` instead of a JSON-LD body is a
  failure, not a manifest
- SHOULD use conditional requests (`If-None-Match` / `If-Modified-Since`)
  when re-fetching to avoid unnecessary data transfer
- MUST NOT construct artifact URLs by convention; discover them via the
  manifest's declared fields
- SHOULD cap recursion at 3 levels when following nested lists
- SHOULD de-duplicate by normalized URL

---

## Appendix A. Content-type configuration by platform

The two extensions that cause the most trouble are `.jsonld`
(`application/ld+json`) and `.md` (`text/markdown`). Most platforms default
to `application/octet-stream` for one or both, which can break consumers
that rely on the `Content-Type` header to parse the response.

### GitHub Pages

GitHub Pages auto-detects content types from a large MIME database.
`.jsonld`, `.md`, and `.json` are all served with the correct type. No
configuration needed.

GitHub Pages does not support custom headers (`_headers`, `.htaccess`), so
there is no override mechanism, but for profile artifacts, the defaults
are correct.

### AWS S3

S3 does not recognize `.jsonld` or `.md` and defaults both to
`application/octet-stream`. You must set the content type at upload time.

**Per-object override (AWS CLI):**

```bash
aws s3 cp profile.jsonld s3://bucket/path/ \
  --content-type "application/ld+json"

aws s3 cp personality/expertise.md s3://bucket/path/personality/ \
  --content-type "text/markdown; charset=utf-8"
```

**Scripted upload (all artifacts):**

```bash
declare -A CT=(
  [jsonld]="application/ld+json"
  [json]="application/json"
  [md]="text/markdown; charset=utf-8"
  [html]="text/html; charset=utf-8"
  [bin]="application/octet-stream"
)

for f in $(find profile/ -type f); do
  ext="${f##*.}"
  aws s3 cp "$f" "s3://bucket/path/$f" \
    --content-type "${CT[$ext]:-application/octet-stream}"
done
```

There is no bucket-wide extension-to-type mapping. Content type is metadata
on each object, set at upload time (via CLI flag, SDK parameter, or the S3
console metadata editor).

### Cloudflare R2

R2 is S3-compatible and has the same defaults: unknown extensions get
`application/octet-stream`. The fix is the same: set `Content-Type` in the
`PutObject` request.

```bash
wrangler r2 object put bucket/path/profile.jsonld \
  --file profile.jsonld \
  --content-type "application/ld+json"
```

Wrangler auto-detects MIME types for common extensions when uploading, but
`.jsonld` may not be in its map. Always set `--content-type` explicitly for
`.jsonld` and `.md` files.

### Cloudflare Workers

Workers are fully programmatic: you control the `Content-Type` in your
response constructor:

```js
return new Response(body, {
  headers: { "Content-Type": "application/ld+json" }
});
```

For serving static assets from R2 via a Worker, look up the correct type
from the file extension before returning the response.

### Cloudflare Pages

Cloudflare Pages auto-detects MIME types on upload via Wrangler. For
extensions it doesn't recognize, add a `_headers` file in the output
directory:

```
/*.jsonld
  Content-Type: application/ld+json

/*.md
  Content-Type: text/markdown; charset=utf-8
```

### Netlify

Netlify uses a large MIME database and typically handles `.jsonld`
and `.md` correctly. To override or guarantee correct types, add a
`_headers` file in the publish directory:

```
/*.jsonld
  Content-Type: application/ld+json

/*.md
  Content-Type: text/markdown; charset=utf-8
```

Or equivalently in `netlify.toml`:

```toml
[[headers]]
  for = "/*.jsonld"
  [headers.values]
    Content-Type = "application/ld+json"

[[headers]]
  for = "/*.md"
  [headers.values]
    Content-Type = "text/markdown; charset=utf-8"
```

### Vercel

Vercel auto-detects common types but `.jsonld` may not be in its map.
Override in `vercel.json`:

```json
{
  "headers": [
    {
      "source": "/(.*)\\.jsonld",
      "headers": [
        { "key": "Content-Type", "value": "application/ld+json" }
      ]
    },
    {
      "source": "/(.*)\\.md",
      "headers": [
        { "key": "Content-Type", "value": "text/markdown; charset=utf-8" }
      ]
    }
  ]
}
```
