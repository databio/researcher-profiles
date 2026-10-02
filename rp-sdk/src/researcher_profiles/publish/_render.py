"""Refresh one profile folder in place, and render a profile page for one audience."""

import logging
import os
import re
from pathlib import Path
from typing import Any

from ..privacy import ViewerTier, effective_tiers, tier_allows
from ..profile import ResearcherProfile
from ..profile.payloads import (
    paper_entries_list,
    profile_detail_dict,
)
from ..schema import ArtifactRef
from ..schema.manifest import build_manifest
from ._html import render_profile_page
from ._jsonld import profile_jsonld_graph

logger = logging.getLogger(__name__)


def render_profile(
    profile_dir: str | os.PathLike,
    *,
    base_url: str | None = None,
    no_index: bool = False,
) -> None:
    """Refresh a single profile folder in place.

    Rebuilds the manifest into ``profile.jsonld`` (``hasPart`` / ``subjectOf``
    plus the ``index.html`` entry and the consumer flags) and renders
    ``index.html`` at ``public``.

    This writes nothing outside the profile folder and gates on nothing.
    Deployment is ``rp publish --who <tier>`` (:func:`publish_collection`), then
    any sync of its output folder.
    """
    prof_dir = Path(profile_dir).expanduser().resolve()
    if not (prof_dir / "profile.jsonld").is_file():
        raise FileNotFoundError(f"no profile.jsonld at {prof_dir}")

    prof = ResearcherProfile.from_files(prof_dir)

    # ---- self-heal the servable flat embeddings -----------------------
    # If a fresh sqlite index exists but the flat form is missing/stale, write
    # it now (idempotent) so `rp render` on its own produces a consistent served
    # form rather than depending on a build tool having done it first.
    from ..embeddings.flat import write_flat_export

    write_flat_export(prof_dir, profile_document=prof.metadata)

    # ---- refresh the manifest -----------------------------------------
    parts, subjects = build_manifest(prof_dir)

    # A rendered profile advertises its HTML entry point. The manifest lists the
    # profile's own files with relative contentUrls, so the consumer-skill
    # document is not a manifest entry: it ships with the SDK and is advertised
    # site-wide by `rp site`'s SKILL.md, not per profile.
    parts = [p for p in parts if p.content_url != "index.html"]
    parts.append(
        ArtifactRef(
            type_="DigitalDocument",
            name="Profile page",
            encoding_format="text/html",
            content_url="index.html",
            role="html",
        )
    )

    prof.metadata.has_part = parts
    prof.metadata.subject_of = subjects

    # ---- consumer flags -----------------------------------------------
    published_paper_ids = {p.paper_id for p in _published_papers(prof) if p.paper_id}

    has_citation_graph = (prof_dir / "sources" / "citations.json").is_file()
    # The flag means "the served flat index exists", not the
    # private, never-deployed sqlite. A public copy advertises embeddings
    # only when it actually ships them.
    has_embedding_index = (prof_dir / "embeddings" / "index.json").is_file()
    expertise_cites: bool | None = None
    if prof.expertise:
        cited = set(re.findall(r"\[([^\[\]\s]+)\]", prof.expertise))
        expertise_cites = bool(cited & published_paper_ids)

    # ---- write profile.jsonld -----------------------------------------
    # All three flags are real ProfileDocument fields, so this is a model copy
    # and one ``save_profile`` call: it canonicalizes, stamps dateModified
    # against what the store already holds, re-validates, and persists.
    update: dict[str, Any] = {
        "has_citation_graph": has_citation_graph,
        "has_embedding_index": has_embedding_index,
    }
    if expertise_cites is not None:
        update["expertise_cites_paper_ids"] = expertise_cites
    prof.save_profile(prof.metadata.model_copy(update=update))

    # ---- render index.html --------------------------------------------
    # ``public``: the in-place page sits beside the private files, and a
    # profile folder is not an audience-specific export. ``rp publish`` renders
    # its own page per audience instead of copying this one.
    html = render_page(prof, "public", base_url=base_url, no_index=no_index)
    (prof_dir / "index.html").write_text(html, encoding="utf-8")


def render_page(
    prof: ResearcherProfile,
    viewer: ViewerTier,
    *,
    base_url: str | None = None,
    no_index: bool = False,
) -> str:
    """The profile's ``index.html`` as a viewer entitled to ``viewer`` sees it.

    No exclude list can redact a field out of a page that already contains it,
    so the page is projected at render time: the same projection the HTTP read
    uses decides the inline sections, and each manifest-backed block (expertise,
    SOUL, the works and grants lists) appears only when its artifact's
    effective tier reaches ``viewer``.
    """
    detail = profile_detail_dict(prof, viewer)
    tiers = effective_tiers(prof.metadata)

    def allowed(content_url: str) -> bool:
        return tier_allows(viewer, tiers.get(content_url, prof.metadata.visibility))

    for key, content_url in (
        ("expertise", "personality/expertise.md"),
        ("soul", "personality/SOUL.md"),
    ):
        if not allowed(content_url):
            detail[key] = None

    build_state = prof.build_state
    papers_list: list[dict[str, Any]] = []
    filtered_papers = []
    if allowed("sources/papers.jsonld"):
        papers_list = paper_entries_list(
            prof,
            build_state=build_state,
            exclude_contaminated=True,
            exclude_untitled=True,
        )
        filtered_papers = _published_papers(prof)
    grants = prof.grants if allowed("sources/grants.jsonld") else []
    grants_for_html: list[dict[str, Any]] = []
    for g in grants:
        gd = g.model_dump(mode="json")
        gd.pop("abstract", None)
        grants_for_html.append(gd)
    jsonld_graph = profile_jsonld_graph(detail, filtered_papers, grants)
    return render_profile_page(
        prof.slug,
        detail,
        papers_list,
        grants_for_html,
        jsonld_graph,
        base_url=base_url,
        no_index=no_index,
    )


def _published_papers(prof: ResearcherProfile) -> list:
    """Titled papers not flagged as contaminated: the ones a page may list."""
    build_state = prof.build_state
    return [
        p
        for p in prof.papers
        if (p.title and p.title.strip())
        and not (p.paper_id and build_state.is_contaminated(p.paper_id))
    ]
