"""The derived profile graph: edge derivation, COI, and the query endpoints.

The suite builds a small fixture store with a KNOWN structure and asserts the
graph derives the expected edges, that the COI endpoints read them correctly,
and that the store round-trips (rebuild is idempotent; a push invalidates it).
No embedding index is needed, since the graph is pure bibliometric transformation.
These tests stub the registry where ranking would otherwise require real vectors.
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
def store(tmp_path: Path) -> Path:
    return _make_store(tmp_path / "profiles")


@pytest.fixture
def graph(store: Path) -> ProfileGraph:
    profs = [ResearcherProfile.from_files(store / s) for s in ("alice", "bob", "carol")]
    return build_graph(profs)


# ---------------------------------------------------------------------------
# Name folding
# ---------------------------------------------------------------------------


def test_normalize_name_folds_initials_and_order():
    assert normalize_name("Jane A. Doe") == "doe j"
    assert normalize_name("J. Doe") == "doe j"
    assert normalize_name("Doe, Jane") == "doe j"
    assert normalize_name("  ") is None


# ---------------------------------------------------------------------------
# 1. Edge derivation
# ---------------------------------------------------------------------------


def _edge(graph: ProfileGraph, a: str, b: str, type_: EdgeType):
    for e in graph.edges_between([a], b):
        if e.type == type_:
            return e
    return None


def test_coauthor_edge_from_shared_doi(graph: ProfileGraph):
    e = _edge(graph, ALICE, BOB, EdgeType.coauthor)
    assert e is not None
    assert e.paper_count == 1  # one shared work, not double-counted per corpus
    assert e.first_year == 2022
    assert e.last_year == 2022
    assert e.confidence == "high"  # cross-corpus join, no name matching


def test_shared_institution_edge_from_affiliation_id(graph: ProfileGraph):
    e = _edge(graph, ALICE, BOB, EdgeType.shared_institution)
    assert e is not None
    assert e.confidence == "high"  # ROR-keyed
    assert e.institution == "Shared University"


def test_advised_edge_from_training_advisor(graph: ProfileGraph):
    e = _edge(graph, ALICE, BOB, EdgeType.advised)
    assert e is not None
    assert e.directed is True
    assert e.src == ALICE  # advisee -> advisor
    assert e.dst == BOB
    assert e.training_kind == "degree"


def test_unrelated_profile_has_no_edges(graph: ProfileGraph):
    assert graph.edges_between([ALICE], CAROL) == []
    assert graph.edges_between([BOB], CAROL) == []


# ---------------------------------------------------------------------------
# 5. Rebuild idempotence + store round-trip
# ---------------------------------------------------------------------------


def test_graph_rebuilds_identically_from_store(store: Path):
    profs = [ResearcherProfile.from_files(store / s) for s in ("alice", "bob", "carol")]
    g = build_graph(profs)
    g.save(store)
    assert graph_db_path(store).is_file()

    nodes, edges, meta = load_graph(store)
    reloaded = ProfileGraph(nodes=nodes, edges=edges, meta=meta, root=store)
    assert {e.key() for e in g.edges} == {e.key() for e in reloaded.edges}
    assert {n.key for n in g.nodes} == {n.key for n in reloaded.nodes}

    # Delete + rebuild yields the same edge set (no dependence on prior state).
    graph_db_path(store).unlink()
    rebuilt = ProfileGraph.from_store(store)
    assert {e.key() for e in g.edges} == {e.key() for e in rebuilt.edges}


# ---------------------------------------------------------------------------
# 2 + 4. COI check endpoint
# ---------------------------------------------------------------------------


def test_coi_check_returns_known_coauthor(make_api_client, store: Path):
    client = make_api_client(store)
    r = client.post(
        "/api/v1/coi/check",
        json={"author_set": [{"rid": ALICE, "name": "Alice Alpha"}], "candidate": "bob"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_coi"] is True
    kinds = {reason["type"] for reason in body["reasons"]}
    assert "coauthor" in kinds
    coauthor = next(x for x in body["reasons"] if x["type"] == "coauthor")
    assert coauthor["last_year"] == 2022
    assert coauthor["in_window"] is True


def test_coi_check_no_relationship_is_clear(make_api_client, store: Path):
    client = make_api_client(store)
    r = client.post(
        "/api/v1/coi/check",
        json={"author_set": [{"rid": CAROL, "name": "Carol Gamma"}], "candidate": "bob"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_coi"] is False


def test_coi_check_external_author_by_affiliation(make_api_client, store: Path):
    """A manuscript author with no profile still trips a same-institution COI."""
    client = make_api_client(store)
    r = client.post(
        "/api/v1/coi/check",
        json={
            "author_set": [{"name": "Nobody Unknown", "affiliation_id": ROR}],
            "candidate": "alice",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_coi"] is True
    assert any(x["type"] == "shared_institution" for x in body["reasons"])


def test_coi_check_unknown_candidate_404(make_api_client, store: Path):
    client = make_api_client(store)
    r = client.post(
        "/api/v1/coi/check",
        json={"author_set": [{"rid": ALICE}], "candidate": "no-such-slug"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3. COI-filtered reviewer match (registry stubbed, no vectors needed)
# ---------------------------------------------------------------------------


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
def reviewer_client(make_api_client, store: Path):
    """An API client whose ranking returns [bob, carol]. Bob is Alice's coauthor."""
    from researcher_profiles.api.deps import get_match_store

    client = make_api_client(store)
    bob = ResearcherProfile.from_files(store / "bob")
    carol = ResearcherProfile.from_files(store / "carol")
    fake = _FakeVectorStore([bob, carol])
    client.app.dependency_overrides[get_match_store] = lambda: fake
    return client


def test_reviewer_match_drops_conflicted(reviewer_client):
    r = reviewer_client.post(
        "/api/v1/match/reviewers",
        json={"query": "genomics", "author_set": [{"rid": ALICE}], "mode": "drop"},
    )
    assert r.status_code == 200, r.text
    slugs = [m["slug"] for m in r.json()["matches"]]
    assert "bob" not in slugs  # coauthor of the manuscript author, dropped
    assert "carol" in slugs  # unconflicted, retained


def test_reviewer_match_annotate_flags_conflicted(reviewer_client):
    r = reviewer_client.post(
        "/api/v1/match/reviewers",
        json={"query": "genomics", "author_set": [{"rid": ALICE}], "mode": "annotate"},
    )
    assert r.status_code == 200, r.text
    by_slug = {m["slug"]: m for m in r.json()["matches"]}
    assert by_slug["bob"]["coi"]["has_coi"] is True
    assert by_slug["carol"].get("coi") is None


# ---------------------------------------------------------------------------
# Neighborhood endpoint
# ---------------------------------------------------------------------------


def test_neighbors_returns_direct_coauthor(make_api_client, store: Path):
    client = make_api_client(store)
    r = client.get("/api/v1/graph/neighbors/alice", params={"types": "coauthor"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["center"]["rid"] == ALICE
    nbr_rids = {n["node"]["rid"] for n in body["neighbors"]}
    assert BOB in nbr_rids
    assert CAROL not in nbr_rids


# ---------------------------------------------------------------------------
# Invalidation: a push drops the graph snapshot and on-disk cache
# ---------------------------------------------------------------------------


def test_push_invalidates_graph(make_api_client, store: Path):
    client = make_api_client(store)
    # Warm the graph and its on-disk cache.
    r = client.get("/api/v1/graph/neighbors/alice")
    assert r.status_code == 200
    assert client.app.state.graph is not None
    assert graph_db_path(store).is_file()

    # A metadata patch funnels through _invalidate_after_write.
    r = client.patch("/api/v1/profiles/carol/metadata", json={"field": "Genomics"})
    assert r.status_code == 200, r.text
    assert client.app.state.graph is None
    assert not graph_db_path(store).is_file()
