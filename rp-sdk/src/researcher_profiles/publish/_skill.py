"""SKILL.md generator for the published static site.

Produces a markdown file describing the site layout and how an LLM agent
should navigate it. This is the agent's entry point into the published tree.
"""

from typing import Any


def generate_skill_md(
    profiles: list[dict[str, Any]],
    *,
    base_url: str | None = None,
    has_embeddings: bool = False,
    has_collection: bool = False,
) -> str:
    """Generate the SKILL.md content for the static site."""
    profile_list = ""
    for p in sorted(profiles, key=lambda x: x.get("slug", "")):
        slug = p.get("slug", "")
        name = p.get("name", slug)
        field = p.get("field", "")
        papers = p.get("paper_count", 0)
        field_note = f" ({field})" if field else ""
        profile_list += f"- **{name}**{field_note}: `profiles/{slug}/`  ({papers} papers)\n"

    embeddings_section = ""
    if has_embeddings:
        embeddings_section = """
## Embeddings

The site includes pre-computed embedding vectors for similarity search.

### Collection centroids
- `collection/embeddings/index.json`: metadata (model, dimension, row manifest)
- `collection/embeddings/<backend>.bin`: float32 centroid matrix (one row per profile)

### Per-profile chunks
Each profile with embeddings has:
- `profiles/<slug>/embeddings/index.json`: chunk-level metadata
- `profiles/<slug>/embeddings/<backend>.bin`: float32 chunk vectors
- `profiles/<slug>/embeddings/<backend>.chunks.json`: row-aligned chunk metadata (source_type, source_id, section)

Blob format: raw little-endian float32, row-major, no header. Read with:
```javascript
const mat = new Float32Array(await (await fetch(url)).arrayBuffer());
```
"""

    collection_section = ""
    if has_collection:
        collection_section = """
## Collection data

- `collection.jsonld`: collection bundle (profile cards, artifact links, embedding metadata)
- `collection/topics.json`: topic labels indexed by profile base path
"""

    # Every path below is site-relative; an agent handed this file as text
    # rather than as a fetch has nothing to resolve them against without this.
    base_line = f"\nSite base URL: `{base_url.rstrip('/')}`\n" if base_url else ""

    return f"""# Researcher Profiles: Static Site

This directory contains a published collection of researcher profiles as
static files. No server, no database, no credentials required.
{base_line}
## Quick start

1. **Find a profile**: read `index.json` for the full list
2. **Read a profile's manifest**: `profiles/<slug>/profile.jsonld`, the entry
   point. It carries `provenance`, `license`, `level`, the expertise topic
   labels, and typed `artifacts` links (`rel`/`href`); resolve every other
   file through it rather than by path convention
3. **Get profile details**: read `profiles/<slug>/profile.json`
4. **Get papers**: read `profiles/<slug>/papers.json`
5. **Get paper summaries**: read `profiles/<slug>/summaries/index.json`
6. **Resolve a researcher id (rid) to a path**: read `by-rid.json`

To *converse with* a profile (adopt its persona, answer as the researcher),
follow the consumer skill that ships with the `researcher-profiles` SDK. Run
`rp skill` to print it, or `rp skill --install` to install it under
`~/.claude/skills/researcher-profile/`.

## Site layout

```
index.json                : ProfileSummary[] (all profiles)
index.html                : human-readable catalog page
index.jsonld              : schema.org DataCatalog
by-rid.json               : {{rid: "profiles/<slug>/profile.json"}}
SKILL.md                  : this file
publish-manifest.json     : build metadata
profiles/<slug>/
  profile.jsonld          : manifest: identity + typed artifact links (entry point)
  graph.jsonld            : self-contained JSON-LD graph (Person + papers + grants)
  profile.json            : ProfileDetail (metadata, expertise, soul)
  papers.json             : PaperEntry[] (bibliographic records)
  papers.jsonld           : JSON-LD papers collection
  index.html              : pre-rendered HTML page
  expertise.md            : expertise document (verbatim)
  soul.md                 : research identity document (verbatim)
  summaries/
    index.json            : {{paper_id: {{json, md, html}}}} href map
    <encoded_id>.json     : paper summary as JSON
    <encoded_id>.md       : paper summary as markdown
    <encoded_id>.html     : paper summary as HTML fragment
```

## Profiles

{profile_list}
{embeddings_section}{collection_section}
## Wire format

The JSON files match the HTTP API wire shapes exactly:

| File | Equivalent API endpoint |
|------|------------------------|
| `index.json` | `GET /api/v1/profiles` |
| `profiles/<slug>/profile.json` | `GET /api/v1/profiles/<slug>` |
| `profiles/<slug>/papers.json` | `GET /api/v1/profiles/<slug>/papers` |

## JSON-LD

Each `graph.jsonld` is a self-contained graph with Person + all
ScholarlyArticle + all Grant nodes inlined. Site-local `@id` values are
relative fragments (`#person`, `#paper/<id>`, `#grant/<id>`). Global
identifiers (ORCID, DOI) are absolute IRIs. `profile.jsonld` is the slim
manifest that enumerates every artifact as a typed link.
"""
