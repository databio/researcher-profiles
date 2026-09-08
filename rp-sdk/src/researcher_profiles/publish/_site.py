"""Build the collection: stage a set of profiles and their collection files."""

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ProfileLoadError
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

    Used to undo a ``mkdir(parents=True)``: only the directories this call would
    actually create are candidates for removal on failure.
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

    :func:`build_site` does not own the whole output directory. The per-profile
    folders under ``profiles/`` are rsynced in by the deploy script (honouring
    each ``.publishignore``) and ``app/`` comes from the explorer build, so a
    whole-directory swap would destroy them. The commit granularity here is
    therefore a single file: each staged file is moved onto its destination with
    :func:`os.replace`, which is atomic per path on POSIX. Nothing under ``out``
    that the build did not write is read, moved, or removed, not even the
    directories the build shares with other producers (``collection/``,
    ``.well-known/``), which are created if absent and otherwise left alone.

    The staging tree is created *inside* ``out`` so every move is a
    same-filesystem rename rather than a copy. Because all content is written
    during staging, a build that raises has touched no destination at all. If a
    move itself fails, the destinations already replaced are restored from
    backups taken immediately before each one, so ``commit`` is all-or-nothing
    too.
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

        Returns ``None`` when there is nothing at ``dest``. Rolling that path
        back means deleting what we put there. A hardlink is preferred because it
        copies no bytes and leaves ``dest`` in place while it is taken; a
        filesystem that refuses one falls back to a byte copy.
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
) -> SiteResult:
    """Write the collection files describing a set of profiles into ``out_dir``.

    These are the documents about the collection. They belong to no single
    profile: the index, the researcher-id map, the agent skill, the hosting
    configs, the sitemap, and the JSON-LD ``@context`` copy. Each profile's own
    ``profile.jsonld`` is read to build the index and ``by-rid`` map.

    This does not copy profile folders. Deployment rsyncs the folders (honouring
    each ``.publishignore``) alongside these collection files.

    The write is atomic in this sense: every file is built into a staging
    directory first and only moved into ``out`` once the whole build has
    succeeded, so if this function raises, ``out`` is exactly as it was before
    the call. A live site is never left half-updated. The move itself is a
    per-path :func:`os.replace`, so a reader that opens one collection file mid
    commit always sees a complete file, though not necessarily one from the same
    build as its neighbour. Only the files listed above are touched: sibling
    trees this function does not own (``profiles/``, ``app/``) are never read or
    removed, and neither are files it does not write in directories it shares.
    """
    root = Path(profiles_root).expanduser().resolve()
    out = Path(out_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"profiles root does not exist: {root}")

    timestamp = now_iso(now)
    result = SiteResult(out_dir=out)
    # Staging lives inside ``out`` so the moves are same-filesystem renames. If
    # the build fails and ``out`` did not exist beforehand, the directories this
    # call created come back out again.
    created = _missing_ancestors(out)
    out.mkdir(parents=True, exist_ok=True)
    stage = _SiteStage(out)
    try:
        try:
            _build_into(stage, root, result, timestamp, base_url, no_index)
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


def _build_into(
    stage: _SiteStage,
    root: Path,
    result: SiteResult,
    timestamp: str,
    base_url: str | None,
    no_index: bool,
) -> None:
    """Stage every collection file. Raising here leaves ``out`` untouched."""
    profile_summaries: list[dict[str, Any]] = []
    by_rid: dict[str, str] = {}
    slugs: list[str] = []
    #: (slug, backend_spec, centroid_vector, probe) for every profile with a
    #: served flat index: the raw material for the collection centroid blob.
    centroid_entries: list[tuple[str, str, Any, dict | None]] = []

    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / "profile.jsonld").is_file():
            continue
        try:
            prof = ResearcherProfile.from_files(entry)
        except ProfileLoadError as e:
            result.warnings.append(f"Skipping {entry.name}: {e}")
            logger.warning("Skipping %s: %s", entry.name, e)
            continue
        slugs.append(prof.slug)
        profile_summaries.append(profile_summary_dict(prof))
        if prof.rid:
            by_rid[prof.rid] = f"profiles/{prof.slug}/profile.jsonld"
        _collect_centroid(entry, prof, centroid_entries, result)

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

    if base_url:
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
    # Self-host the @context every published document references, so the
    # PUBLISHED_CONTEXT_URL resolves to real bytes instead of a 404. The bytes
    # come from the wheel-bundled copy on a pip install, or the repo-root copy
    # in a source checkout (see jsonld.context_document_text).
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
    # One L2-normalized centroid per public profile, for ranking profiles
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
    _write_topics_index(root, slugs, _write, result, timestamp)
