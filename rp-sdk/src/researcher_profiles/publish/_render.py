"""Refresh one profile folder in place: the single-profile publish path."""

import logging
import os
import re
from pathlib import Path
from typing import Any

from ..privacy import render_publishignore
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
    plus the ``index.html`` and consumer-skill entries and the consumer flags),
    renders ``index.html``, and writes ``.publishignore`` at the profile root.

    This does not flatten the tree, copy anything to an output directory, or gate
    on visibility. A profile already is its published form.
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
    build_state = prof.build_state
    filtered_papers = [
        p
        for p in prof.papers
        if (p.title and p.title.strip())
        and not (p.paper_id and build_state.is_contaminated(p.paper_id))
    ]
    published_paper_ids = {p.paper_id for p in filtered_papers if p.paper_id}

    has_citation_graph = (prof_dir / "sources" / "citations.json").is_file()
    # The flag means "the served flat index exists", not the
    # restricted, never-deployed sqlite. A public copy advertises embeddings
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
    detail = profile_detail_dict(prof)
    papers_list = paper_entries_list(
        prof,
        build_state=build_state,
        exclude_contaminated=True,
        exclude_untitled=True,
    )
    grants_for_html: list[dict[str, Any]] = []
    for g in prof.grants:
        gd = g.model_dump(mode="json")
        gd.pop("abstract", None)
        grants_for_html.append(gd)
    jsonld_graph = profile_jsonld_graph(detail, filtered_papers, prof.grants)
    html = render_profile_page(
        prof.slug,
        detail,
        papers_list,
        grants_for_html,
        jsonld_graph,
        base_url=base_url,
        no_index=no_index,
    )
    (prof_dir / "index.html").write_text(html, encoding="utf-8")

    # ---- .publishignore -----------------------------------------------
    # ``privacy.render_publishignore`` is the single authority: it derives the
    # deny-list from the same effective-tier rule everything else uses, so the
    # declared tiers and the deployed layout cannot drift.
    (prof_dir / ".publishignore").write_text(render_publishignore(prof.metadata), encoding="utf-8")
