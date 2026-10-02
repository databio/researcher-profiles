"""Write the files that put a profile on the web: its HTML page, its metadata,
and the index for a whole collection.

Three writers, and the projection they share (:func:`plan_profile_export`):

- :func:`render_profile` updates one profile folder in place. It rebuilds the
  manifest (``hasPart`` / ``subjectOf``) in ``profile.jsonld``, records the
  consumer flags, and writes ``index.html``.
- :func:`publish_collection` writes the static tree one audience may see: each
  visible profile projected to that tier, plus the collection files.
- :func:`build_site` writes the files that describe a set of profiles and belong
  to no single one: ``index.json``, ``index.jsonld``, ``by-rid.json``,
  ``SKILL.md``, ``style.css``, ``_headers``, ``sitemap.xml``, ``robots.txt``,
  ``.well-known/researcher-profiles.json``, and the JSON-LD ``@context`` copy.

Deployment is ``rp publish --who <tier>`` then any sync of the output folder.
Privacy is declared per artifact and per section in the profile, and applied
when the export is written, so the output folder needs no filtering.
"""

from ._export import ExportPlan, plan_profile_export
from ._publish import ProfileExport, PublishError, PublishResult, publish_collection
from ._render import render_page, render_profile
from ._site import SiteResult, build_site

__all__ = [
    "ExportPlan",
    "ProfileExport",
    "PublishError",
    "PublishResult",
    "SiteResult",
    "build_site",
    "plan_profile_export",
    "publish_collection",
    "render_page",
    "render_profile",
]
