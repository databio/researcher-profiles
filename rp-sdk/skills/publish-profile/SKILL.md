---
name: publish-researcher-profile
description: Use when the user asks you to publish, deploy, or host researcher profiles as a static site (on Cloudflare, S3, nginx, or GitHub Pages) using the researcher-profiles package.
user_invocable: true
---

# Publishing researcher profiles

## What publishing means

Publishing is two steps: export, then upload. The `researcher-profiles`
package (installed from a checkout of the researcher-profiles monorepo; CLI
`rp`) does the first:

- `rp publish <profiles_root> --out <dir> --who <tier>` writes the folder one
  audience may see. Each visible profile lands in `profiles/<slug>/` with its
  `profile.jsonld` trimmed to that audience, only the files that audience may
  read, and its `index.html` and `embeddings/` rebuilt for that audience. The
  collection files (`index.json`, `index.jsonld`, `by-rid.json`, `SKILL.md`,
  `style.css`, `sitemap.xml`, `robots.txt`, hosting configs, `.well-known/`)
  sit at the top.

The second step is any sync tool (`aws s3 sync`, `rclone`, `wrangler`,
`rsync`) copying that folder as is. Never upload a profile folder directly:
its `profile.jsonld` holds every section, whatever its tier. This skill assumes
the profile directories already exist.

## Step 1: Pre-flight, per profile

- `rp validate <profiles_dir>/<slug>` passes and
  `rp manifest <profiles_dir>/<slug> --check` agrees with the directory.
- `url` is set to the published location; `@id` is the researcher's ORCID
  IRI when one exists. The deployment URL is a *location* that can move,
  not an *identifier*.
- `license` is set. A published profile with no reuse terms makes every
  consumer guess, and the conservative guess is "do not use it".
- `provenance` is the honest label. `orcid_verified` asserts the ORCID
  record's website list contains this profile's `url`. Do not claim it
  unless that round-trip is real.

**Privacy tiers.** What may leave the machine is declared *in the profile*.
Each artifact (and the profile as a whole) has a `public` / `limited` /
`private` tier; `public` is the default for authored content. Hold a whole
profile back by setting the document's `visibility` to `limited` or
`private`. Inline sections (summary, contact email, methods, ...) carry their
own tiers too. `rp publish` applies all of them when it writes the export.

Full text of copyrighted papers (`sources/papers/`), `sources/cv.md`, and
scraped `sources/web/` are `private` by default and never reach the open
web. `.cache/` (serve-time derived caches) and `.keys/` are always excluded
regardless of visibility; build bookkeeping lives outside the profile
entirely, at `$RESEARCHER_PROFILES_ROOT/.build/<slug>/meta/`.

## Step 2: Export and upload

```bash
# 1. Preview what ships and what is held back, and why
rp publish <profiles_dir> --out site/ --dry-run

# 2. Write the folder for the open web
rp publish <profiles_dir> --out site/ --base-url https://profiles.example.org

# 3. Upload it as is
aws s3 sync site/ s3://my-bucket/ --delete
```

Useful `rp publish` flags:

| Flag | Effect |
|---|---|
| `--who <tier>` | The audience: `public` (default), `limited`, or `private` |
| `--base-url <url>` | Base URL for canonical links, `sitemap.xml` and `robots.txt` |
| `--no-index` | Add noindex directives (for a personal/staging site) |
| `--now <ISO8601>` | Pin timestamps for deterministic output |

A static host serves one audience. A mirror for `limited` readers is a second
export (`--who limited`) to a second, access-controlled host; it gets noindex
pages and a disallow-all `robots.txt` automatically. A `--who private` export
holds what only the owner may see and must never be uploaded where others can
read it. Re-running into the same folder refreshes it and removes files that
are no longer allowed.

## Step 3: Choose a host that meets the requirements

A conformant host must:

1. Serve `Access-Control-Allow-Origin: *` on every response (including
   preflight). Without CORS, browser-based viewers cannot fetch the profile.
   The failure is invisible to the publisher.
2. Serve `.jsonld` as `Content-Type: application/ld+json`. Crawlers that
   receive `application/octet-stream` skip the document as linked data.
3. Return a real **404** for missing paths, not a 200 HTML shell.
4. Support `Range` requests for embedding blobs (`.bin`).

### Cloudflare Workers Static Assets (recommended)

The reference deployment. A small Worker overlay sets headers on every
response. Two settings are load-bearing in `wrangler.toml`:

- `run_worker_first = true`: without it the Worker only runs on asset
  misses and headers are never applied to static files.
- `not_found_handling = "404-page"`, not `"single-page-application"`:
  an SPA fallback serves HTML where a manifest should 404.

Write the overlay Worker yourself. It is about forty lines: fetch from
the `ASSETS` binding, then set `Access-Control-Allow-Origin: *` and a correct
`Content-Type` per extension on the way out. Then `wrangler deploy`.

(A static host is a conformant way to publish your own profiles, so the rest
of this section stands whether or not a live origin is also involved.)

### GitHub Pages: not conformant on its own

GitHub Pages cannot set custom response headers (no CORS) and serves
`.jsonld` as `application/octet-stream`. Use it only with a proxy in front,
e.g. Cloudflare, or a Worker that fetches from Pages and overrides headers.
Do not tell a user a bare GitHub Pages deploy is conformant; it is not.

### S3 + CloudFront

Set `Content-Type` per object at upload:

```bash
aws s3 cp profile.jsonld s3://bucket/p/slug/profile.jsonld \
  --content-type "application/ld+json; charset=utf-8"
```

Add CORS via a CloudFront response headers policy
(`Access-Control-Allow-Origin: *`).

### nginx

```nginx
location / {
    add_header Access-Control-Allow-Origin *;
    add_header Access-Control-Expose-Headers "Content-Length, Content-Range";
}
location ~* \.jsonld$ {
    types { }
    default_type application/ld+json;
    add_header Access-Control-Allow-Origin *;
}
```

### Caddy

```
profiles.example.org {
    header Access-Control-Allow-Origin *
    root * /var/www/profiles
    file_server
    @jsonld path *.jsonld
    header @jsonld Content-Type "application/ld+json; charset=utf-8"
}
```

## Step 4: Verify the deployment

Spot-check from the command line:

```bash
curl -sI https://profiles.example.org/<slug>/profile.jsonld \
  | grep -i 'content-type\|access-control'
curl -s -o /dev/null -w '%{http_code}\n' https://profiles.example.org/definitely-missing
```

Expect `application/ld+json`, `Access-Control-Allow-Origin: *`, and `404`.

Then run the browser-side conformance validator: open the explorer app's
**Validate** tab and paste the profile URL. It fetches from the browser
(the same origin position as any real viewer) and reports broken CORS,
wrong content types, and manifest/schema errors.

## Checklist

- [ ] `profile.jsonld` present; `conformsTo`, `provenance`, `license` set
- [ ] `url` and `@id` point at the right places
- [ ] manifest matches disk (`rp manifest <profile> --check`)
- [ ] deployed from `rp publish` output, never from a profile folder
- [ ] no `private` artifact served: `sources/papers/` full text,
      `sources/cv.md`, `sources/web/`, `.cache/`, `.keys/` absent from the host
- [ ] `.jsonld` served as `application/ld+json`
- [ ] `Access-Control-Allow-Origin: *` on every response
- [ ] missing paths return a real 404
