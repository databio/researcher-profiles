# How to access profiles over HTTP

The `[client]` extra provides `ApiArtifactStorage`. `ResearcherProfile.from_api` wraps
it in an ordinary `ResearcherProfile` that behaves like a local profile but
fetches its data from a server.

!!! info "Prerequisites"
    - `researcher-profiles[client]`
    - A running profile server (see [How to serve a profile API](serve-a-profile-api.md))

## Call the server directly

List profiles, then fetch one:

```bash
curl -H "Authorization: Bearer $RESEARCHER_PROFILES_TOKEN" \
    http://127.0.0.1:8109/api/v1/profiles

curl -H "Authorization: Bearer $RESEARCHER_PROFILES_TOKEN" \
    http://127.0.0.1:8109/api/v1/profiles/jane-doe
```

`GET /api/v1/profiles` answers one page, `{results, pagination, note}`, in
slug order. Ask for the next page with `?page=1` (pages count from 0), and
stop when `page + 1` reaches `pagination.total_pages`. `page_size` defaults to
100; a larger one is lowered to 1000, and `note` says so. Filter with `rid`
(repeat it for several), `clinical` or `affiliation`; an unknown parameter is
a `400`:

```bash
curl "http://127.0.0.1:8109/api/v1/profiles?clinical=true&page_size=50"
```

To read one profile by ORCID, call `GET /api/v1/profiles/{orcid}` directly;
do not list and scan.

The papers list, `GET /api/v1/profiles/{slug}/papers`, pages by token
instead: pass `pagination.next_page_token` back as `page_token` until it is
`null`.

Both `GET /api/v1/profiles` (each row in `results`) and `GET /api/v1/profiles/{slug}`
(under `metadata`) include a `level` field, one of `lite`, `full`, or `deep` (see
[profile depth levels](../profile-format.md#profile-depth-levels)). Clients can
filter or branch on the tier: for example, skip persona calls for a `lite` profile,
which is not persona-ready.

## Use the Python client

`ResearcherProfile.from_api(...)` returns a `ResearcherProfile` exposing the same
properties and method signatures as a local profile:

```python
from researcher_profiles import ResearcherProfile

p = ResearcherProfile.from_api(
    "http://127.0.0.1:8109",
    slug="jane-doe",
    token="a-long-random-string",  # or set RESEARCHER_PROFILES_TOKEN
)
print(p.name, p.affiliation)
print(len(p.papers))
print(list(p.summaries.keys()))

hits = p.index.search("region set enrichment", k=3)
for h in hits:
    print(round(h.score, 3), h.source_type, h.source_id)
```

You can also pass a slug-qualified URL and omit `slug`:

```python
p = ResearcherProfile.from_api(
    "http://127.0.0.1:8109/api/v1/profiles/jane-doe",
    token="a-long-random-string",
)
```

If `token` is not passed, the client reads `RESEARCHER_PROFILES_TOKEN` from the
environment. Use the profile as a context manager to close its HTTP connection:

```python
with ResearcherProfile.from_api("http://127.0.0.1:8109", slug="jane-doe") as p:
    print(p.name)
```

List remote profiles without loading each one. `list_remote` walks every page
and returns all the rows:

```python
summaries = ResearcherProfile.list_remote("http://127.0.0.1:8109", token="...")
for s in summaries:
    print(s["slug"], s["name"], s["level"], s["paper_count"])
```

To walk any list endpoint yourself, page-based or token-based, use
`iter_results` with an `httpx.Client`:

```python
import httpx
from researcher_profiles.client import iter_results

with httpx.Client(base_url="http://127.0.0.1:8109") as http:
    for row in iter_results(http, "/api/v1/profiles", {"clinical": "true"}):
        print(row["slug"])
```

Each summary carries a `level`, so you can branch before making a persona call. A
persona call (`ask`/`review`/`innovate`/`riff`) against a `lite` profile, or any
profile without a synthesized persona, returns 409, which the client raises as
a generic client error. See
[How to use the persona methods](persona-methods.md#when-a-profile-has-no-persona).

## Match profiles over HTTP

Cross-profile ranking is not profile-scoped, so it is a module-level function
that POSTs to `/api/v1/match`:

```python
from researcher_profiles.client import rank_against

matches = rank_against(
    "http://127.0.0.1:8109",
    "region set enrichment analysis",
    k=5,
    token="a-long-random-string",
)
for m in matches:
    print(round(m["score"], 3), m["slug"], m["name"], m["rid"])
```

Each match is a dict shaped like `MatchResult`: `{slug, rid, name, orcid, score,
evidence}`. `rid` is the join key across servers; `orcid` is the ORCID when
the rid is an ORCID rid, else `null`. Pass `include_chunks=True` to have the
server return chunk-level
evidence in `evidence.top_chunks`.

The `/api/v1/match` endpoint is embedding-backed. On a server without the
`embeddings` extra, or when no profile is indexed, it returns 503 rather than
crashing, so callers can fall back instead of failing. See the
[API reference](../reference/api.md#post-apiv1match).

