"""Write the files that put a profile on the web: its HTML page, its metadata,
and the index for a whole collection.

Two functions:

- :func:`render_profile` updates one profile folder. It rebuilds the manifest
  (``hasPart`` / ``subjectOf``) in ``profile.jsonld``, records the consumer
  flags, writes ``index.html``, and writes ``.publishignore``.
- :func:`build_site` writes the files that describe a set of profiles and belong
  to no single one: ``index.json``, ``index.jsonld``, ``by-rid.json``,
  ``SKILL.md``, ``style.css``, ``_headers``, ``sitemap.xml``, ``robots.txt``,
  ``.well-known/researcher-profiles.json``, and the JSON-LD ``@context`` copy.

A profile folder already is its published form, so nothing here copies or
transforms it into a separate output tree. Deployment is an ``rsync`` of the
profile folders (each honouring its ``.publishignore``) plus the collection
files. Privacy is declared per artifact in the profile and enforced by
``.publishignore`` at deploy time.
"""

from ._render import render_profile
from ._site import SiteResult, build_site

__all__ = ["render_profile", "build_site", "SiteResult"]
