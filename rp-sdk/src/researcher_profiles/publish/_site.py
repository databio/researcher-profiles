"""Build the collection: stage a set of profiles and their collection files."""

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ProfileLoadError
from ..privacy import ViewerTier, profile_visible
from ..profile import ResearcherProfile
from ..profile.payloads import (
    profile_summary_dict,
)
from ..schema.jsonld import canonical_dumps
from ..utils.clock import now_iso
from ._centroids import (
    _collect_centroid,
    _write_collection_bundle,
    _write_collection_centroids,
    _write_topics_index,
)
from ._hosting import (
    cloudflare_headers,
    robots_txt,
    sitemap_xml,
    well_known_json,
)
from ._html import CSS, render_index_page
from ._jsonld import catalog_jsonld
from ._skill import generate_skill_md

logger = logging.getLogger(__name__)


def _missing_ancestors(path: Path) -> list[Path]:
    """The directories in ``path``'s own chain that do not exist yet, outermost first.

    Used to undo a ``mkdir(parents=True)`` on failure.
    """
    missing: list[Path] = []
    p = path
    while p != p.parent and not p.exists():
        missing.append(p)
        p = p.parent
    missing.reverse()
    return missing


class _SiteStage:
    """Collect the collection files off to one side, then move them into ``out``.

    :func:`build_site` does not own all of ``out`` (``profiles/`` and ``app/``
    belong to others), so commits are per file via :func:`os.replace`, atomic
    per path on POSIX. Nothing the build did not write is read, moved, or
    removed.

    Staging lives inside ``out`` so moves are same-filesystem renames. A build
    that raises touches no destination; a failed move restores the
    destinations already replaced from per-file backups, so ``commit`` is
    all-or-nothing.
    """

    def __init__(self, out: Path) -> None:
        self.out = out
        self._root = Path(tempfile.mkdtemp(prefix=".rp-site-stage-", dir=out))
        self._files = self._root / "files"
        self._backups = self._root / "backup"
        self._files.mkdir()
        self._backups.mkdir()
        self._staged: list[str] = []

    def write(self, rel: str, content: str | bytes) -> None:
        """Stage one collection file at relative path ``rel``."""
        path = self._files / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        self._staged.append(rel)

    def copy(self, rel: str, src: Path) -> None:
        """Stage a copy of the file at ``src`` at relative path ``rel``."""
        path = self._files / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, path)
        self._staged.append(rel)

    def commit(self) -> None:
        """Move every staged file onto its final path, or restore what was there."""
        done: list[tuple[Path, Path | None]] = []
        made_dirs: list[Path] = []
        try:
            for rel in self._staged:
                dest = self.out / rel
                made_dirs.extend(_missing_ancestors(dest.parent))
                dest.parent.mkdir(parents=True, exist_ok=True)
                backup = self._back_up(rel, dest)
                os.replace(self._files / rel, dest)
                done.append((dest, backup))
        except BaseException:
            for dest, backup in reversed(done):
                if backup is None:
                    dest.unlink(missing_ok=True)
                else:
                    os.replace(backup, dest)
            for d in reversed(made_dirs):
                try:
                    d.rmdir()
                except OSError:
                    pass
            raise

    def _back_up(self, rel: str, dest: Path) -> Path | None:
        """Snapshot an existing ``dest`` so a later failure can put it back.

        ``None`` when there is nothing at ``dest`` (rollback then deletes it).
        Prefers a hardlink, falling back to a byte copy.
        """
        if not os.path.lexists(dest):
            return None
        backup = self._backups / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(dest, backup)
        except OSError:
            shutil.copy2(dest, backup)
        return backup

    def close(self) -> None:
        """Discard the staging tree. Safe to call whether or not ``commit`` ran."""
        shutil.rmtree(self._root, ignore_errors=True)


@dataclass
class SiteResult:
    """Result of a :func:`build_site` run."""

    out_dir: Path
    slugs: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: ``{slug: reason}`` for every profile the audience may not see at all.
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def profile_count(self) -> int:
        return len(self.slugs)


def build_site(
    profiles_root: str | os.PathLike,
    out_dir: str | os.PathLike,
    *,
    base_url: str | None = None,
    no_index: bool = False,
    now: str | None = None,
    viewer: ViewerTier = "public",
) -> SiteResult:
    """Write the collection files describing a set of profiles into ``out_dir``.

    The index, the researcher-id map, the agent skill, the hosting configs,
    the sitemap, and the JSON-LD ``@context`` copy. Profile folders are not
    copied.

    A profile whose ``visibility`` the viewer may not see is left out of every
    file (reported in :attr:`SiteResult.skipped`), and every summary is
    projected to ``viewer``. A non-``public`` audience gets ``noindex`` pages
    and a disallow-all ``robots.txt``: a non-public mirror must never be
    crawled.

    Staged, then committed per file (see :class:`_SiteStage`); if this raises,
    ``out`` is as it was.
    """
    root = Path(profiles_root).expanduser().resolve()
    out = Path(out_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"profiles root does not exist: {root}")

    timestamp = now_iso(now)
    result = SiteResult(out_dir=out)
    # On failure, remove the directories this call created.
    created = _missing_ancestors(out)
    out.mkdir(parents=True, exist_ok=True)
    stage = _SiteStage(out)
    try:
        try:
            _build_into(stage, profile_dirs(root), result, timestamp, base_url, no_index, viewer)
            stage.commit()
        finally:
            stage.close()
    except BaseException:
        for d in reversed(created):
            try:
                d.rmdir()
            except OSError:
                pass
        raise
    return result


def profile_dirs(root: Path) -> list[Path]:
    """Every profile folder directly under ``root``, in name order."""
    return [
        entry
        for entry in sorted(root.iterdir())
        if entry.is_dir()
        and not entry.name.startswith(".")
        and (entry / "profile.jsonld").is_file()
    ]


def _build_into(
    stage: _SiteStage,
    entries: list[Path],
    result: SiteResult,
    timestamp: str,
    base_url: str | None,
    no_index: bool,
    viewer: ViewerTier = "public",
    centroids: dict[str, tuple[str, Any, dict | None]] | None = None,
) -> None:
    """Stage every collection file. Raising here leaves ``out`` untouched.

    ``centroids`` is ``{slug: (backend_spec, centroid, probe)}`` computed from
    the rows that ship to ``viewer``. Without it, each profile's centroid is
    read from its own flat files.
    """
    no_index = no_index or viewer != "public"
    profile_summaries: list[dict[str, Any]] = []
    by_rid: dict[str, str] = {}
    slugs: list[str] = []
    #: (slug, backend_spec, centroid_vector, probe) per profile with a served flat index.
    centroid_entries: list[tuple[str, str, Any, dict | None]] = []

    dirs: dict[str, Path] = {}

    for entry in entries:
        try:
            prof = ResearcherProfile.from_files(entry)
        except ProfileLoadError as e:
            result.warnings.append(f"Skipping {entry.name}: {e}")
            logger.warning("Skipping %s: %s", entry.name, e)
            continue
        if not profile_visible(prof.metadata, viewer):
            result.skipped[prof.slug] = (
                f"profile visibility is {prof.metadata.visibility}, above {viewer}"
            )
            continue
        slugs.append(prof.slug)
        dirs[prof.slug] = entry
        profile_summaries.append(profile_summary_dict(prof, viewer))
        if prof.rid:
            by_rid[prof.rid] = f"profiles/{prof.slug}/profile.jsonld"
        if centroids is None:
            _collect_centroid(entry, prof, centroid_entries, result, viewer)
        elif prof.slug in centroids:
            centroid_entries.append((prof.slug, *centroids[prof.slug]))

    result.slugs = slugs

    def _write(rel: str, content: str | bytes) -> None:
        stage.write(rel, content)
        result.files.append(rel)

    # ---- catalog / index files ----------------------------------------
    _write("index.json", canonical_dumps([f"profiles/{s}/" for s in sorted(slugs)]))
    _write("by-rid.json", canonical_dumps(by_rid))
    _write(
        "index.jsonld",
        canonical_dumps(catalog_jsonld(profile_summaries, base_url=base_url)),
    )
    _write("style.css", CSS)
    _write(
        "SKILL.md",
        generate_skill_md(profile_summaries, base_url=base_url),
    )
    _write(
        "index.html",
        render_index_page(profile_summaries, base_url=base_url, no_index=no_index),
    )

    # ---- hosting configs ----------------------------------------------
    _write("_headers", cloudflare_headers())

    # A non-public export gets no sitemap: it would list every slug, including
    # the ones only that audience may see, and point crawlers at them.
    if viewer != "public":
        _write("robots.txt", robots_txt(no_index=True))
    elif base_url:
        _write(
            "sitemap.xml",
            sitemap_xml(slugs, base_url=base_url, timestamp=timestamp),
        )
        _write("robots.txt", robots_txt(base_url=base_url, no_index=no_index))
    else:
        result.warnings.append("No base_url given; skipping sitemap.xml and robots.txt")

    _write(
        ".well-known/researcher-profiles.json",
        well_known_json(base_url=base_url),
    )

    # ---- JSON-LD context copy -----------------------------------------
    # Self-host the @context so PUBLISHED_CONTEXT_URL resolves.
    from ..schema.jsonld import context_document_text

    context_text = context_document_text()
    if context_text is not None:
        _write("context/v1.jsonld", context_text)
    else:
        result.warnings.append(
            "Context file not found (researcher_profiles/context/v1.jsonld or "
            "repo-root context/v1.jsonld); published @context will not resolve "
            "on this site."
        )

    # ---- collection centroids (site-level embeddings) -----------------
    # One L2-normalized centroid per searchable profile, for ranking profiles
    # against a query without fetching every profile's chunk set (spec §7).
    centroid_meta = _write_collection_centroids(centroid_entries, _write, result)

    # ---- collection bundle (collection.jsonld) ------------------------
    _write_collection_bundle(
        profile_summaries,
        centroid_meta,
        _write,
        result,
        timestamp,
        base_url,
    )

    # ---- topics index --------------------------------------------------
    _write_topics_index(dirs, _write, result, timestamp)
