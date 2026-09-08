"""``ProfileGraph``: the query object over the derived profile graph.

Sits over a profiles root the way the vector analytics accessors sit over a
store: it loads (or builds) a derived cross-profile artifact, caches it on disk
under ``<root>/.cache/``, and answers relation queries. Where ``store.match``
answers "how well does this profile match a query", the graph answers "who is
related to whom, and how": the substrate for COI checks, COI-filtered reviewer
matching, and neighborhood/team-assembly queries.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from ..schema import validate_ref
from .identity import NameIndex, normalize_institution, resolve_descriptor
from .model import EdgeType, GraphEdge, PersonNode


@dataclass
class CoiReason:
    """One reason a candidate conflicts with a manuscript author."""

    author_key: str
    type: str  # "coauthor" | "shared_institution" | "advised"
    author_name: Optional[str] = None
    author_rid: Optional[str] = None
    # coauthor
    last_year: Optional[int] = None
    in_window: Optional[bool] = None
    paper_count: Optional[int] = None
    # shared_institution
    institution: Optional[str] = None
    overlap_years: Optional[bool] = None
    # advised
    direction: Optional[str] = None  # "candidate_advised_author" | "author_advised_candidate"
    confidence: str = "medium"


@dataclass
class CoiVerdict:
    has_coi: bool
    reasons: list[CoiReason] = field(default_factory=list)


@dataclass
class Neighbor:
    node: PersonNode
    hops: int
    edges: list[GraphEdge]


def _current_year() -> int:
    return datetime.now(timezone.utc).year


class ProfileGraph:
    """A relation graph over the profiles under one root."""

    def __init__(
        self,
        *,
        nodes: Iterable[PersonNode],
        edges: Iterable[GraphEdge],
        meta: Optional[dict] = None,
        root: Optional[str | os.PathLike] = None,
    ):
        self.root = Path(root).expanduser().resolve() if root is not None else None
        self._nodes: dict[str, PersonNode] = {n.key: n for n in nodes}
        self._edges: list[GraphEdge] = list(edges)
        self.meta: dict = dict(meta or {})

        # Indexes.
        self._by_slug: dict[str, str] = {}
        self._name_index = NameIndex()
        for n in self._nodes.values():
            if n.slug:
                self._by_slug[n.slug] = n.key
            if n.kind == "profiled" and n.name and n.rid:
                self._name_index.add(n.name, n.rid)
        # Adjacency: key -> list[(neighbor_key, edge)]. Every edge contributes an
        # entry at both endpoints so an undirected neighbor is found from either.
        self._adj: dict[str, list[tuple[str, GraphEdge]]] = {}
        self._pair_index: dict[tuple[str, str], list[GraphEdge]] = {}
        for e in self._edges:
            self._adj.setdefault(e.src, []).append((e.dst, e))
            self._adj.setdefault(e.dst, []).append((e.src, e))
            self._pair_index.setdefault(self._pk(e.src, e.dst), []).append(e)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_store(
        cls, root: str | os.PathLike, *, profiles: Optional[Iterable] = None
    ) -> "ProfileGraph":
        """Load ``<root>/.cache/graph.sqlite``, or build and persist it.

        When ``profiles`` is given it is used for the build; otherwise the
        profiles under ``root`` are loaded fresh. The built graph is written back
        to the store so the next query is warm.
        """
        from . import cache as _store

        try:
            nodes, edges, meta = _store.load_graph(root)
            return cls(nodes=nodes, edges=edges, meta=meta, root=root)
        except FileNotFoundError:
            pass
        return cls.build_and_save(root, profiles=profiles)

    @classmethod
    def build_and_save(
        cls, root: str | os.PathLike, *, profiles: Optional[Iterable] = None
    ) -> "ProfileGraph":
        """Build the graph from profiles and persist it under ``root``."""
        from .build import build_graph, load_profiles

        profs = list(profiles) if profiles is not None else load_profiles(root)
        g = build_graph(profs)
        g.save(root)
        return g

    def save(self, root: Optional[str | os.PathLike] = None) -> Path:
        from . import cache as _store

        target = root if root is not None else self.root
        if target is None:
            raise ValueError("ProfileGraph.save needs a root (none stored)")
        self.root = Path(target).expanduser().resolve()
        return _store.save_graph(self.root, list(self._nodes.values()), self._edges, self.meta)

    # ------------------------------------------------------------------
    # Basics
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def nodes(self) -> list[PersonNode]:
        return list(self._nodes.values())

    @property
    def edges(self) -> list[GraphEdge]:
        return list(self._edges)

    @staticmethod
    def _pk(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def get_node(self, key: str) -> Optional[PersonNode]:
        return self._nodes.get(key)

    def resolve(self, ref: str) -> str:
        """Resolve a slug or rid to a node key, or raise ``KeyError``.

        Uses the same boundary contract (:func:`validate_ref`) as the rest of the
        API. A rid resolves to itself (it is the node key); a slug resolves
        through the slug index; an ORCID resolves as a rid.
        """
        ref = validate_ref(ref)
        if ref in self._nodes:
            return ref
        if ref in self._by_slug:
            return self._by_slug[ref]
        raise KeyError(f"no graph node for ref {ref!r}")

    # ------------------------------------------------------------------
    # Neighborhood queries
    # ------------------------------------------------------------------

    def edges_between(self, a_keys: Iterable[str], b_key: str) -> list[GraphEdge]:
        """Every edge directly connecting ``b_key`` to any key in ``a_keys``."""
        out: list[GraphEdge] = []
        for a in a_keys:
            out.extend(self._pair_index.get(self._pk(a, b_key), []))
        return out

    def coauthors(self, ref: str, *, since_year: Optional[int] = None) -> list[Neighbor]:
        """Direct coauthors of ``ref`` (1 hop), optionally filtered by recency."""
        return self.neighbors(ref, types=[EdgeType.coauthor], since_year=since_year, max_hops=1)

    def neighbors(
        self,
        ref: str,
        *,
        types: Optional[Iterable[EdgeType]] = None,
        since_year: Optional[int] = None,
        max_hops: int = 1,
    ) -> list[Neighbor]:
        """Neighbor nodes reachable within ``max_hops``, with connecting edges.

        ``max_hops=1`` gives direct neighbors; ``2`` also gives
        collaborators-of-collaborators (the origin itself is always excluded).
        Results are ordered by tie strength (paper_count, then recency), and each
        neighbor's ``hops`` is the fewest hops at which it was reached.
        """
        origin = self.resolve(ref)
        allowed = set(types) if types is not None else None

        def edge_ok(e: GraphEdge) -> bool:
            if allowed is not None and e.type not in allowed:
                return False
            if since_year is not None and e.type == EdgeType.coauthor:
                if e.last_year is None or e.last_year < since_year:
                    return False
            return True

        # BFS. Track the fewest hops to each node and the edges that connect it
        # to the previous frontier.
        best_hops: dict[str, int] = {origin: 0}
        conn_edges: dict[str, list[GraphEdge]] = {}
        frontier = {origin}
        for hop in range(1, max_hops + 1):
            next_frontier: set[str] = set()
            for node_key in frontier:
                for nbr_key, e in self._adj.get(node_key, []):
                    if not edge_ok(e):
                        continue
                    if nbr_key == origin:
                        continue
                    conn_edges.setdefault(nbr_key, []).append(e)
                    if nbr_key not in best_hops:
                        best_hops[nbr_key] = hop
                        next_frontier.add(nbr_key)
            frontier = next_frontier
            if not frontier:
                break

        out: list[Neighbor] = []
        for key, hops in best_hops.items():
            if key == origin:
                continue
            node = self._nodes.get(key)
            if node is None:
                continue
            edges = conn_edges.get(key, [])
            out.append(Neighbor(node=node, hops=hops, edges=edges))

        def strength(n: Neighbor) -> tuple:
            pc = max((e.paper_count for e in n.edges), default=0)
            recency = max((e.last_year or 0 for e in n.edges), default=0)
            return (-n.hops, pc, recency)

        out.sort(key=strength, reverse=True)
        return out

    # ------------------------------------------------------------------
    # COI
    # ------------------------------------------------------------------

    def resolve_author_descriptor(
        self,
        *,
        rid: Optional[str] = None,
        orcid: Optional[str] = None,
        name: Optional[str] = None,
    ) -> tuple[str, str]:
        """Resolve an inbound author descriptor to ``(node_key, kind)``.

        Delegates to the shared resolver so an incoming author is folded exactly
        the way the builder folded the corpus.
        """
        return resolve_descriptor(self._name_index, rid=rid, orcid=orcid, name=name)

    def coi_edges(
        self,
        author_descriptors: Iterable[dict],
        candidate: str,
        *,
        years: int = 4,
    ) -> CoiVerdict:
        """Every COI between ``candidate`` and any manuscript author.

        ``author_descriptors`` is a list of ``{name?, orcid?, rid?, affiliation?,
        affiliation_id?}`` dicts (the manuscript's authors, who may or may not have
        profiles). ``candidate`` is a slug or rid. ``years`` is the coauthor
        window in years. Returns a structured verdict.

        A same-institution conflict is caught for an external author (no profile)
        by comparing the affiliation passed inline against the candidate's own
        institution history. So a manuscript author with no profile in the registry
        still trips a shared-institution COI.
        """
        candidate_key = self.resolve(candidate)
        cutoff = _current_year() - years

        reasons: list[CoiReason] = []
        for desc in author_descriptors:
            try:
                author_key, _kind = self.resolve_author_descriptor(
                    rid=desc.get("rid"), orcid=desc.get("orcid"), name=desc.get("name")
                )
            except ValueError:
                continue
            if author_key == candidate_key:
                # Self-conflict is not a reviewer COI question; skip.
                continue
            reasons.extend(self._coi_reasons_for_author(candidate_key, author_key, desc, cutoff))

        has_coi = any(
            (r.type == "coauthor" and r.in_window) or r.type in ("shared_institution", "advised")
            for r in reasons
        )
        return CoiVerdict(has_coi=has_coi, reasons=reasons)

    def _coi_reasons_for_author(
        self, candidate_key: str, author_key: str, desc: dict, cutoff: int
    ) -> list[CoiReason]:
        """Every COI reason between one resolved author and the candidate."""
        author_node = self._nodes.get(author_key)
        who = {
            "author_key": author_key,
            "author_name": desc.get("name") or (author_node.name if author_node else None),
            "author_rid": author_node.rid if author_node else desc.get("rid") or desc.get("orcid"),
        }

        reasons: list[CoiReason] = []
        for e in self._pair_index.get(self._pk(candidate_key, author_key), []):
            if e.type == EdgeType.coauthor:
                reasons.append(
                    CoiReason(
                        **who,
                        type="coauthor",
                        last_year=e.last_year,
                        in_window=e.last_year is None or e.last_year >= cutoff,
                        paper_count=e.paper_count,
                        confidence=e.confidence,
                    )
                )
            elif e.type == EdgeType.shared_institution:
                reasons.append(
                    CoiReason(
                        **who,
                        type="shared_institution",
                        institution=e.institution,
                        overlap_years=e.overlap_years,
                        confidence=e.confidence,
                    )
                )
            elif e.type == EdgeType.advised:
                # Directed advisee -> advisor. src is the advisee.
                direction = (
                    "candidate_advised_by_author"
                    if e.src == candidate_key
                    else "author_advised_by_candidate"
                )
                reasons.append(
                    CoiReason(**who, type="advised", direction=direction, confidence=e.confidence)
                )

        # An author with no shared-institution edge (typically one with no
        # profile) still trips a shared-institution COI if the affiliation
        # passed inline is one the candidate holds.
        if not any(r.type == "shared_institution" for r in reasons):
            inst = normalize_institution(
                affiliation_id=desc.get("affiliation_id"), name=desc.get("affiliation")
            )
            cand_node = self._nodes.get(candidate_key)
            cand_insts = (
                {ref.key: (ref.name or ref.key) for ref in cand_node.institutions}
                if cand_node
                else {}
            )
            if inst is not None and inst[0] in cand_insts:
                reasons.append(
                    CoiReason(
                        **who,
                        type="shared_institution",
                        institution=cand_insts[inst[0]],
                        overlap_years=None,
                        confidence="low",
                    )
                )
        return reasons


__all__ = ["ProfileGraph", "CoiReason", "CoiVerdict", "Neighbor"]
