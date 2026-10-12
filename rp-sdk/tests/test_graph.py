"""The derived profile graph: edge derivation, COI, and the query endpoints, on a fixture store with a known shape.
No embedding index is needed; the registry is stubbed where ranking would require vectors.
"""

from pathlib import Path

import pytest

from researcher_profiles.graph import (
    EdgeType,
    ProfileGraph,
    build_graph,
    normalize_name,
)
from researcher_profiles.graph.cache import graph_db_path, load_graph
from researcher_profiles.models.results import Match, MatchEvidence
from researcher_profiles.profile import ResearcherProfile

from .factories import write_papers, write_profile

# Three researchers with this structure:
#   alice & bob   -> coauthored a shared DOI (2022), both at Shared University
#   alice         -> advised by bob (training.advisor names Bob)
#   carol         -> unrelated (own paper, different institution)
ALICE = "0000-0002-1825-0097"
BOB = "0000-0004-4600-113X"
CAROL = "0000-0001-2345-6789"

ROR = "https://ror.org/05xyz1234"


def _make_store(root: Path) -> Path:
    write_profile(
        root / "alice",
        name="Alice Alpha",
        rid=ALICE,
        affiliation="Shared University",
        affiliation_id=ROR,
        training=[
            {
                "kind": "degree",
                "degree": "PhD",
                "institution": "Grad School",
                "year_end": 2011,
                "advisor": "Bob Beta",
            }
        ],
    )
    write_papers(
        root / "alice",
        [
            {
                "paperId": "shared2022",
                "name": "A Shared Paper",
                "datePublished": "2022",
                "doi": "10.1/shared",
                "author": ["Alice Alpha", "Bob Beta"],
            }
        ],
    )
    write_profile(
        root / "bob",
        name="Bob Beta",
        rid=BOB,
        affiliation="Shared University",
        affiliation_id=ROR,
    )
    write_papers(
        root / "bob",
        [
            {
                "paperId": "bshared",
                "name": "A Shared Paper",
                "datePublished": "2022",
                "doi": "10.1/shared",
                "author": ["Alice Alpha", "Bob Beta"],
            }
        ],
    )
    write_profile(
        root / "carol",
        name="Carol Gamma",
        rid=CAROL,
        affiliation="Other Institute",
    )
    write_papers(
        root / "carol",
        [{"paperId": "solo", "name": "Carol Solo Work", "datePublished": "2015"}],
    )
    return root


@pytest.fixture
def graph_root(tmp_path: Path) -> Path:
    return _make_store(tmp_path / "profiles")


@pytest.fixture
def graph(graph_root: Path) -> ProfileGraph:
    profs = [ResearcherProfile.from_files(graph_root / s) for s in ("alice", "bob", "carol")]
    return build_graph(profs)


@pytest.mark.parametrize(
    "raw, folded",
    [("Jane A. Doe", "doe j"), ("J. Doe", "doe j"), ("Doe, Jane", "doe j"), ("  ", None)],
)
def test_normalize_name_folds_initials_and_order(raw, folded):
    assert normalize_name(raw) == folded


def _edge(graph: ProfileGraph, a: str, b: str, type_: EdgeType):
    for e in graph.edges_between([a], b):
        if e.type == type_:
            return e
    return None


@pytest.mark.parametrize(
    "type_, expected",
    [
        (  # one shared work, not double-counted per corpus; cross-corpus join, no name matching
            EdgeType.coauthor,
            {"paper_count": 1, "first_year": 2022, "last_year": 2022, "confidence": "high"},
        ),
        (EdgeType.shared_institution, {"confidence": "high", "institution": "Shared University"}),
        (  # advisee -> advisor
            EdgeType.advised,
            {"directed": True, "src": ALICE, "dst": BOB, "training_kind": "degree"},
        ),
    ],
    ids=["coauthor", "shared-institution", "advised"],
)
def test_alice_bob_edge(graph: ProfileGraph, type_, expected):
    e = _edge(graph, ALICE, BOB, type_)
    assert {k: getattr(e, k) for k in expected} == expected


def test_unrelated_profile_has_no_edges(graph: ProfileGraph):
    assert graph.edges_between([ALICE], CAROL) == []
    assert graph.edges_between([BOB], CAROL) == []


def test_graph_rebuilds_identically_from_store(graph_root: Path):
    profs = [ResearcherProfile.from_files(graph_root / s) for s in ("alice", "bob", "carol")]
    g = build_graph(profs)
    g.save(graph_root)
    assert graph_db_path(graph_root).is_file()

    nodes, edges, meta = load_graph(graph_root)
    reloaded = ProfileGraph(nodes=nodes, edges=edges, meta=meta, root=graph_root)
    assert {e.key() for e in g.edges} == {e.key() for e in reloaded.edges}
    assert {n.key for n in g.nodes} == {n.key for n in reloaded.nodes}

    # Delete + rebuild yields the same edge set (no dependence on prior state).
    graph_db_path(graph_root).unlink()
    rebuilt = ProfileGraph.from_store(graph_root)
    assert {e.key() for e in g.edges} == {e.key() for e in rebuilt.edges}


@pytest.mark.parametrize(
    "author_set, candidate, status, has_coi, reason",
    [
        (
            [{"rid": ALICE, "name": "Alice Alpha"}],
            "bob",
            200,
            True,
            {"type": "coauthor", "last_year": 2022, "in_window": True},
        ),
        ([{"rid": CAROL, "name": "Carol Gamma"}], "bob", 200, False, None),
        (  # a manuscript author with no profile still trips a same-institution COI
            [{"name": "Nobody Unknown", "affiliation_id": ROR}],
            "alice",
            200,
            True,
            {"type": "shared_institution"},
        ),
        ([{"rid": ALICE}], "no-such-slug", 404, None, None),
    ],
    ids=["coauthor", "clear", "external-by-affiliation", "unknown-candidate"],
)
def test_coi_check(
    make_api_client, graph_root: Path, author_set, candidate, status, has_coi, reason
):
    client = make_api_client(graph_root)
    r = client.post("/api/v1/coi/check", json={"author_set": author_set, "candidate": candidate})
    assert r.status_code == status, r.text
    if status != 200:
        return
    body = r.json()
    assert body["has_coi"] is has_coi
    if reason:
        assert any(reason.items() <= x.items() for x in body["reasons"])


class _FakeMatch:
    """Ranks a fixed candidate list, highest first, with empty evidence."""

    def __init__(self, profiles):
        self._profiles = profiles

    def rank(self, text, **kwargs):
        out = []
        for i, prof in enumerate(self._profiles):
            ev = MatchEvidence(
                top_chunks=[], top_papers=[], overlapping_topics=[], centroid_score=1.0
            )
            out.append(Match(profile=prof, score=1.0 - i * 0.1, evidence=ev))
        return out


class _FakeVectorStore:
    """Duck-types a ``VectorStore`` far enough for ``/match/reviewers``."""

    def __init__(self, profiles):
        self.match = _FakeMatch(profiles)
        self._profiles = profiles

    def list_slugs(self):
        return [p.slug for p in self._profiles]


@pytest.fixture
def reviewer_client(make_api_client, graph_root: Path):
    """An API client whose ranking returns [bob, carol]. Bob is Alice's coauthor."""
    from researcher_profiles.api.deps import get_match_store

    client = make_api_client(graph_root)
    bob = ResearcherProfile.from_files(graph_root / "bob")
    carol = ResearcherProfile.from_files(graph_root / "carol")
    fake = _FakeVectorStore([bob, carol])
    client.app.dependency_overrides[get_match_store] = lambda: fake
    return client


def test_reviewer_match_handles_conflicts(reviewer_client):
    def post(mode):
        r = reviewer_client.post(
            "/api/v1/match/reviewers",
            json={"query": "genomics", "author_set": [{"rid": ALICE}], "mode": mode},
        )
        assert r.status_code == 200, r.text
        return {m["slug"]: m for m in r.json()["matches"]}

    dropped = post("drop")
    assert "bob" not in dropped  # coauthor of the manuscript author
    assert "carol" in dropped  # unconflicted, retained
    annotated = post("annotate")
    assert annotated["bob"]["coi"]["has_coi"] is True
    assert annotated["carol"].get("coi") is None


def test_neighbors_returns_direct_coauthor(make_api_client, graph_root: Path):
    client = make_api_client(graph_root)
    r = client.get("/api/v1/graph/neighbors/alice", params={"types": "coauthor"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["center"]["rid"] == ALICE
    nbr_rids = {n["node"]["rid"] for n in body["neighbors"]}
    assert BOB in nbr_rids
    assert CAROL not in nbr_rids


def test_push_invalidates_graph(make_api_client, graph_root: Path):
    client = make_api_client(graph_root)
    # Warm the graph and its on-disk cache.
    r = client.get("/api/v1/graph/neighbors/alice")
    assert r.status_code == 200
    assert client.app.state.service.graph is not None
    assert graph_db_path(graph_root).is_file()

    # A metadata patch funnels through Service.invalidate.
    r = client.patch("/api/v1/profiles/carol/metadata", json={"field": "Genomics"})
    assert r.status_code == 200, r.text
    assert client.app.state.service.graph is None
    assert not graph_db_path(graph_root).is_file()
