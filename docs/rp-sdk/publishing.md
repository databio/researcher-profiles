# How to host a profile

A researcher profile is a directory of static files. Hosting one means serving
that directory over HTTP from S3, GitHub Pages, nginx, or Cloudflare. There is
no application to run and no database to provision.

!!! info "Prerequisites"
    - A built profile directory with `profile.jsonld`
    - A static file host

## What to deploy

Deploying is two steps: export, then upload.

**1. Export the folder for one audience.** `rp publish` writes a folder that
holds exactly what one audience may see (see
[Privacy tiers](../rp-spec/privacy.md)):

```bash
rp publish ~/researcher-profiles --out ./_site                # the open web (--who public)
rp publish ~/researcher-profiles --who limited --out ./_lab   # a mirror for granted readers
```

Each profile lands in `profiles/<slug>/` with its `profile.jsonld` trimmed to
that audience, only the files that audience may read, and its page and
embeddings rebuilt for that audience. The collection files (`index.json`,
`by-rid.json`, `SKILL.md`, ...) sit at the top. Run it again into the same
folder to refresh it; files that are no longer allowed are removed. Add
`--dry-run` to see what ships, what is held back and why, and what a real run
would remove.

The folder also holds `.rp-publish.json`, which records the audience and the
file list and is uploaded with the rest; it holds nothing beyond paths already
in the tree.

**2. Upload the folder as is.** Nothing in it needs filtering, so any sync
tool works:

```bash
aws s3 sync ./_site s3://my-bucket/ --delete                       # Amazon S3
aws s3 sync ./_site s3://my-bucket/ --delete \
  --endpoint-url https://<account>.r2.cloudflarestorage.com        # Cloudflare R2
npx wrangler deploy                                                # Cloudflare Workers (assets dir = ./_site)
rclone sync ./_site remote:my-bucket                               # anything rclone supports
```

For GitHub Pages, commit the folder's contents to the branch Pages serves (see
the note below about headers).

Never upload a profile folder directly. Its `profile.jsonld` holds every
section the owner wrote, including ones marked `limited` or `private`, and its
`sources/` holds private files such as full text and the CV.

## Host requirements

A conformant host must:

1. Serve `Access-Control-Allow-Origin: *` on all responses (including `OPTIONS`)
2. Serve `.jsonld` files as `application/ld+json`
3. Return a real 404 for missing paths (not a 200 HTML shell)
4. Support `Range` requests for embedding blobs (`.bin`)

## Host-specific configuration

### Cloudflare Workers Static Assets (recommended)

A Worker overlay in front of the `ASSETS` binding sets headers on every response.
Two settings in `wrangler.toml` matter:

- `run_worker_first = true`: without it the Worker only runs on asset misses
- `not_found_handling` must be `"404-page"`, not `"single-page-application"`

### GitHub Pages: not conformant on its own

GitHub Pages cannot set custom headers (so no CORS) and serves `.jsonld` as
`application/octet-stream`. Use it only behind a proxy (Cloudflare, or a Worker that fetches and overrides headers).

### Amazon S3 + CloudFront

Set `Content-Type` per object at upload; add CORS via CloudFront response headers policy:

```bash
aws s3 cp profile.jsonld s3://bucket/p/slug/profile.jsonld \
  --content-type "application/ld+json; charset=utf-8"
```

### nginx

```nginx
location / {
    add_header Access-Control-Allow-Origin *;
    add_header Access-Control-Allow-Methods "GET, HEAD, OPTIONS";
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
    header Access-Control-Allow-Methods "GET, HEAD, OPTIONS"
    root * /var/www/profiles
    file_server

    @jsonld path *.jsonld
    header @jsonld Content-Type "application/ld+json; charset=utf-8"
}
```

## Setting the profile URL

Once deployed, set `url` in `profile.jsonld` to the published location. For
ORCID-verified profiles, this URL must appear in the ORCID record's website list.

Set `license` to a licence IRI. A profile with no declared reuse terms leaves
consumers guessing.

## Validating your deployment

The explorer app (`rp-browser/`) includes a conformance validator. Paste your
profile URL and it checks the schema, CORS headers, and content types. This
catches silent failures that make a profile unreachable to
browser-based consumers.

See also: [How to validate a profile](how-to/validate-a-profile.md)
