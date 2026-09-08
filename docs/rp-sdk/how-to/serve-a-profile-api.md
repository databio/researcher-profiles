# How to serve a profile API

The SDK includes a FastAPI server for serving profiles over HTTP. Use it when
you need programmatic access, cross-profile search/match, or the persona
endpoints.

!!! info "Prerequisites"
    - `researcher-profiles[api,vectors,st]`
    - A directory of profiles (one subdirectory per profile, each with `profile.jsonld`)

To call the server from Python or curl, see
[How to access profiles over HTTP](access-profiles-over-http.md).

## Start the server

The CLI entry point serves a profiles directory:

```bash
python -m researcher_profiles.api \
    --profiles-dir /path/to/profiles \
    --host 127.0.0.1 \
    --port 8109
```

`--profiles-dir` defaults to the `RESEARCHER_PROFILES_ROOT` environment variable,
`--host` to `RESEARCHER_PROFILES_HOST` (default `127.0.0.1`), and `--port` to
`RESEARCHER_PROFILES_PORT` (default `8109`). See the
[API reference](../reference/api.md#running-the-server) for all environment
variables.

## Set a bearer token

Set `RESEARCHER_PROFILES_TOKEN` to require `Authorization: Bearer <token>` on
the write, search, match, persona, and edit routes. The read routes stay open
and instead project each response through the caller's viewer tier, which the
token widens to `restricted`:

```bash
export RESEARCHER_PROFILES_TOKEN="a-long-random-string"
python -m researcher_profiles.api --profiles-dir /path/to/profiles
```

If the token is unset or empty, the server runs in open mode and logs a
warning; every request is accepted. Do not expose an open-mode server to
untrusted networks. The `/health` route is always unauthenticated.

## Push a built profile to a server

Profiles are built locally by whatever process produces a conforming profile
directory. They are then indexed (`rp index <dir>`, so `/match` can rank them)
and pushed to the serving instance. The server never builds profiles itself.

```bash
rp push /path/to/profiles/jane-doe \
    --url http://127.0.0.1:8109
```

The slug defaults to the directory name (`--slug` overrides); the token
defaults to `RESEARCHER_PROFILES_TOKEN`. Or from Python:

```python
from researcher_profiles.client import push_profile

summary = push_profile("http://127.0.0.1:8109", "/path/to/profiles/jane-doe")
print(summary)  # {"slug": ..., "rid": ..., "name": ..., "level": ..., "indexed": ...}
```

The push builds the archive with `build_profile_archive`, the whole-record
transfer builder. Only the documented profile members ship. Dotfiles and
non-spec build residue (for example a `sources/html/` scrape cache) are
dropped. By default the push is lite: `sources/papers/`
(the extracted full paper text) is omitted. Pass `--include-fulltext` (CLI) or
`include_fulltext=True` (`push_profile`) to carry paper text. The server
strips fulltext on ingest unless it runs with
`RESEARCHER_PROFILES_ACCEPT_FULLTEXT=true`. A spec-clean profile is text-only
and a few MB even with fulltext included, so either way it sits comfortably
under the 50 MB cap; there is no need to truncate a profile to fit. A profile
that hits the cap is signalling accidental non-text content, not normal
growth.

The server's `GET .../archive` route calls `build_viewer_archive`, not
`build_profile_archive`, so it projects the profile through the reader's
privacy tier instead of handing over the full record. Keeping the two as
separate functions means the tier cannot be bypassed by passing the wrong one.

The server swaps the profile into place atomically. The pushed profile is
immediately visible to `GET /api/v1/profiles` with no server restart. If
`.cache/embeddings.sqlite` was included it is also visible to `/api/v1/match`.
See the
[API reference](../reference/api.md#put-apiv1profilesslug) for validation
rules and the size cap.

