"""The record view's size budget (``api/_records.py``).

``GET /profiles/{slug}`` defaults to the trimmed record, the read an agent
makes before nearly everything else. Its budget is a contract: at most 8 KB
whatever the profile holds, because a record that grows with the corpus
(the file manifest, every interest with its evidence) is what made a
one-field edit cost a 34K-token read.
"""

import json

import pytest

from .factories import build_profile_dir

BUDGET = 8 * 1024


def _interest(i: int) -> dict:
    """An OpenAlex-topic interest with its full evidence, as the build writes it."""
    return {
        "concept": {
            "id": f"https://openalex.org/T{10000 + i}",
            "system": "https://openalex.org/topics",
            "code": f"T{10000 + i}",
            "display": f"Topic number {i} in regulatory genomics and chromatin biology",
            "version": "2026-09",
        },
        "method": "inferred",
        "generator": "openalex-topics@2026-09",
        "assertedAt": "2026-09-27T01:16:55Z",
        "evidence": {"papers": [f"W{4000000000 + j}" for j in range(21)], "share": 0.05},
    }


@pytest.fixture
def big_profile(tmp_path):
    root = tmp_path / "profiles"
    papers = [
        {
            "paper_id": f"p{i:03d}",
            "title": f"A long title about region set enrichment analysis, number {i}",
            "year": 1990 + i % 35,
            "doi": f"10.1/{i}",
        }
        for i in range(300)
    ]
    build_profile_dir(
        root / "big",
        papers=papers,
        summaries={f"p{i:03d}": "A summary. " * 30 for i in range(300)},
        research_interests=[_interest(i) for i in range(19)],
        career=[
            {"role": f"Role {i}", "institution": "University", "start_year": 2000 + i}
            for i in range(12)
        ],
    )
    return root


@pytest.mark.parametrize("where", ["fixture", "big"])
def test_the_record_stays_within_budget(where, make_api_client, fixture_profiles_root, big_profile):
    if where == "fixture":
        root, slug = fixture_profiles_root("jane-doe"), "jane-doe"
    else:
        root, slug = big_profile, "big"
    record = make_api_client(root).get(f"/api/v1/profiles/{slug}").json()
    size = len(json.dumps(record, separators=(",", ":")).encode())
    assert size <= BUDGET, size
    if where == "big":
        assert record["fields"]["research_interests"]["total"] == 19
        assert len(record["fields"]["research_interests"]["top"]) == 8
        assert "evidence" not in json.dumps(record["fields"]["research_interests"])
        assert record["fields"]["career"]["total"] == 12
        assert record["fields"]["career"]["top"][0]["role"] == "Role 11"  # most recent first
        assert record["fields"]["paper_count"] == 300


def test_the_full_view_keeps_what_the_record_trims(big_profile, make_api_client):
    full = make_api_client(big_profile).get("/api/v1/profiles/big", params={"view": "full"}).json()
    assert len(full["fields"]["research_interests"]) == 19
    assert "evidence" in full["fields"]["research_interests"][0]
    assert len(full["fields"]["career"]) == 12
