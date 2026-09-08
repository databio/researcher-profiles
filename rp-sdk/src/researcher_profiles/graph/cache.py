"""SQLite persistence for the derived profile graph.

The graph is a **cache**, not a database of record: it is rebuilt from profiles,
never hand-edited, and lives under the already-disposable ``<root>/.cache/``
tree at ``graph.sqlite``, the same place the centroid matrix caches. Deleting
it is free; the next query rebuilds it.

Schema:

* ``nodes(person_key, rid, slug, name, kind, institutions_json)``
* ``edges(src_key, dst_key, type, directed, confidence, paper_count,
  first_year, last_year, institution, overlap_years, training_kind,
  evidence_json)``, indexed on ``(src_key, type)`` and ``(dst_key, type)`` for
  neighbor lookups from either end of an undirected edge.
* ``graph_meta(key, value)``: build timestamp, counts, source hash.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from ..utils.paths import STORE_CACHE_DIRNAME, store_cache_dir
from .model import EdgeType, GraphEdge, InstitutionRef, PersonNode

GRAPH_DB_NAME = "graph.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    person_key        TEXT PRIMARY KEY,
    rid               TEXT,
    slug              TEXT,
    name              TEXT,
    kind              TEXT NOT NULL,
    institutions_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS edges (
    src_key       TEXT NOT NULL,
    dst_key       TEXT NOT NULL,
    type          TEXT NOT NULL,
    directed      INTEGER NOT NULL DEFAULT 0,
    confidence    TEXT NOT NULL DEFAULT 'medium',
    paper_count   INTEGER NOT NULL DEFAULT 0,
    first_year    INTEGER,
    last_year     INTEGER,
    institution   TEXT,
    overlap_years INTEGER,
    training_kind TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (src_key, dst_key, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_key, type);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_key, type);
CREATE TABLE IF NOT EXISTS graph_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def graph_db_path(root: str | Path) -> Path:
    return Path(root).expanduser().resolve() / STORE_CACHE_DIRNAME / GRAPH_DB_NAME


def _bool_to_int(v: Optional[bool]) -> Optional[int]:
    return None if v is None else (1 if v else 0)


def _int_to_bool(v: Optional[int]) -> Optional[bool]:
    return None if v is None else bool(v)


def save_graph(
    root: str | Path, nodes: list[PersonNode], edges: list[GraphEdge], meta: dict
) -> Path:
    """Write the graph to ``<root>/.cache/graph.sqlite`` atomically-ish.

    The whole DB is rewritten from scratch (drop + recreate), because the graph
    is derived in full. There is no partial-write correctness to preserve.
    """
    store_cache_dir(Path(root).expanduser().resolve())
    path = graph_db_path(root)
    tmp = path.with_suffix(".sqlite.tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(str(tmp))
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT OR REPLACE INTO nodes"
            "(person_key, rid, slug, name, kind, institutions_json)"
            " VALUES (?,?,?,?,?,?)",
            [
                (
                    n.key,
                    n.rid,
                    n.slug,
                    n.name,
                    n.kind,
                    json.dumps([i.model_dump() for i in n.institutions]),
                )
                for n in nodes
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO edges"
            "(src_key, dst_key, type, directed, confidence, paper_count, first_year,"
            " last_year, institution, overlap_years, training_kind, evidence_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    e.src,
                    e.dst,
                    e.type.value,
                    1 if e.directed else 0,
                    e.confidence,
                    e.paper_count,
                    e.first_year,
                    e.last_year,
                    e.institution,
                    _bool_to_int(e.overlap_years),
                    e.training_kind,
                    json.dumps(e.evidence),
                )
                for e in edges
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO graph_meta(key, value) VALUES (?,?)",
            [(k, str(v)) for k, v in meta.items()],
        )
        conn.commit()
    finally:
        conn.close()
    tmp.replace(path)
    return path


def _row_to_node(row: sqlite3.Row) -> PersonNode:
    insts = [InstitutionRef(**i) for i in json.loads(row["institutions_json"] or "[]")]
    return PersonNode(
        key=row["person_key"],
        kind=row["kind"],
        rid=row["rid"],
        slug=row["slug"],
        name=row["name"],
        institutions=insts,
    )


def _row_to_edge(row: sqlite3.Row) -> GraphEdge:
    return GraphEdge(
        src=row["src_key"],
        dst=row["dst_key"],
        type=EdgeType(row["type"]),
        directed=bool(row["directed"]),
        confidence=row["confidence"],
        paper_count=row["paper_count"],
        first_year=row["first_year"],
        last_year=row["last_year"],
        institution=row["institution"],
        overlap_years=_int_to_bool(row["overlap_years"]),
        training_kind=row["training_kind"],
        evidence=json.loads(row["evidence_json"] or "[]"),
    )


def load_graph(root: str | Path) -> tuple[list[PersonNode], list[GraphEdge], dict]:
    """Load nodes, edges, and meta from ``graph.sqlite``.

    Raises :class:`FileNotFoundError` when the DB is absent, so the caller can
    fall back to a rebuild.
    """
    path = graph_db_path(root)
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        nodes = [_row_to_node(r) for r in conn.execute("SELECT * FROM nodes")]
        edges = [_row_to_edge(r) for r in conn.execute("SELECT * FROM edges")]
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM graph_meta")}
    finally:
        conn.close()
    return nodes, edges, meta


def delete_profile(root: str | Path, rid: str) -> None:
    """Remove one profile's node and its incident edges from the store.

    The incremental primitive: on a single-profile change a caller may drop that
    profile's rows and re-derive, rather than rebuilding the whole graph. It is
    the full rebuild, not this, that remains the source of truth (the write
    invalidation path unlinks the DB and rebuilds), so this can never silently
    diverge from a clean build.
    """
    path = graph_db_path(root)
    if not path.is_file():
        return
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("DELETE FROM nodes WHERE person_key = ?", (rid,))
        conn.execute("DELETE FROM edges WHERE src_key = ? OR dst_key = ?", (rid, rid))
        conn.commit()
    finally:
        conn.close()


def unlink_graph(root: str | Path) -> None:
    """Delete the on-disk graph cache (called by the post-write invalidation hook)."""
    path = graph_db_path(root)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


__all__ = [
    "GRAPH_DB_NAME",
    "graph_db_path",
    "save_graph",
    "load_graph",
    "delete_profile",
    "unlink_graph",
]
