"""Generate the ``profile.jsonld`` manifest by walking a profile directory.

Without a manifest, an agent landing on a published directory would have to
guess at ``personality/SOUL.md``, ``sources/summaries/*.md``,
``.cache/embeddings.sqlite``. The manifest enumerates what a profile contains:
every artifact gets one typed entry with a name, an encoding format, a role,
and a **relative** ``contentUrl``.

Relative is the whole point: a profile is a directory of static files that must
stay portable across servers. An absolute URL would bind a published profile to
the host that happened to build it.

What is not in the manifest: the build-session bookkeeping
(``build_state.json`` and its kin) and the append-only usage logs beside it.
Both sit in the build root (``.build/<slug>/``), outside the content tree. From the
profile-adjacent ``.cache/`` directory only the embedding index is listed; the
other derived caches (the cluster-method ``topics.json``, ``calibration.json``,
…) are regenerable and never part of the record. LLM-labeled topics are not a
cache and are listed, as ``personality/topics.json``.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..utils.paths import CACHE_DIRNAME
from ._parts import ArtifactRef

MARKDOWN = "text/markdown"
JSONLD = "application/ld+json"
JSON = "application/json"
SQLITE = "application/vnd.sqlite3"

#: Files under ``.cache/`` listed in the manifest. ``embeddings.sqlite`` is a
#: derived index served at tier ``restricted``; the public servable embeddings
#: are the flat blob/index/chunks under ``embeddings/``.
PUBLISHED_CACHE = ("embeddings.sqlite",)

_SUMMARY_SUFFIX = ".summary.md"


#: Encoding formats whose bodies are TEXT. Everything else is binary, and a
#: store may legitimately hold a binary artifact's manifest entry without its
#: bytes (``ProfileStore.put(..., include_binary=True)`` is opt-in because
#: ``.cache/embeddings.sqlite`` runs to tens of megabytes).
TEXT_FORMATS: frozenset[str] = frozenset(
    {
        "text/markdown",
        "text/plain",
        "text/html",
        "application/json",
        "application/ld+json",
    }
)


def is_text_artifact(encoding_format: str | None, content_url: str) -> bool:
    """Whether an artifact's body is text. The one definition of that question.

    The SQL backend asks it to pick which column a body lands in, and any
    other caller must get the same answer, so the list of media types lives
    here and nowhere else.
    """
    if encoding_format in TEXT_FORMATS:
        return True
    if encoding_format:
        return False
    return content_url.endswith((".md", ".json", ".jsonld", ".html", ".txt", ".yaml", ".yml"))


def _part(
    content_url: str,
    *,
    name: str,
    role: str,
    encoding_format: str,
    type_: str = "DigitalDocument",
    paper_id: str | None = None,
) -> ArtifactRef:
    # The ArtifactRef model applies the role's default tier (cv/web/full-text ->
    # restricted) when `visibility` is not passed, so it is omitted here.
    return ArtifactRef(
        type_=type_,
        name=name,
        encoding_format=encoding_format,
        content_url=content_url,
        role=role,
        paper_id=paper_id,
    )


def _stamp(part: ArtifactRef, root: Path) -> ArtifactRef:
    """Populate ``bytes`` and ``sha256`` from the file on disk, when present."""
    f = root / part.content_url
    if f.is_file():
        data = f.read_bytes()
        part.bytes = len(data)
        part.sha256 = hashlib.sha256(data).hexdigest()
    return part


@dataclass(frozen=True)
class _FileSpec:
    """One optional file at a fixed relative path, and how it is described."""

    rel: str
    name: str
    role: str
    encoding_format: str
    type_: str = "DigitalDocument"


@dataclass(frozen=True)
class _DirSpec:
    """A directory scanned for files with a common suffix, sorted by name."""

    rel_dir: str
    suffix: str
    role: str
    name_prefix: str
    with_paper_id: bool


#: Persona documents: things *about* the person, carried in ``subjectOf``.
_SUBJECT_SPECS: tuple[_FileSpec, ...] = (
    _FileSpec("personality/SOUL.md", "SOUL", "soul", MARKDOWN),
    _FileSpec("personality/expertise.md", "Expertise", "expertise", MARKDOWN),
    _FileSpec("personality/topics.json", "Research topics", "topics", JSON),
)

#: The record and its sources, carried in ``hasPart``.
_PART_SPECS: tuple[_FileSpec, ...] = (
    _FileSpec("index.html", "Profile page", "html", "text/html"),
    _FileSpec("SKILL.md", "Agent entry point", "agent_entry_point", MARKDOWN),
    _FileSpec("sources/papers.jsonld", "Works", "works", JSONLD, type_="Collection"),
    _FileSpec("sources/grants.jsonld", "Grants", "grants", JSONLD, type_="Collection"),
    _FileSpec("sources/citations.json", "Citations", "citations", JSON),
    _FileSpec("sources/cv.md", "CV", "cv", MARKDOWN),
)

_DIR_SPECS: tuple[_DirSpec, ...] = (
    _DirSpec("sources/summaries", _SUMMARY_SUFFIX, "paper_summary", "Summary: ", True),
    _DirSpec("sources/papers", ".md", "paper_fulltext", "Full text: ", True),
    _DirSpec("sources/web", ".md", "web", "Web page: ", False),
)

#: Flat servable embeddings (public artifacts). Kept out of ``_PART_SPECS``
#: because the manifest lists it after the directory scans, and that order is
#: part of the published bytes.
_INDEX_SPECS: tuple[_FileSpec, ...] = (
    _FileSpec(
        "embeddings/index.json",
        "Embedding index",
        "embedding_index",
        JSON,
        type_="DataDownload",
    ),
)


def _present_parts(root: Path, specs: Sequence[_FileSpec]) -> list[ArtifactRef]:
    """A part for each spec whose file exists, in spec order."""
    return [
        _part(
            spec.rel,
            name=spec.name,
            role=spec.role,
            encoding_format=spec.encoding_format,
            type_=spec.type_,
        )
        for spec in specs
        if (root / spec.rel).is_file()
    ]


def _dir_parts(root: Path, spec: _DirSpec) -> list[ArtifactRef]:
    """A part for each matching file in one scanned directory, sorted by name."""
    directory = root / spec.rel_dir
    if not directory.is_dir():
        return []
    out: list[ArtifactRef] = []
    for f in sorted(directory.iterdir()):
        if not (f.is_file() and f.name.endswith(spec.suffix)):
            continue
        stem = f.name[: -len(spec.suffix)]
        out.append(
            _part(
                f"{spec.rel_dir}/{f.name}",
                name=f"{spec.name_prefix}{stem}",
                role=spec.role,
                encoding_format=MARKDOWN,
                paper_id=stem if spec.with_paper_id else None,
            )
        )
    return out


def _cache_parts(root: Path) -> list[ArtifactRef]:
    """The derived sqlite index (tier restricted): reachable by an authorized
    consumer, excluded from a public sync. Profile-adjacent under ``.cache/``.
    """
    return [
        _part(
            f"{CACHE_DIRNAME}/{name}",
            name="Embedding index (local sqlite)",
            role="embedding_index_sqlite",
            encoding_format=SQLITE,
            type_="DataDownload",
        )
        for name in PUBLISHED_CACHE
        if (root / CACHE_DIRNAME / name).is_file()
    ]


def build_manifest(profile_dir: str | Path) -> tuple[list[ArtifactRef], list[ArtifactRef]]:
    """Return ``(hasPart, subjectOf)`` for the profile at ``profile_dir``.

    ``subjectOf`` carries the persona documents, things *about* the person.
    ``hasPart`` carries everything else: the record, its sources, the index.
    """
    root = Path(profile_dir)
    subjects = _present_parts(root, _SUBJECT_SPECS)
    parts = _present_parts(root, _PART_SPECS)
    for dir_spec in _DIR_SPECS:
        parts.extend(_dir_parts(root, dir_spec))
    parts.extend(_present_parts(root, _INDEX_SPECS))
    parts.extend(_cache_parts(root))

    parts = [_stamp(p, root) for p in parts]
    subjects = [_stamp(s, root) for s in subjects]
    return parts, subjects


def manifest_drift(profile_dir: str | Path, recorded: list[ArtifactRef]) -> dict[str, list[str]]:
    """Compare a recorded manifest against what is actually on disk.

    Returns ``{"missing": [...], "stale": [...]}``: ``missing`` is files
    present on disk but absent from the manifest, and ``stale`` is manifest
    entries whose file is gone.
    """
    parts, subjects = build_manifest(profile_dir)
    on_disk = {p.content_url for p in (*parts, *subjects)}
    in_manifest = {p.content_url for p in recorded}
    return {
        "missing": sorted(on_disk - in_manifest),
        "stale": sorted(in_manifest - on_disk),
    }


__all__ = [
    "PUBLISHED_CACHE",
    "TEXT_FORMATS",
    "build_manifest",
    "is_text_artifact",
    "manifest_drift",
]
