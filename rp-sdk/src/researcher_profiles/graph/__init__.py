"""The derived profile graph: coauthor / shared-institution / advised edges.

A rebuildable, registry-level artifact over the profile store, modeled on the
embeddings/centroid precedent. It is derived purely from already-published
bibliometric fields (author lists, affiliations, training records), no LLM, no
paper full text, no persona, no consent gate, so it works against the whole
store, ``lite`` profiles included.

Public API
----------

- :class:`ProfileGraph`: the query object (COI check, neighborhood, coauthors)
- :func:`build_graph`: pure builder over a set of loaded profiles
- :func:`load_profiles`: the graph's lightweight profile loader (no numpy)
- :class:`PersonNode`, :class:`GraphEdge`, :class:`EdgeType`: the value objects
- :func:`normalize_name`: the single name-fold the builder and COI check share

The built graph persists at ``<root>/.cache/graph.sqlite`` and is a cache:
deleting it is free; the next query rebuilds it.
"""

from .build import build_graph, load_profiles
from .cache import graph_db_path, unlink_graph
from .graph import CoiReason, CoiVerdict, Neighbor, ProfileGraph
from .identity import NameIndex, normalize_institution, normalize_name, paper_identity_key
from .model import (
    DIRECTED_TYPES,
    EdgeType,
    GraphEdge,
    InstitutionRef,
    PersonNode,
    canonical_pair,
)

__all__ = [
    "ProfileGraph",
    "CoiReason",
    "CoiVerdict",
    "Neighbor",
    "build_graph",
    "load_profiles",
    "EdgeType",
    "GraphEdge",
    "PersonNode",
    "InstitutionRef",
    "DIRECTED_TYPES",
    "canonical_pair",
    "NameIndex",
    "normalize_name",
    "normalize_institution",
    "paper_identity_key",
    "graph_db_path",
    "unlink_graph",
]
