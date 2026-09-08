"""Derive a :class:`ProfileGraph` from a set of loaded profiles.

``build_graph`` is a pure function of the works corpora and profile metadata:
it walks every profile's author lists, affiliation/career/training histories,
and emits nodes and edges. No I/O beyond reading the profiles it is handed.

The one subtlety is scope: a coauthor edge is emitted only when at least one
endpoint is a **profiled** person. Two external names that merely co-occur on a
paper produce no edge. Nothing queries an external-to-external relation, and
materializing every such pair would let one large author list dominate the
store. Every edge the graph answers questions about is therefore incident to a
profile.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import Iterable, Mapping, Optional

import pydantic

from ..errors import ProfileError, ProfileLoadError
from ..profile import ResearcherProfile
from ..schema import _slugify
from ..utils.clock import now_iso
from .identity import (
    NameIndex,
    normalize_institution,
    normalize_name,
    paper_identity_key,
)
from .model import (
    DIRECTED_TYPES,  # noqa: F401  (re-exported convenience)
    EdgeType,
    GraphEdge,
    InstitutionRef,
    PersonNode,
    canonical_pair,
)

logger = logging.getLogger(__name__)

_CONF_RANK = {"high": 3, "medium": 2, "low": 1}


def _better_conf(a: str, b: str) -> str:
    return a if _CONF_RANK.get(a, 0) >= _CONF_RANK.get(b, 0) else b


def load_profiles(root: str | os.PathLike) -> list[ResearcherProfile]:
    """Load every profile under ``root``, the same directory walk the registry
    uses, but with no embedding/centroid machinery (the graph needs neither).
    """
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise FileNotFoundError(f"graph root does not exist: {root_path}")
    profiles: list[ResearcherProfile] = []
    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / "profile.jsonld").is_file():
            continue
        try:
            profiles.append(ResearcherProfile.from_files(entry))
        except ProfileLoadError as e:
            logger.warning("graph: skipping malformed profile at %s: %s", entry, e)
    return profiles


def _profile_institutions(prof: ResearcherProfile) -> list[InstitutionRef]:
    """The institution history of a profiled person, from affiliation + career +
    training. Each ref carries a join key (ROR when known, else folded name)."""
    out: list[InstitutionRef] = []
    md = prof.metadata
    aff = normalize_institution(affiliation_id=md.affiliation_id, name=md.affiliation)
    if aff is not None:
        out.append(InstitutionRef(key=aff[0], name=aff[1]))
    for c in md.career:
        inst = normalize_institution(name=c.institution)
        if inst is not None:
            out.append(
                InstitutionRef(
                    key=inst[0], name=inst[1], year_start=c.start_year, year_end=c.end_year
                )
            )
    for t in md.training:
        inst = normalize_institution(name=t.institution)
        if inst is not None:
            out.append(
                InstitutionRef(
                    key=inst[0], name=inst[1], year_start=t.year_start, year_end=t.year_end
                )
            )
    return out


def _spans_overlap(
    spans_a: list[tuple[Optional[int], Optional[int]]],
    spans_b: list[tuple[Optional[int], Optional[int]]],
) -> Optional[bool]:
    """Did two people's tenures at one institution overlap in time?

    ``True``/``False`` when at least one span on each side carries a year;
    ``None`` when neither side has any year information (e.g. two current
    affiliations with no dates), so the caller reports "unknown" rather than
    guessing overlap from thin air.
    """

    def has_year(spans: list[tuple[Optional[int], Optional[int]]]) -> bool:
        return any(s is not None or e is not None for s, e in spans)

    if not has_year(spans_a) or not has_year(spans_b):
        return None
    NEG, POS = -(10**9), 10**9
    for sa, ea in spans_a:
        lo_a = sa if sa is not None else NEG
        hi_a = ea if ea is not None else POS
        for sb, eb in spans_b:
            lo_b = sb if sb is not None else NEG
            hi_b = eb if eb is not None else POS
            if lo_a <= hi_b and lo_b <= hi_a:
                return True
    return False


def _build_name_index(profiles: list[ResearcherProfile]) -> NameIndex:
    """Folded profile name -> rid. The index drops names that are ambiguous."""
    name_index = NameIndex()
    for p in profiles:
        name_index.add(p.name, p.rid)
    return name_index


def _build_person_nodes(profiles: list[ResearcherProfile]) -> dict[str, PersonNode]:
    """One ``profiled`` node per profile, keyed by rid."""
    return {
        p.rid: PersonNode(
            key=p.rid,
            kind="profiled",
            rid=p.rid,
            slug=p.slug,
            name=p.name,
            institutions=_profile_institutions(p),
        )
        for p in profiles
    }


def _ensure_external(nodes: dict[str, PersonNode], key: str, name: Optional[str]) -> None:
    """Materialize an external endpoint as a node, or fill in a missing name."""
    node = nodes.get(key)
    if node is None:
        nodes[key] = PersonNode(key=key, kind="external", name=name)
    elif node.kind == "external" and not node.name and name:
        node.name = name


def _note_external(externals: dict[str, Optional[str]], key: str, name: Optional[str]) -> None:
    """Record an external endpoint in first-seen order, upgrading a blank name.

    The edge builders collect these instead of mutating the node map, which is
    what keeps them pure. ``build_graph`` folds the result into the nodes.
    """
    if key not in externals:
        externals[key] = name
    elif not externals[key] and name:
        externals[key] = name


def _group_papers(profiles: list[ResearcherProfile], name_index: NameIndex) -> dict[str, dict]:
    """Group works by cross-corpus identity.

    Each entry is ``{"owners": set[rid], "participants": {key: kind},
    "years": set, "evidence": set, "names": {key: name}}``.
    """
    paper_map: dict[str, dict] = {}
    for p in profiles:
        owner = p.rid
        try:
            papers = p.papers
        except (OSError, ProfileError, pydantic.ValidationError):
            logger.warning("graph: could not read papers for %s", p.slug, exc_info=True)
            papers = []
        for paper in papers:
            idk = paper_identity_key(
                doi=paper.doi, openalex_id=paper.openalex_id, paper_id=paper.paper_id
            )
            if idk is None:
                idk = f"local:{owner}:{paper.paper_id or _slugify(paper.title)}"
            entry = paper_map.setdefault(
                idk,
                {
                    "owners": set(),
                    "participants": {},
                    "years": set(),
                    "evidence": set(),
                    "names": {},
                },
            )
            entry["owners"].add(owner)
            entry["participants"][owner] = "profiled"
            entry["names"].setdefault(owner, p.name)
            if paper.year:
                entry["years"].add(int(paper.year))
            ev = paper.doi or paper.paper_id or paper.openalex_id
            if ev:
                entry["evidence"].add(str(ev))
            for author_name in paper.authors or []:
                matched = name_index.resolve(author_name)
                if matched:
                    key, kind = matched, "profiled"
                else:
                    folded = normalize_name(author_name)
                    if not folded:
                        continue
                    key, kind = folded, "external"
                if kind == "profiled":
                    entry["participants"][key] = "profiled"
                else:
                    entry["participants"].setdefault(key, "external")
                entry["names"].setdefault(key, author_name)
    return paper_map


def _merge_coauthor_edge(
    edges: dict[tuple[str, str], GraphEdge],
    src: str,
    dst: str,
    *,
    confidence: str,
    first_year: Optional[int],
    last_year: Optional[int],
    evidence: list[str],
) -> None:
    """Add a coauthor edge, or widen the one already recorded for the pair."""
    existing = edges.get((src, dst))
    if existing is None:
        edges[(src, dst)] = GraphEdge(
            src=src,
            dst=dst,
            type=EdgeType.coauthor,
            directed=False,
            confidence=confidence,
            paper_count=1,
            first_year=first_year,
            last_year=last_year,
            evidence=list(evidence),
        )
        return
    existing.paper_count += 1
    existing.confidence = _better_conf(existing.confidence, confidence)
    if first_year is not None:
        existing.first_year = (
            first_year if existing.first_year is None else min(existing.first_year, first_year)
        )
    if last_year is not None:
        existing.last_year = (
            last_year if existing.last_year is None else max(existing.last_year, last_year)
        )
    for e in evidence:
        if e not in existing.evidence:
            existing.evidence.append(e)


def _coauthor_edges(
    paper_map: dict[str, dict],
) -> tuple[dict[tuple[str, str], GraphEdge], dict[str, Optional[str]]]:
    """Coauthor edges, plus the external endpoints they reference.

    Each unordered pair is emitted once, and only for pairs incident to at
    least one profiled node: external<->external relations are never queried
    and would let one large author list dominate the store.
    """
    edges: dict[tuple[str, str], GraphEdge] = {}
    externals: dict[str, Optional[str]] = {}
    for entry in paper_map.values():
        participants = entry["participants"]
        owners = entry["owners"]
        years = entry["years"]
        yr_min = min(years) if years else None
        yr_max = max(years) if years else None
        evidence = sorted(entry["evidence"])
        keys = list(participants)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                if participants[a] != "profiled" and participants[b] != "profiled":
                    continue
                both_owner = a in owners and b in owners
                both_profiled = participants[a] == "profiled" and participants[b] == "profiled"
                conf = "high" if both_owner else ("medium" if both_profiled else "low")
                src, dst = canonical_pair(a, b)
                _merge_coauthor_edge(
                    edges,
                    src,
                    dst,
                    confidence=conf,
                    first_year=yr_min,
                    last_year=yr_max,
                    evidence=evidence,
                )
                for k in (a, b):
                    if participants[k] == "external":
                        _note_external(externals, k, entry["names"].get(k))
    return edges, externals


def _institution_index(
    nodes: Mapping[str, PersonNode],
) -> tuple[
    dict[str, dict[str, list[tuple[Optional[int], Optional[int]]]]],
    dict[str, str],
]:
    """Institution key -> {rid: tenure spans}, plus a display name per key."""
    inst_map: dict[str, dict[str, list[tuple[Optional[int], Optional[int]]]]] = {}
    inst_name: dict[str, str] = {}
    for rid, node in nodes.items():
        if node.kind != "profiled":
            continue
        for ref in node.institutions:
            inst_map.setdefault(ref.key, {}).setdefault(rid, []).append(
                (ref.year_start, ref.year_end)
            )
            if ref.name and ref.key not in inst_name:
                inst_name[ref.key] = ref.name
    return inst_map, inst_name


def _shared_institution_edges(
    inst_map: dict[str, dict[str, list[tuple[Optional[int], Optional[int]]]]],
    inst_name: dict[str, str],
) -> dict[tuple[str, str], GraphEdge]:
    """Shared-institution edges (undirected, profiled<->profiled)."""
    shared: dict[tuple[str, str], GraphEdge] = {}
    for inst_key, per_rid in inst_map.items():
        rids = sorted(per_rid)
        if len(rids) < 2:
            continue
        conf = "high" if inst_key.startswith("ror:") else "medium"
        display = inst_name.get(inst_key, inst_key)
        for i in range(len(rids)):
            for j in range(i + 1, len(rids)):
                a, b = rids[i], rids[j]
                overlap = _spans_overlap(per_rid[a], per_rid[b])
                src, dst = canonical_pair(a, b)
                shared[(src, dst)] = GraphEdge(
                    src=src,
                    dst=dst,
                    type=EdgeType.shared_institution,
                    directed=False,
                    confidence=conf,
                    institution=display,
                    overlap_years=overlap,
                )
    return shared


def _advised_edges(
    profiles: list[ResearcherProfile], name_index: NameIndex
) -> tuple[dict[tuple[str, str], GraphEdge], dict[str, Optional[str]]]:
    """Advised edges (directed advisee -> advisor) from ``training[].advisor``,
    plus the external advisors they reference.
    """
    advised: dict[tuple[str, str], GraphEdge] = {}
    externals: dict[str, Optional[str]] = {}
    for p in profiles:
        advisee = p.rid
        for t in p.metadata.training:
            if not t.advisor:
                continue
            matched = name_index.resolve(t.advisor)
            if matched:
                dst, dst_kind = matched, "profiled"
            else:
                folded = normalize_name(t.advisor)
                if not folded:
                    continue
                dst, dst_kind = folded, "external"
            if dst == advisee:
                continue
            if dst_kind == "external":
                _note_external(externals, dst, t.advisor)
            pk = (advisee, dst)
            existing = advised.get(pk)
            if existing is None:
                advised[pk] = GraphEdge(
                    src=advisee,
                    dst=dst,
                    type=EdgeType.advised,
                    directed=True,
                    confidence="low",
                    training_kind=t.kind,
                    last_year=t.year_end,
                )
            elif existing.last_year is None and t.year_end is not None:
                existing.last_year = t.year_end
    return advised, externals


def build_graph(profiles: Iterable[ResearcherProfile]):
    """Build a :class:`ProfileGraph` from ``profiles``."""
    from .graph import ProfileGraph  # local import avoids a module cycle

    profiles = list(profiles)
    name_index = _build_name_index(profiles)
    nodes = _build_person_nodes(profiles)
    paper_map = _group_papers(profiles, name_index)
    coauthor, coauthor_externals = _coauthor_edges(paper_map)
    advised, advised_externals = _advised_edges(profiles, name_index)
    for key, nm in (*coauthor_externals.items(), *advised_externals.items()):
        _ensure_external(nodes, key, nm)
    inst_map, inst_name = _institution_index(nodes)
    shared = _shared_institution_edges(inst_map, inst_name)

    edges: list[GraphEdge] = [*coauthor.values(), *shared.values(), *advised.values()]
    meta = {
        "built_at": now_iso(),
        "profile_count": str(len(profiles)),
        "node_count": str(len(nodes)),
        "edge_count": str(len(edges)),
        "source_hash": _source_hash(profiles),
    }
    return ProfileGraph(nodes=list(nodes.values()), edges=edges, meta=meta)


def _source_hash(profiles: list[ResearcherProfile]) -> str:
    """A coarse fingerprint of the input corpus, enough to notice a change.

    A failure to read a corpus propagates. This hash is the graph cache's
    freshness key, so swallowing the error would let a stale graph be served
    as fresh.
    """
    h = hashlib.sha256()
    for p in sorted(profiles, key=lambda x: x.rid):
        h.update(p.rid.encode("utf-8"))
        h.update(b"\0")
        h.update(str(len(p.papers)).encode("utf-8"))
        h.update(b"\0")
        h.update((p.metadata.affiliation_id or p.metadata.affiliation or "").encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


__all__ = ["build_graph", "load_profiles"]
