"""Export a set of profiles as the static tree one audience may see.

:func:`publish_collection` is the whole privacy story for a static host. It
writes a self-contained folder holding exactly what one viewer tier may read:
each visible profile projected through :func:`plan_profile_export`, its page
rendered at that tier, its embeddings exported at that tier, and the
collection files built for that tier. Uploading the folder is a separate, dumb
step (``aws s3 sync``, ``rclone``, ``wrangler``, ``rsync``) that needs no
filtering.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ProfileError, ProfileLoadError
from ..privacy import ViewerTier, derivation_errors, profile_visible
from ..profile import ResearcherProfile
from ..utils.clock import now_iso
from ..utils.paths import cache_dir
from ._export import PROFILE_DOCUMENT, plan_profile_export
from ._render import render_page
from ._site import SiteResult, _build_into, _missing_ancestors, _SiteStage, profile_dirs

logger = logging.getLogger(__name__)

#: Written into ``out`` on success. Its presence is what lets a later run
#: replace the folder's contents; its file list is what lets that run remove
#: the files a tightened tier no longer allows, and nothing else.
MARKER = ".rp-publish.json"


class PublishError(ProfileError):
    """The export was refused before anything was written."""


@dataclass
class ProfileExport:
    """What one profile contributes to the export."""

    slug: str
    #: Profile-relative paths written under ``profiles/<slug>/``.
    files: list[str] = field(default_factory=list)
    #: Profile-relative paths on disk that were not written, with the reason.
    withheld: dict[str, str] = field(default_factory=dict)


@dataclass
class PublishResult:
    """Result of a :func:`publish_collection` run."""

    out_dir: Path
    viewer: ViewerTier
    profiles: list[ProfileExport] = field(default_factory=list)
    #: ``{slug: reason}`` for each profile the audience may not see at all.
    skipped: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: Paths under ``out`` removed because the previous export wrote them and
    #: this one did not.
    removed: list[str] = field(default_factory=list)
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        """The result as plain data, for ``--json``."""
        return {
            "out_dir": str(self.out_dir),
            "who": self.viewer,
            "dry_run": self.dry_run,
            "profiles": [
                {"slug": p.slug, "files": p.files, "withheld": p.withheld} for p in self.profiles
            ],
            "skipped": self.skipped,
            "removed": self.removed,
            "warnings": self.warnings,
        }


def _source_dirs(source: Path) -> list[Path]:
    """A single profile folder, or every profile folder under a root."""
    if (source / PROFILE_DOCUMENT).is_file():
        return [source]
    return profile_dirs(source)


def _check_out(out: Path) -> dict[str, Any] | None:
    """The previous run's marker, or ``None`` for an absent or empty ``out``.

    Raises when ``out`` holds something this command did not write: the export
    owns its folder, and removing files from a folder it does not own is how a
    publish deletes somebody's work.
    """
    if not out.exists():
        return None
    if not out.is_dir():
        raise PublishError(f"output is not a directory: {out}")
    marker = out / MARKER
    if marker.is_file():
        try:
            return json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise PublishError(f"unreadable {MARKER} in {out}: {e}") from e
    if any(out.iterdir()):
        raise PublishError(
            f"{out} is not empty and was not written by rp publish (no {MARKER}). "
            "Pick an empty or new folder."
        )
    return None


def _stage_embeddings(
    stage: _SiteStage, prof_dir: Path, prof: ResearcherProfile, viewer: ViewerTier, base: str
) -> list[str] | None:
    """Export ``embeddings/`` at ``viewer`` from the local sqlite into the stage.

    Returns the profile-relative paths staged, or ``None`` when there is no
    local index to export from, in which case the profile's own (``public``)
    flat files ship through the plan like any other artifact.
    """
    if not (cache_dir(prof_dir) / "embeddings.sqlite").is_file():
        return None
    from ..embeddings.flat import write_flat_export

    with tempfile.TemporaryDirectory(prefix=".rp-publish-emb-") as tmp:
        try:
            result = write_flat_export(
                prof_dir, profile_document=prof.metadata, viewer=viewer, out_dir=tmp
            )
        except RuntimeError as e:
            # No stored probe: fall back to the profile's own public flat files.
            logger.warning("embeddings for %s not re-exported at %s: %s", prof.slug, viewer, e)
            return None
        if result is None:
            return []
        written: list[str] = []
        for f in sorted(Path(tmp).iterdir()):
            rel = f"embeddings/{f.name}"
            stage.copy(f"{base}/{rel}", f)
            written.append(rel)
        return written


def publish_collection(
    source: str | os.PathLike,
    out: str | os.PathLike,
    *,
    viewer: ViewerTier = "public",
    base_url: str | None = None,
    no_index: bool = False,
    now: str | None = None,
    dry_run: bool = False,
) -> PublishResult:
    """Write the static tree an audience at ``viewer`` may see into ``out``.

    ``source`` is a profiles root or one profile folder. For every profile the
    viewer may see (``privacy.profile_visible``), ``profiles/<slug>/`` gets the
    projected ``profile.jsonld``, each artifact whose effective tier the viewer
    may see, ``index.html`` rendered at ``viewer``, and ``embeddings/`` exported
    at ``viewer``. The collection files (:func:`build_site`) are built for the
    same audience into the same tree.

    ``out`` is owned by this command. A non-empty folder without the
    ``.rp-publish.json`` marker is refused. A re-run removes every file the
    previous run wrote that this one does not, so a tier tightened since the
    last export disappears from the tree. Everything is staged first and
    committed together; if this raises, ``out`` is as it was.

    Refuses (:class:`PublishError`) when any profile's ``derivedFrom`` graph
    does not resolve: a restriction that cannot be computed is not guessed at.
    ``dry_run`` decides everything and writes nothing.
    """
    src = Path(source).expanduser().resolve()
    dest = Path(out).expanduser().resolve()
    if not src.is_dir():
        raise FileNotFoundError(f"no such profiles folder: {src}")
    previous = _check_out(dest)
    timestamp = now_iso(now)
    result = PublishResult(out_dir=dest, viewer=viewer, dry_run=dry_run)

    # ---- decide: load, gate, and check every profile before writing --------
    visible: list[tuple[Path, ResearcherProfile]] = []
    broken: list[str] = []
    for entry in _source_dirs(src):
        try:
            prof = ResearcherProfile.from_files(entry)
        except ProfileLoadError as e:
            result.warnings.append(f"Skipping {entry.name}: {e}")
            continue
        errors = derivation_errors(prof.metadata)
        if errors:
            broken.extend(f"{prof.slug}: {e}" for e in errors)
            continue
        if not profile_visible(prof.metadata, viewer):
            result.skipped[prof.slug] = (
                f"profile visibility is {prof.metadata.visibility}, above {viewer}"
            )
            continue
        visible.append((entry, prof))
    if broken:
        raise PublishError(
            "refusing to publish: a derivedFrom chain does not resolve, so a "
            "restriction cannot be computed.\n  " + "\n  ".join(broken)
        )

    plans = {prof.slug: plan_profile_export(entry, viewer) for entry, prof in visible}
    if dry_run:
        for _entry, prof in visible:
            plan = plans[prof.slug]
            result.profiles.append(
                ProfileExport(
                    slug=prof.slug,
                    files=sorted({PROFILE_DOCUMENT, "index.html", *plan.files}),
                    withheld=plan.withheld,
                )
            )
        return result

    # ---- write: stage everything, then commit -------------------------------
    created = _missing_ancestors(dest)
    dest.mkdir(parents=True, exist_ok=True)
    stage = _SiteStage(dest)
    page_no_index = no_index or viewer != "public"
    try:
        try:
            written: list[str] = []
            for entry, prof in visible:
                plan = plans[prof.slug]
                base = f"profiles/{prof.slug}"
                export = ProfileExport(slug=prof.slug, withheld=dict(plan.withheld))
                stage.write(f"{base}/{PROFILE_DOCUMENT}", plan.document)
                export.files.append(PROFILE_DOCUMENT)

                embeddings = _stage_embeddings(stage, entry, prof, viewer, base)
                for rel in plan.files:
                    if rel == "index.html":
                        continue
                    if embeddings is not None and rel.startswith("embeddings/"):
                        continue
                    stage.copy(f"{base}/{rel}", entry / rel)
                    export.files.append(rel)
                export.files.extend(embeddings or [])

                html = render_page(prof, viewer, base_url=base_url, no_index=page_no_index)
                stage.write(f"{base}/index.html", html)
                export.files.append("index.html")
                export.files.sort()
                written.extend(f"{base}/{rel}" for rel in export.files)
                result.profiles.append(export)

            site = SiteResult(out_dir=dest)
            _build_into(stage, [e for e, _ in visible], site, timestamp, base_url, no_index, viewer)
            result.warnings.extend(site.warnings)
            written.extend(site.files)

            stage.write(
                MARKER,
                json.dumps(
                    {
                        "who": viewer,
                        "rp_version": _rp_version(),
                        "published_at": timestamp,
                        "slugs": sorted(p.slug for p in result.profiles),
                        "files": sorted(written),
                    },
                    indent=2,
                )
                + "\n",
            )
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

    result.removed = _remove_stale(dest, previous, set(written))
    return result


def _remove_stale(out: Path, previous: dict[str, Any] | None, written: set[str]) -> list[str]:
    """Delete the files the previous export wrote that this one did not.

    Only paths the previous marker lists are candidates, so nothing this
    command did not write is ever touched. Directories left empty go too.
    """
    if not previous:
        return []
    removed: list[str] = []
    for rel in previous.get("files") or []:
        if rel in written or rel == MARKER:
            continue
        path = (out / rel).resolve()
        if not path.is_relative_to(out) or not path.is_file():
            continue
        path.unlink()
        removed.append(rel)
        parent = path.parent
        while parent != out:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return sorted(removed)


def _rp_version() -> str:
    from .. import __version__

    return str(__version__)


__all__ = ["MARKER", "ProfileExport", "PublishError", "PublishResult", "publish_collection"]
