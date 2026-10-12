"""Export a set of profiles as the static tree one audience may see."""

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..errors import ProfileError, ProfileLoadError
from ..privacy import ViewerTier, derivation_errors, profile_visible, tier_allows
from ..profile import ResearcherProfile
from ..schema.jsonld import canonical_dumps
from ..utils.clock import now_iso
from ..utils.paths import cache_dir
from ._export import PROFILE_DOCUMENT, ExportPlan, plan_profile_export
from ._render import render_page
from ._site import SiteResult, _build_into, _missing_ancestors, _SiteStage, profile_dirs

logger = logging.getLogger(__name__)

#: Written into ``out`` on success. Its presence is what lets a later run
#: replace the folder's contents; its file list is what lets that run remove
#: the files a tightened tier no longer allows, and nothing else.
MARKER = ".rp-publish.json"


_TIERS = ("public", "limited", "private")


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
    #: In a dry run, the paths a real run would remove.
    would_remove: list[str] = field(default_factory=list)
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
            **({"would_remove": self.would_remove} if self.dry_run else {}),
            "warnings": self.warnings,
        }


def _source_dirs(source: Path) -> list[Path]:
    """A single profile folder, or every profile folder under a root."""
    if (source / PROFILE_DOCUMENT).is_file():
        return [source]
    return profile_dirs(source)


def _check_out(out: Path) -> dict[str, Any] | None:
    """The previous run's marker, or ``None`` for an absent or empty ``out``.

    Raises when ``out`` holds something this command did not write, so a
    publish never deletes somebody else's files.
    """
    if not out.exists():
        return None
    if not out.is_dir():
        raise PublishError(f"output is not a directory: {out}")
    marker = out / MARKER
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise PublishError(f"unreadable {MARKER} in {out}: {e}") from e
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise PublishError(
                f"unreadable {MARKER} in {out}: expected an object with a list of "
                "file paths under 'files'"
            )
        return data
    if any(out.iterdir()):
        raise PublishError(
            f"{out} is not empty and was not written by rp publish (no {MARKER}). "
            "Pick an empty or new folder."
        )
    return None


def _stage_embeddings(
    stage: _SiteStage,
    prof_dir: Path,
    prof: ResearcherProfile,
    viewer: ViewerTier,
    base: str,
    centroids: dict[str, tuple[str, Any, dict | None]],
) -> list[str] | None:
    """Export ``embeddings/`` at ``viewer`` from the local sqlite into the stage.

    Returns the profile-relative paths staged, or ``None`` when there is no
    local index (then the profile's own ``public`` flat files ship through the
    plan). Records the centroid of the staged rows in ``centroids``.
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
        centroids[prof.slug] = (result.backend_spec, result.centroid, result.probe)
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
    change_audience: bool = False,
) -> PublishResult:
    """Write the static tree an audience at ``viewer`` may see into ``out``.

    ``source`` is a profiles root or one profile folder. Each visible profile
    gets ``profiles/<slug>/`` projected through :func:`plan_profile_export`,
    with its page and embeddings rendered at ``viewer``; the collection files
    (:func:`build_site`) are built for the same audience.

    ``out`` is owned by this command: a non-empty folder without the
    ``.rp-publish.json`` marker is refused, and a re-run removes every file the
    previous run wrote that this one does not, so a tightened tier disappears.
    Everything is staged and committed together; if this raises, ``out`` is
    as it was.

    Raises :class:`PublishError` when any profile does not load (leaving it out
    would prune its last published copy) or when a ``derivedFrom`` graph does
    not resolve (a restriction is never guessed at). ``dry_run`` reports what
    would be written and pruned (``would_remove``) without touching ``out``.

    Re-publishing into a folder last written for a narrower audience (say
    ``public`` then ``limited``) is refused unless ``change_audience`` is true;
    narrowing is always allowed.
    """
    src = Path(source).expanduser().resolve()
    dest = Path(out).expanduser().resolve()
    if not src.is_dir():
        raise FileNotFoundError(f"no such profiles folder: {src}")
    previous = _check_out(dest)
    prev_who = (previous or {}).get("who")
    if (
        prev_who in _TIERS
        and not change_audience
        and tier_allows(viewer, prev_who)
        and not tier_allows(prev_who, viewer)
    ):
        raise PublishError(
            f"{dest} was last published for --who {prev_who}; --who {viewer} would "
            "widen its audience. Pass change_audience=True (--change-audience) "
            "if that is intended."
        )
    timestamp = now_iso(now)
    result = PublishResult(out_dir=dest, viewer=viewer, dry_run=dry_run)

    # ---- decide: load, gate, and check every profile before writing --------
    visible: list[tuple[Path, ResearcherProfile]] = []
    failed: list[str] = []
    broken: list[str] = []
    for entry in _source_dirs(src):
        try:
            prof = ResearcherProfile.from_files(entry)
            document = prof.metadata  # loads lazily: read it here, inside the guard
        except (ProfileLoadError, ValidationError) as e:
            failed.append(f"{entry.name}: {e}")
            continue
        errors = derivation_errors(document)
        if errors:
            broken.extend(f"{prof.slug}: {e}" for e in errors)
            continue
        if not profile_visible(prof.metadata, viewer):
            result.skipped[prof.slug] = (
                f"profile visibility is {prof.metadata.visibility}, above {viewer}"
            )
            continue
        visible.append((entry, prof))

    plans: dict[str, ExportPlan] = {}
    for entry, prof in visible:
        try:
            plans[prof.slug] = plan_profile_export(entry, viewer)
        except ValidationError as e:
            failed.append(f"{entry.name}: {e}")
    if failed:
        # Skipping would prune its live copy.
        raise PublishError(
            "refusing to publish: a profile does not load. Fix it (rp validate) "
            "or move it out of the profiles folder.\n  " + "\n  ".join(failed)
        )
    if broken:
        raise PublishError(
            "refusing to publish: a derivedFrom chain does not resolve, so a "
            "restriction cannot be computed.\n  " + "\n  ".join(broken)
        )

    if dry_run:
        scratch = Path(tempfile.mkdtemp(prefix=".rp-publish-dry-"))
        stage = _SiteStage(scratch)
        try:
            written = _stage_all(stage, visible, plans, result, timestamp, base_url, no_index)
        finally:
            stage.close()
            shutil.rmtree(scratch, ignore_errors=True)
        result.would_remove = _stale(dest, previous, set(written))
        return result

    # ---- write: stage everything, then commit -------------------------------
    created = _missing_ancestors(dest)
    dest.mkdir(parents=True, exist_ok=True)
    stage = _SiteStage(dest)
    slugs: list[str] = []
    try:
        try:
            written = _stage_all(stage, visible, plans, result, timestamp, base_url, no_index)
            slugs = sorted(p.slug for p in result.profiles)
            # Carry the previous run's files, so a crash before pruning
            # finishes does not orphan them.
            carried = set((previous or {}).get("files", []))
            stage.write(MARKER, _marker(viewer, timestamp, slugs, set(written) | carried))
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
    tmp = dest / f"{MARKER}.tmp"
    tmp.write_text(_marker(viewer, timestamp, slugs, set(written)), encoding="utf-8")
    os.replace(tmp, dest / MARKER)
    return result


def _stage_all(
    stage: _SiteStage,
    visible: list[tuple[Path, ResearcherProfile]],
    plans: dict[str, ExportPlan],
    result: PublishResult,
    timestamp: str,
    base_url: str | None,
    no_index: bool,
) -> list[str]:
    """Stage every profile folder and the collection files; return the paths."""
    viewer = result.viewer
    page_no_index = no_index or viewer != "public"
    written: list[str] = []
    centroids: dict[str, tuple[str, Any, dict | None]] = {}
    for entry, prof in visible:
        plan = plans[prof.slug]
        base = f"profiles/{prof.slug}"
        export = ProfileExport(slug=prof.slug, withheld=dict(plan.withheld))

        # Chunk-level filtering is not a substitute for the artifact's own gate.
        embeddings = None
        if plan.embeddings:
            embeddings = _stage_embeddings(stage, entry, prof, viewer, base, centroids)
            if embeddings is None:
                _profile_centroid(entry, prof, centroids, result)
        document = plan.document
        if embeddings == []:
            # No row survived filtering at this tier: say so in the document.
            doc = json.loads(document)
            doc["hasEmbeddingIndex"] = False
            document = canonical_dumps(doc).encode("utf-8")
        stage.write(f"{base}/{PROFILE_DOCUMENT}", document)
        export.files.append(PROFILE_DOCUMENT)

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

    site = SiteResult(out_dir=result.out_dir)
    _build_into(
        stage, [e for e, _ in visible], site, timestamp, base_url, no_index, viewer, centroids
    )
    result.warnings.extend(site.warnings)
    written.extend(site.files)
    return written


def _profile_centroid(
    entry: Path,
    prof: ResearcherProfile,
    centroids: dict[str, tuple[str, Any, dict | None]],
    result: PublishResult,
) -> None:
    """Record the centroid of the profile's own flat files, which ship as is."""
    from ._centroids import _collect_centroid

    entries: list[tuple[str, str, Any, dict | None]] = []
    _collect_centroid(entry, prof, entries, result, result.viewer)  # type: ignore[arg-type]
    for slug, backend, vec, probe in entries:
        centroids[slug] = (backend, vec, probe)


def _marker(viewer: ViewerTier, timestamp: str, slugs: list[str], files: set[str]) -> str:
    """The text of ``.rp-publish.json``."""
    return (
        json.dumps(
            {
                "who": viewer,
                "rp_version": _rp_version(),
                "published_at": timestamp,
                "slugs": slugs,
                "files": sorted(files),
            },
            indent=2,
        )
        + "\n"
    )


def _stale(out: Path, previous: dict[str, Any] | None, written: set[str]) -> list[str]:
    """The files the previous export wrote that this one does not.

    Only paths the previous marker lists are candidates.
    """
    if not previous:
        return []
    stale: list[str] = []
    for rel in previous.get("files") or []:
        if rel in written or rel == MARKER:
            continue
        path = (out / rel).resolve()
        if path.is_relative_to(out) and path.is_file():
            stale.append(rel)
    return sorted(stale)


def _remove_stale(out: Path, previous: dict[str, Any] | None, written: set[str]) -> list[str]:
    """Delete :func:`_stale` files, and the directories they leave empty."""
    removed = _stale(out, previous, written)
    for rel in removed:
        path = (out / rel).resolve()
        path.unlink()
        parent = path.parent
        while parent != out:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return removed


def _rp_version() -> str:
    from .. import __version__

    return str(__version__)


__all__ = ["MARKER", "ProfileExport", "PublishError", "PublishResult", "publish_collection"]
