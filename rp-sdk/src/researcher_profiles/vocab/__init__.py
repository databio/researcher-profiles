"""Pinned copies of the interest vocabularies: OpenAlex topics and MeSH.

A stored ``ResearchInterest`` concept is ``{system, code, display, version}``.
For a code to mean the same thing in every tool that reads it (rp-builder,
Prosopia, RADAR), every tool resolves it against the same snapshot, and that
snapshot is this package's data:

- ``openalex_topics.json``: the OpenAlex topic list (``T…`` ids, with
  subfield, field and domain). OpenAlex publishes no release number, so the
  release is the snapshot date of this copy (``2026-09``).
- ``mesh_descriptors.json.gz``: MeSH descriptors (``D…`` ids, preferred
  label, tree numbers, entry terms). The release is the MeSH year.

``version`` on a concept is always the release of the copy it was looked up
in, never a term's own update date or a retrieval date.

Both files load lazily and once: importing this module reads nothing, so the
release constants are module attributes resolved on first access (PEP 562).
``rp vocab refresh`` rewrites the files (see :mod:`.refresh`).
"""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..schema import InterestConcept

#: The system URI for OpenAlex topics. OpenAlex defines no identifier prefix
#: for its topic list as a scheme, so this is the format's choice.
OPENALEX_SYSTEM = "https://openalex.org/topics"
#: The system URI for MeSH descriptors. MeSH's own term IRIs use ``http``.
MESH_SYSTEM = "http://id.nlm.nih.gov/mesh"

OPENALEX_FILE = "openalex_topics.json"
MESH_FILE = "mesh_descriptors.json.gz"

__all__ = [
    "MESH_FILE",
    "MESH_RELEASE",
    "MESH_SYSTEM",
    "OPENALEX_FILE",
    "OPENALEX_RELEASE",
    "OPENALEX_SYSTEM",
    "concept_iri",
    "load_mesh",
    "load_openalex_topics",
    "lookup",
    "search",
]


def _read(name: str) -> bytes:
    return resources.files(__name__).joinpath(name).read_bytes()


@lru_cache(maxsize=1)
def load_openalex_topics() -> dict[str, Any]:
    """``{"release", "system", "topics": [{id, display_name, subfield, field, domain}]}``."""
    return json.loads(_read(OPENALEX_FILE))


@lru_cache(maxsize=1)
def load_mesh() -> dict[str, Any]:
    """``{"release", "system", "descriptors": [{id, label, tree_numbers, synonyms}]}``."""
    return json.loads(gzip.decompress(_read(MESH_FILE)))


@lru_cache(maxsize=2)
def _index(system: str) -> dict[str, tuple[str, list[str]]]:
    """code -> (label, synonyms) for one system."""
    if system == OPENALEX_SYSTEM:
        return {t["id"]: (t["display_name"], []) for t in load_openalex_topics()["topics"]}
    if system == MESH_SYSTEM:
        return {d["id"]: (d["label"], d.get("synonyms", [])) for d in load_mesh()["descriptors"]}
    raise ValueError(f"unknown vocabulary system {system!r}")


def _release(system: str) -> str:
    return (load_openalex_topics() if system == OPENALEX_SYSTEM else load_mesh())["release"]


def concept_iri(system: str, code: str) -> str | None:
    """The term's own IRI: ``https://openalex.org/T…`` or ``http://id.nlm.nih.gov/mesh/D…``."""
    if system == OPENALEX_SYSTEM:
        return f"https://openalex.org/{code}"
    if system == MESH_SYSTEM:
        return f"{MESH_SYSTEM}/{code}"
    return None


def _normalize_code(system: str, code: str) -> str:
    """Bare, upper-case code: ``https://openalex.org/t10222`` -> ``T10222``."""
    code = str(code).strip().rstrip("/").rsplit("/", 1)[-1]
    return code.upper() if system == OPENALEX_SYSTEM else code


def lookup(system: str, code: str) -> InterestConcept | None:
    """The coded concept for ``code`` in the pinned copy, or ``None`` if unknown.

    ``@id``, ``display`` and ``version`` all come from the pinned file, so two
    tools that look up the same code build the same concept.
    """
    from ..schema import InterestConcept

    bare = _normalize_code(system, code)
    hit = _index(system).get(bare)
    if hit is None:
        return None
    return InterestConcept.model_validate(
        {
            "@id": concept_iri(system, bare),
            "system": system,
            "code": bare,
            "display": hit[0],
            "version": _release(system),
        }
    )


def search(system: str, query: str, limit: int = 20) -> list[InterestConcept]:
    """Concepts whose label (or, for MeSH, entry term) matches ``query``.

    Lexical only, best first: an exact label, then a label that starts with the
    query, then every query word present in the label or an entry term.
    """
    from ..schema import InterestConcept

    q = query.casefold().strip()
    if not q:
        return []
    words = set(q.split())
    ranked: list[tuple[int, str, str]] = []
    for code, (label, synonyms) in _index(system).items():
        low = label.casefold()
        if low == q:
            rank = 0
        elif low.startswith(q):
            rank = 1
        elif words <= set(low.replace(",", " ").split()):
            rank = 2
        elif any(q == s.casefold() for s in synonyms):
            rank = 3
        elif q in low or any(q in s.casefold() for s in synonyms):
            rank = 4
        else:
            continue
        ranked.append((rank, low, code))
    ranked.sort()
    release = _release(system)
    return [
        InterestConcept.model_validate(
            {
                "@id": concept_iri(system, code),
                "system": system,
                "code": code,
                "display": _index(system)[code][0],
                "version": release,
            }
        )
        for _, _, code in ranked[:limit]
    ]


def __getattr__(name: str) -> str:
    """``OPENALEX_RELEASE`` / ``MESH_RELEASE``, read from the pinned files on first access."""
    if name == "OPENALEX_RELEASE":
        return load_openalex_topics()["release"]
    if name == "MESH_RELEASE":
        return load_mesh()["release"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
