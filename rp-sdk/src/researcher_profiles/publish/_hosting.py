"""Hosting configuration files for static deployment.

Emits the Cloudflare Pages / Netlify ``_headers`` file plus the crawl and
discovery files (``robots.txt``, ``sitemap.xml``, the well-known document).
Other hosts are configured by hand; see
``docs/rp-sdk/publishing.md``.

Without permissive CORS headers the browser app cannot fetch cross-origin
(fails silently: empty page, no error).
"""

import json


def cloudflare_headers() -> str:
    """Generate ``_headers`` for Cloudflare Pages / Netlify."""
    return """\
/*
  Access-Control-Allow-Origin: *
  Access-Control-Allow-Methods: GET, HEAD, OPTIONS
  Access-Control-Allow-Headers: *

/*.jsonld
  Content-Type: application/ld+json; charset=utf-8

/*.bin
  Content-Type: application/octet-stream

/*.md
  Content-Type: text/markdown; charset=utf-8
"""


def robots_txt(*, base_url: str | None = None, no_index: bool = False) -> str:
    """Generate ``robots.txt``."""
    if no_index:
        lines = ["User-agent: *", "Disallow: /"]
    else:
        lines = ["User-agent: *", "Allow: /"]
    if base_url:
        lines.append(f"Sitemap: {base_url.rstrip('/')}/sitemap.xml")
    return "\n".join(lines) + "\n"


def sitemap_xml(
    slugs: list[str],
    *,
    base_url: str,
    timestamp: str | None = None,
) -> str:
    """Generate ``sitemap.xml``."""
    base = base_url.rstrip("/")
    urls = [
        f"""\
  <url>
    <loc>{base}/profiles/{slug}/index.html</loc>{
            f'''
    <lastmod>{timestamp}</lastmod>'''
            if timestamp
            else ""
        }
  </url>"""
        for slug in sorted(slugs)
    ]

    # Add root
    root_url = f"""\
  <url>
    <loc>{base}/index.html</loc>{
        f'''
    <lastmod>{timestamp}</lastmod>'''
        if timestamp
        else ""
    }
  </url>"""

    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{root_url}
{"".join(urls)}
</urlset>
"""


def well_known_json(*, base_url: str | None = None) -> str:
    """Generate ``.well-known/researcher-profiles.json``."""
    doc = {
        "version": 1,
        "profiles_index": "index.json",
        "by_rid": "by-rid.json",
    }
    if base_url:
        doc["base_url"] = base_url.rstrip("/")
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"
