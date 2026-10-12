"""What one audience may receive from one profile: the shared egress projection.

Both the HTTP archive and the static export build from
:func:`plan_profile_export`, so they cannot disagree about what a tier may see.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..privacy import (
    TierExplanation,
    ViewerTier,
    explain_tiers,
    project_document,
    tier_allows,
)
from ..schema import ProfileDocument
from ..schema.jsonld import canonical_dumps

PROFILE_DOCUMENT = "profile.jsonld"

#: Directories whose files are parts of one manifest artifact rather than
#: entries of their own. The flat embedding blob and chunk metadata are named
#: by ``embeddings/index.json`` (its ``file`` field), so they travel at its tier.
_EMBEDDING_INDEX = "embeddings/index.json"
_COMPANION_DIRS: dict[str, str] = {"embeddings/": _EMBEDDING_INDEX}


@dataclass(frozen=True)
class ExportPlan:
    """What a viewer at one tier receives from one profile folder."""

    #: The projected, canonical ``profile.jsonld``. Never the bytes on disk:
    #: inline sections carry their own tiers, and a file list cannot redact a
    #: field out of a document.
    document: bytes
    #: Profile-relative paths that ship, sorted. Directories are implied.
    files: list[str]
    #: Profile-relative paths on disk that do not ship, each with the reason.
    withheld: dict[str, str] = field(default_factory=dict)
    #: Whether the ``embeddings/index.json`` artifact reaches this viewer.
    embeddings: bool = False


def _walk(profile_dir: Path) -> Iterator[str]:
    """Every regular file under ``profile_dir`` whose path has no dot part."""
    for dirpath, dirnames, filenames in os.walk(profile_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        base = Path(dirpath).relative_to(profile_dir)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            yield (base / name).as_posix()


def _withheld_reason(entry: TierExplanation) -> str:
    """The sentence a dry run prints for an artifact above the viewer's tier."""
    if entry.raised_by:
        return f"{entry.effective}: " + "; ".join(entry.raised_by)
    return f"{entry.effective}: declared {entry.declared}"


def plan_profile_export(profile_dir: str | os.PathLike, viewer: ViewerTier) -> ExportPlan:
    """Decide what a viewer entitled to ``viewer`` receives from one profile.

    Rules, in order:

    * ``profile.jsonld`` always ships, projected through
      :func:`~researcher_profiles.privacy.project_document`; its manifest is
      tier-invariant (spec section 6), its inline sections are not;
    * a path with any dot part (``.cache/``, ``.keys/``, dotfiles) never ships;
    * a file the manifest does not list never ships: it is not part of the
      published record. The one exception is a file under ``embeddings/``,
      which is part of the ``embeddings/index.json`` artifact and takes its
      tier;
    * a manifest artifact ships when ``tier_allows(viewer, effective_tier)``;
    * ``hasEmbeddingIndex`` in the projected document is true only when
      ``embeddings/index.json`` ships.

    The whole-profile gate (``privacy.profile_visible``) is the caller's job: it
    decides whether this profile reaches the viewer at all.
    """
    src = Path(profile_dir).expanduser().resolve()
    doc_path = src / PROFILE_DOCUMENT
    if not doc_path.is_file():
        raise FileNotFoundError(f"not a profile directory (no {PROFILE_DOCUMENT}): {src}")
    profile = ProfileDocument.model_validate_json(doc_path.read_bytes())
    explain = explain_tiers(profile)

    files: list[str] = []
    withheld: dict[str, str] = {}
    for rel in _walk(src):
        if rel == PROFILE_DOCUMENT:
            continue
        entry = explain.get(rel)
        if entry is None:
            entry = next(
                (explain.get(owner) for d, owner in _COMPANION_DIRS.items() if rel.startswith(d)),
                None,
            )
        if entry is None:
            withheld[rel] = "not in the manifest"
        elif tier_allows(viewer, entry.effective):
            files.append(rel)
        else:
            withheld[rel] = _withheld_reason(entry)

    emb = explain.get(_EMBEDDING_INDEX)
    embeddings = emb is not None and tier_allows(viewer, emb.effective)
    projected = project_document(profile, viewer)
    if not embeddings and projected.has_embedding_index:
        projected.has_embedding_index = False
    document = canonical_dumps(projected.model_dump(mode="json")).encode("utf-8")
    return ExportPlan(document=document, files=files, withheld=withheld, embeddings=embeddings)


__all__ = ["ExportPlan", "plan_profile_export"]
