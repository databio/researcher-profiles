"""The derived profile graph: coauthor / shared-institution / advised edges.

Derived only from published bibliometric fields (author lists, affiliations,
training records), with no LLM, full text, persona or consent gate, so it
covers the whole store, ``lite`` profiles included.
"""

from .build import build_graph, load_profiles
from .cache import graph_db_path
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
]
