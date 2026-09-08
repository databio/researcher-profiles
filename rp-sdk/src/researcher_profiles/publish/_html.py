"""Pre-rendered HTML page generation.

Every page is complete with JavaScript disabled. The SPA viewer is an
optional progressive enhancement layered on top of the static pages.

Uses ``html.escape`` on every interpolated value: no template engine.
"""

import html
import json
from typing import Any

from ._markdown import md_to_html


def _esc(value: Any) -> str:
    """Escape a value for safe HTML interpolation."""
    if value is None:
        return ""
    return html.escape(str(value))


def render_profile_page(
    slug: str,
    profile_detail: dict[str, Any],
    papers: list[dict[str, Any]],
    grants: list[dict[str, Any]],
    jsonld_graph: dict[str, Any],
    *,
    base_url: str | None = None,
    no_index: bool = False,
    css_path: str = "../../style.css",
) -> str:
    """Render a complete per-profile index.html."""
    md = profile_detail.get("metadata", {})
    name = _esc(md.get("name", slug))
    affiliation = _esc(md.get("affiliation", ""))
    field_ = _esc(md.get("field", ""))
    summary = _esc(md.get("summary", ""))
    expertise_md = profile_detail.get("expertise", "")
    soul_md = profile_detail.get("soul", "")
    rid = _esc(profile_detail.get("rid", ""))

    expertise_html = md_to_html(expertise_md) if expertise_md else ""
    soul_html = md_to_html(soul_md) if soul_md else ""

    # Papers table
    papers_rows = ""
    for p in papers:
        title = _esc(p.get("title", ""))
        year = _esc(p.get("year", ""))
        journal = _esc(p.get("journal", ""))
        first_author = _esc(p.get("first_author", ""))
        papers_rows += (
            f"<tr><td>{title}</td><td>{first_author}</td><td>{year}</td><td>{journal}</td></tr>\n"
        )

    papers_section = ""
    if papers:
        papers_section = f"""
<section id="papers">
<h2>Publications ({len(papers)})</h2>
<div style="overflow-x:auto">
<table>
<thead><tr><th>Title</th><th>First Author</th><th>Year</th><th>Journal</th></tr></thead>
<tbody>
{papers_rows}
</tbody>
</table>
</div>
</section>"""

    # Grants section
    grants_section = ""
    if grants:
        grants_items = ""
        for g in grants:
            gtitle = _esc(g.get("name", g.get("title", "")))
            gfunder = g.get("funder", "")
            if isinstance(gfunder, dict):
                gfunder = gfunder.get("name", "")
            gfunder = _esc(gfunder)
            grole = _esc(g.get("role", ""))
            gstatus = _esc(g.get("status", ""))
            grants_items += f"<li><strong>{gtitle}</strong>"
            details = []
            if gfunder:
                details.append(gfunder)
            if grole:
                details.append(grole)
            if gstatus:
                details.append(gstatus)
            if details:
                grants_items += f" ({', '.join(details)})"
            grants_items += "</li>\n"
        grants_section = f"""
<section id="grants">
<h2>Grants ({len(grants)})</h2>
<ul>
{grants_items}
</ul>
</section>"""

    # JSON-LD script
    jsonld_script = (
        '<script type="application/ld+json">'
        + json.dumps(jsonld_graph, ensure_ascii=False)
        + "</script>"
    )

    # Meta tags
    meta_robots = ""
    if no_index:
        meta_robots = '<meta name="robots" content="noindex">'

    canonical = ""
    if base_url:
        canon_url = f"{base_url.rstrip('/')}/profiles/{_esc(slug)}/index.html"
        canonical = f'<link rel="canonical" href="{canon_url}">'

    og_tags = f"""<meta property="og:title" content="{name}">
<meta property="og:type" content="profile">
<meta property="og:description" content="{_esc(md.get("summary", ""))}">"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} - Researcher Profile</title>
{meta_robots}
{canonical}
{og_tags}
<link rel="alternate" type="application/ld+json" href="profile.jsonld">
<link rel="stylesheet" href="{_esc(css_path)}">
</head>
<body>
<nav><a href="../../index.html">All Profiles</a> | <a id="explorer-link" href="../../app/">Explorer View</a></nav>
<main>
<header>
<h1>{name}</h1>
{f'<p class="affiliation">{affiliation}</p>' if affiliation else ""}
{f'<p class="field">{field_}</p>' if field_ else ""}
{f'<p class="rid">ID: {rid}</p>' if rid else ""}
{f'<p class="summary">{summary}</p>' if summary else ""}
</header>

{f'<section id="expertise"><h2>Expertise</h2>{expertise_html}</section>' if expertise_html else ""}

{f'<section id="soul"><h2>Research Identity</h2>{soul_html}</section>' if soul_html else ""}

{papers_section}

{grants_section}

<section id="data-files">
<h2>Data Files</h2>
<ul class="file-list">
<li><a href="profile.jsonld">profile.jsonld</a></li>
<li><a href="profile.json">profile.json</a></li>
<li><a href="papers.jsonld">papers.jsonld</a></li>
<li><a href="papers.json">papers.json</a></li>
<li><a href="expertise.md">expertise.md</a></li>
<li><a href="soul.md">soul.md</a></li>
<li><a href="summaries/">summaries/</a></li>
</ul>
</section>

{jsonld_script}
</main>
<script>
document.getElementById("explorer-link").href =
  "../../app/#/p?u=" + encodeURIComponent(new URL(".", location.href).href);
</script>
</body>
</html>
"""


def render_index_page(
    profiles: list[dict[str, Any]],
    *,
    base_url: str | None = None,
    no_index: bool = False,
    title: str = "Researcher Profiles",
) -> str:
    """Render the root catalog index.html."""
    rows = ""
    for p in sorted(profiles, key=lambda x: x.get("name", "")):
        slug = _esc(p.get("slug", ""))
        name = _esc(p.get("name", slug))
        affiliation = _esc(p.get("affiliation", ""))
        field_ = _esc(p.get("field", ""))
        paper_count = p.get("paper_count", 0)
        rows += (
            f'<tr><td><a href="profiles/{slug}/index.html">{name}</a></td>'
            f"<td>{affiliation}</td><td>{field_}</td>"
            f"<td>{paper_count}</td>"
            f'<td><a href="profiles/{slug}/profile.jsonld">jsonld</a> '
            f'<a href="profiles/{slug}/profile.json">json</a></td></tr>\n'
        )

    meta_robots = ""
    if no_index:
        meta_robots = '<meta name="robots" content="noindex">'

    canonical = ""
    if base_url:
        canon_url = f"{base_url.rstrip('/')}/index.html"
        canonical = f'<link rel="canonical" href="{canon_url}">'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
{meta_robots}
{canonical}
<link rel="stylesheet" href="style.css">
</head>
<body>
<main>
<h1>{_esc(title)}</h1>
<p>{len(profiles)} profiles published.</p>
<div style="overflow-x:auto">
<table>
<thead><tr><th>Name</th><th>Affiliation</th><th>Field</th><th>Papers</th><th>Data</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div>
</main>
</body>
</html>
"""


CSS = """\
/* researcher-profiles static site: minimal stylesheet */
:root {
  --bg: #fff; --fg: #222; --link: #0366d6; --border: #e1e4e8;
  --table-stripe: #f6f8fa; --code-bg: #f0f0f0;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #c9d1d9; --link: #58a6ff; --border: #30363d;
    --table-stripe: #161b22; --code-bg: #161b22;
  }
}
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  color: var(--fg); background: var(--bg); max-width: 960px; margin: 0 auto; padding: 1rem; line-height: 1.6; }
a { color: var(--link); }
nav { margin-bottom: 1.5rem; }
h1 { margin-bottom: 0.25rem; }
.affiliation, .field, .rid { margin: 0.15rem 0; color: #666; }
@media (prefers-color-scheme: dark) { .affiliation, .field, .rid { color: #8b949e; } }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border); }
tbody tr:nth-child(even) { background: var(--table-stripe); }
pre { background: var(--code-bg); padding: 1rem; overflow-x: auto; border-radius: 4px; }
code { font-size: 0.9em; }
blockquote { border-left: 3px solid var(--border); margin-left: 0; padding-left: 1rem; color: #666; }
section { margin-top: 2rem; }
ul, ol { padding-left: 1.5rem; }
.file-list { list-style: none; padding: 0; }
.file-list li { display: inline-block; margin-right: 1rem; }
.file-list a { font-family: monospace; font-size: 0.9em; }
"""
