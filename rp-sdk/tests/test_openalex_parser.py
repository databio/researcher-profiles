"""Golden-record tests for the pure OpenAlex parser.

No network, no real data. Small inline fixture dicts plus committed golden
snapshots under ``tests/fixtures/openalex/``.
"""

import json
import logging
from pathlib import Path

import pytest

from researcher_profiles.openalex import (
    authorship_evidence,
    decode_abstract_inverted_index,
    parse_work,
    to_normalized_dict,
    to_work_dict,
)

_OPENALEX_FIXTURES = Path(__file__).parent / "fixtures" / "openalex"


# ---------------------------------------------------------------------------
# decode_abstract_inverted_index
# ---------------------------------------------------------------------------


def test_deinvert_empty_and_none():
    assert decode_abstract_inverted_index(None) == ""
    assert decode_abstract_inverted_index({}) == ""


def test_deinvert_repeated_word_multiple_positions():
    idx = {"of": [1, 3], "bread": [0], "kinds": [2], "wheat": [4]}
    # positions: 0 bread, 1 of, 2 kinds, 3 of, 4 wheat
    assert decode_abstract_inverted_index(idx) == "bread of kinds of wheat"


# ---------------------------------------------------------------------------
# parse_work
# ---------------------------------------------------------------------------


def _raw_work(**overrides) -> dict:
    base = {
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1/ABC",
        "title": "  A Study of Things  ",
        "publication_year": 2021,
        "authorships": [
            {"author": {"display_name": "Jane Q Doe"}},
            {"author": {"display_name": "John Smith"}},
        ],
        "primary_location": {
            "source": {"display_name": "Journal of Things"},
            "pdf_url": "https://example.org/paper.pdf",
        },
        "type": "Article",
        "cited_by_count": 42,
        "abstract_inverted_index": {"We": [0], "did": [1], "it": [2]},
        "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/12345678"},
        "locations": [
            {"id": "pmh:oai:pubmedcentral.nih.gov:6772529"},
        ],
    }
    base.update(overrides)
    return base


def test_parse_work_field_mapping():
    rec = parse_work(_raw_work())
    assert rec is not None
    assert rec.title == "A Study of Things"
    assert rec.year == 2021
    assert rec.first_author == "Doe"  # last name only
    assert rec.journal == "Journal of Things"
    assert rec.pdf_url == "https://example.org/paper.pdf"
    assert rec.openalex_id == "W123"
    assert rec.doi == "10.1/abc"  # bare, lowercased
    assert rec.type == "article"
    assert rec.abstract == "We did it"
    assert rec.status == "pending"
    assert rec.paper_id is None  # parser does not compute ID policy
    # Extra fields carried through PaperRecord's extra="allow".
    assert rec.pmid == "12345678"
    assert rec.pmcid == "PMC6772529"
    assert rec.cited_by_count == 42


@pytest.mark.parametrize(
    "overrides",
    [{"title": ""}, {"title": "   "}, {"publication_year": None}],
    ids=["missing_title", "whitespace_title", "missing_year"],
)
def test_parse_work_returns_none_on_a_missing_required_field(overrides):
    assert parse_work(_raw_work(**overrides)) is None


def test_parse_work_host_venue_fallback_and_no_ids():
    raw = {
        "id": "https://openalex.org/W999",
        "title": "Fallback Journal Paper",
        "publication_year": 2019,
        "authorships": [],
        "host_venue": {"display_name": "Legacy Venue"},
        "type": "book-chapter",
    }
    rec = parse_work(raw)
    assert rec is not None
    assert rec.journal == "Legacy Venue"
    assert rec.first_author == ""
    assert rec.doi is None
    assert rec.pmid is None
    assert rec.pmcid is None
    assert rec.abstract == ""
    assert rec.cited_by_count == 0
    assert rec.type == "book-chapter"


def test_parse_work_pmcid_from_landing_page():
    raw = _raw_work(
        locations=[
            {
                "source": {"display_name": "PubMed Central"},
                "landing_page_url": "https://www.ncbi.nlm.nih.gov/pmc/articles/7000000",
            }
        ]
    )
    rec = parse_work(raw)
    assert rec is not None
    assert rec.pmcid == "PMC7000000"


# ---------------------------------------------------------------------------
# to_work_dict / to_normalized_dict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, expected_abstract",
    [({}, "We did it"), ({"abstract_inverted_index": None}, None)],
    ids=["shape", "empty_abstract_is_none"],
)
def test_to_work_dict_shape(overrides, expected_abstract):
    d = to_work_dict(_raw_work(**overrides))
    assert d == {
        "title": "  A Study of Things  ",
        "abstract": expected_abstract,
        "year": 2021,
        "doi": "10.1/ABC",  # bare (case preserved by this adapter), no lowercasing
        "openalex_id": "https://openalex.org/W123",
        "coauthors": ["Jane Q Doe", "John Smith"],
    }


def test_to_normalized_dict_uses_authors_key():
    d = to_normalized_dict(_raw_work())
    assert "authors" in d
    assert "coauthors" not in d
    assert d["authors"] == ["Jane Q Doe", "John Smith"]
    assert d["openalex_id"] == "https://openalex.org/W123"


# ---------------------------------------------------------------------------
# Full output-contract golden snapshots
#
# External consumers bind to the emitted shape, so these snapshots pin
# the FULL emitted dict for parse_work / to_work_dict / to_normalized_dict so a
# silent field rename, type change, or key appearing/disappearing fails loudly.
# The committed golden files make the diff on any parser change reviewable.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["article", "book_chapter"])
def test_openalex_output_contract_golden(fixture):
    raw = json.loads((_OPENALEX_FIXTURES / f"{fixture}.work.json").read_text())
    golden = json.loads((_OPENALEX_FIXTURES / f"{fixture}.golden.json").read_text())

    rec = parse_work(raw)
    emitted_parse = rec.model_dump(mode="json") if rec is not None else None

    assert emitted_parse == golden["parse_work"]
    assert to_work_dict(raw) == golden["to_work_dict"]
    assert to_normalized_dict(raw) == golden["to_normalized_dict"]


def _article_raw() -> dict:
    return json.loads((_OPENALEX_FIXTURES / "article.work.json").read_text())


def test_authorship_evidence_matches_on_bare_orcid():
    ev = authorship_evidence(_article_raw(), "0000-0002-0000-0001")
    assert ev == {
        "orcid_present": True,
        "display_name": "Jane Q Redacted",
        "raw_affiliations": ["Department of Genomics, Redacted University"],
        "institution_ids": ["https://openalex.org/I0000001"],
        "institution_names": ["Redacted University"],
        # The other author's entity IRI, not the matched author's own.
        "coauthor_ids": ["https://openalex.org/A0000002"],
    }


def test_authorship_evidence_matches_on_url_form_orcid():
    ev = authorship_evidence(_article_raw(), "https://orcid.org/0000-0002-0000-0001")
    assert ev["orcid_present"] is True
    assert ev["display_name"] == "Jane Q Redacted"


def test_authorship_evidence_no_match_lists_all_coauthors():
    ev = authorship_evidence(_article_raw(), "0000-0000-0000-9999")
    assert ev == {
        "orcid_present": False,
        "display_name": "",
        "raw_affiliations": [],
        "institution_ids": [],
        "institution_names": [],
        "coauthor_ids": [
            "https://openalex.org/A0000001",
            "https://openalex.org/A0000002",
        ],
    }


def test_authorship_evidence_empty_orcid_never_matches():
    # A blank orcid must not spuriously match an author whose orcid is "".
    ev = authorship_evidence({"authorships": [{"author": {"id": "A1"}}]}, "")
    assert ev["orcid_present"] is False
    assert ev["coauthor_ids"] == ["A1"]


# ---------------------------------------------------------------------------
# de-inversion edge cases (dup-same-position warn path; gap-fill + collapse)
# ---------------------------------------------------------------------------


def test_deinvert_duplicate_same_position_keeps_first_and_warns(caplog):
    # Two words both claim position 0: keep-first (stable by word tie-break) and
    # log a warning; the loser is dropped.
    idx = {"foo": [0], "bar": [0], "baz": [1]}
    with caplog.at_level(logging.WARNING, logger="researcher_profiles.openalex.abstract_decode"):
        out = decode_abstract_inverted_index(idx)
    assert out == "bar baz"  # "bar" wins position 0 by the (pos, word) sort
    assert any("Duplicate position" in r.message for r in caplog.records)


def test_deinvert_fills_gaps_and_collapses_whitespace():
    # Position 1 is absent -> filled with a space -> collapsed on join/strip.
    idx = {"a": [0], "c": [2]}
    assert decode_abstract_inverted_index(idx) == "a c"


# ---------------------------------------------------------------------------
# fetch_new_works: the candidate query client behind work ranking (stubbed HTTP)
# ---------------------------------------------------------------------------


def _fetch_work(oid="W1", title="A Candidate Work", year=2026, wtype="article"):
    return {
        "id": f"https://openalex.org/{oid}",
        "title": title,
        "publication_year": year,
        "type": wtype,
    }


class TestFetchNewWorks:
    """Filter construction, pagination, and dedupe over a stubbed HTTP client."""

    def _stub(self, monkeypatch, respond):
        """Install ``respond(params) -> page`` on the single call site; record calls."""
        import researcher_profiles.openalex as oa

        calls: list[dict] = []

        def fake(url, params):
            calls.append(dict(params))
            return respond(params)

        monkeypatch.setattr(oa, "_http_get_json", fake)
        return calls

    def test_builds_one_query_per_mode_and_dedupes(self, monkeypatch):
        from datetime import date

        from researcher_profiles.openalex import fetch_new_works

        # Every query returns the SAME work: the result must carry it once.
        calls = self._stub(
            monkeypatch, lambda p: {"results": [_fetch_work()], "meta": {"next_cursor": None}}
        )
        out = fetch_new_works(
            since=date(2026, 1, 1),
            topics=["chromatin biology", "T10002"],
            seed_work_ids=["https://openalex.org/W9", "W8"],
            mailto="who@example.org",
        )
        filters = [c["filter"] for c in calls]
        assert all(f.startswith("from_publication_date:2026-01-01,") for f in filters)
        assert any("topics.id:T10002" in f for f in filters)
        assert any("title_and_abstract.search:chromatin biology" in f for f in filters)
        assert any("cites:W9|W8" in f for f in filters)
        assert all(c["mailto"] == "who@example.org" for c in calls)
        # Three query modes ran; the shared work deduped to one PaperRecord.
        assert len(calls) == 3
        assert len(out) == 1
        assert out[0].openalex_id == "W1"
        assert out[0].title == "A Candidate Work"

    def test_cursor_pagination_follows_next_cursor(self, monkeypatch):
        from researcher_profiles.openalex import fetch_new_works

        pages = {
            "*": {"results": [_fetch_work("W1")], "meta": {"next_cursor": "abc"}},
            "abc": {"results": [_fetch_work("W2")], "meta": {"next_cursor": None}},
        }
        calls = self._stub(monkeypatch, lambda p: pages[p["cursor"]])
        out = fetch_new_works(since="2026-01-01", topics=["chromatin"])
        assert [c["cursor"] for c in calls] == ["*", "abc"]
        assert [w.openalex_id for w in out] == ["W1", "W2"]

    def test_max_pages_caps_a_runaway_cursor(self, monkeypatch):
        from researcher_profiles.openalex import fetch_new_works

        calls = self._stub(
            monkeypatch,
            lambda p: {"results": [_fetch_work(f"W{len(calls)}")], "meta": {"next_cursor": "more"}},
        )
        out = fetch_new_works(since="2026-01-01", topics=["chromatin"], max_pages=3)
        assert len(calls) == 3
        assert len(out) == 3

    def test_unusable_works_are_dropped(self, monkeypatch):
        from researcher_profiles.openalex import fetch_new_works

        page = {
            "results": [_fetch_work("W1"), {"id": "https://openalex.org/W2", "title": ""}],
            "meta": {"next_cursor": None},
        }
        self._stub(monkeypatch, lambda p: page)
        out = fetch_new_works(since="2026-01-01", topics=["chromatin"])
        assert [w.openalex_id for w in out] == ["W1"]

    def test_no_topics_and_no_seeds_is_an_error(self):
        from researcher_profiles.openalex import fetch_new_works

        with pytest.raises(ValueError, match="topics and/or seed_work_ids"):
            fetch_new_works(since="2026-01-01")

    def test_default_excludes_deposit_types_but_keeps_papers(self, monkeypatch):
        from researcher_profiles.openalex import fetch_new_works

        # A mixed page: two real papers plus bare software/book deposits. Even
        # if OpenAlex's query-level negation leaks a deposit into the results,
        # the post-parse type gate must still drop it.
        page = {
            "results": [
                _fetch_work("W1", "A Real Article", wtype="article"),
                _fetch_work("W2", "A Preprint", wtype="preprint"),
                _fetch_work("W3", "org/repo: v1.0.0 code dump", wtype="software"),
                _fetch_work("W4", "Python for Bioinformatics", wtype="book"),
                _fetch_work("W5", "Reviewer #2 (Public review)", wtype="peer-review"),
                _fetch_work("W6", "A region-centric R framework", wtype="dataset"),
            ],
            "meta": {"next_cursor": None},
        }
        calls = self._stub(monkeypatch, lambda p: page)
        out = fetch_new_works(since="2026-01-01", topics=["chromatin"])
        # deposits dropped; article, preprint, and (kept) dataset survive.
        assert [w.openalex_id for w in out] == ["W1", "W2", "W6"]
        # The default set is negated at the query level too.
        filt = calls[0]["filter"]
        assert "type:!software" in filt
        assert "type:!book" in filt
        assert "type:!dataset" not in filt  # datasets are kept

    def test_all_types_disables_the_filter(self, monkeypatch):
        from researcher_profiles.openalex import fetch_new_works

        page = {
            "results": [
                _fetch_work("W1", "A Real Article", wtype="article"),
                _fetch_work("W3", "code dump", wtype="software"),
            ],
            "meta": {"next_cursor": None},
        }
        calls = self._stub(monkeypatch, lambda p: page)
        out = fetch_new_works(since="2026-01-01", topics=["chromatin"], exclude_types=set())
        assert [w.openalex_id for w in out] == ["W1", "W3"]
        assert "type:!" not in calls[0]["filter"]

    def test_custom_exclude_types_override(self, monkeypatch):
        from researcher_profiles.openalex import fetch_new_works

        page = {
            "results": [
                _fetch_work("W1", "A Real Article", wtype="article"),
                _fetch_work("W2", "A Preprint", wtype="preprint"),
            ],
            "meta": {"next_cursor": None},
        }
        calls = self._stub(monkeypatch, lambda p: page)
        out = fetch_new_works(since="2026-01-01", topics=["chromatin"], exclude_types={"preprint"})
        assert [w.openalex_id for w in out] == ["W1"]
        assert "type:!preprint" in calls[0]["filter"]
        assert "type:!software" not in calls[0]["filter"]


class TestProfileQueryTerms:
    def test_terms_from_fixture_profile(self, jane_doe_readonly):
        from researcher_profiles.openalex import profile_query_terms

        terms = profile_query_terms(jane_doe_readonly)
        # subfields + interests, order-preserving; the fixture papers carry no
        # OpenAlex ids, so the citation seed list is empty.
        assert "synthetic data science" in terms["topics"]
        assert "vector-representations-of-example-regions" in terms["topics"]
        assert terms["seed_work_ids"] == []

    def test_seed_ids_are_bare(self):
        from types import SimpleNamespace

        from researcher_profiles.openalex import profile_query_terms

        prof = SimpleNamespace(
            metadata=SimpleNamespace(subfields=["x", "x", ""], interests=["y"]),
            papers=[
                SimpleNamespace(openalex_id="https://openalex.org/W123"),
                SimpleNamespace(openalex_id="W456"),
                SimpleNamespace(openalex_id=None),
            ],
        )
        terms = profile_query_terms(prof)
        assert terms["topics"] == ["x", "y"]
        assert terms["seed_work_ids"] == ["W123", "W456"]


# ---------------------------------------------------------------------------
# fetch_work: single-work point lookup (HTTP layer stubbed)
# ---------------------------------------------------------------------------


def _raises_http_error(url, params):
    """A ``_http_get_json`` stub that fails the way a real 404 does."""
    import httpx

    raise httpx.HTTPError("404 not found")


class TestFetchWork:
    def _raw(self):
        return {
            "id": "https://openalex.org/W2741809807",
            "title": "A point-lookup paper",
            "publication_year": 2024,
            "abstract_inverted_index": {"Novel": [0], "method": [1]},
            "type": "article",
        }

    @pytest.mark.parametrize(
        "ref",
        ["W2741809807", "https://openalex.org/W2741809807"],
        ids=["bare-id", "full-url"],
    )
    def test_normalizes_the_ref_and_parses(self, ref, monkeypatch):
        import researcher_profiles.openalex as oa

        calls = {}

        def fake_get(url, params):
            calls["url"] = url
            calls["params"] = params
            return self._raw()

        monkeypatch.setattr(oa, "_http_get_json", fake_get)
        rec = oa.fetch_work(ref, mailto="me@example.org")
        assert rec is not None
        assert rec.title == "A point-lookup paper"
        assert rec.openalex_id == "W2741809807"
        assert rec.abstract == "Novel method"
        assert calls["url"].endswith("/works/W2741809807")
        assert calls["params"]["mailto"] == "me@example.org"

    @pytest.mark.parametrize(
        "stub,ref",
        [
            (_raises_http_error, "W_missing"),
            # No title/year -> parse_work returns None -> fetch_work returns None.
            (lambda url, params: {"id": "x"}, "W_bad"),
            # An empty ref never reaches _http_get_json, so it needs no stub.
            (None, ""),
        ],
        ids=["http-error", "unparseable-work", "empty-id"],
    )
    def test_returns_none(self, stub, ref, monkeypatch):
        import researcher_profiles.openalex as oa

        if stub is not None:
            monkeypatch.setattr(oa, "_http_get_json", stub)
        assert oa.fetch_work(ref) is None
