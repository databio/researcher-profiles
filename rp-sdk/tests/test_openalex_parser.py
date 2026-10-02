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
        "primary_topic": {"id": "https://openalex.org/T10222"},
        "topics": [{"id": "https://openalex.org/T10222"}, {"id": "https://openalex.org/T11289"}],
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
    # Primary topic first, each topic once, bare ids.
    assert rec.topics == ["T10222", "T11289"]


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


class _ParamsView:
    """A live list-like view of the params a :class:`FakeClient` was called with."""

    def __init__(self, client):
        self._client = client

    def _params(self):
        return [p for _, p in self._client.calls]

    def __len__(self):
        return len(self._client.calls)

    def __getitem__(self, i):
        return self._params()[i]

    def __iter__(self):
        return iter(self._params())


class FakeClient:
    """Stands in for ``OpenAlexClient``: ``respond(path, params) -> body``; records calls."""

    def __init__(self, respond):
        self.respond = respond
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return self.respond(path, dict(params or {}))


class TestFetchNewWorks:
    """Filter construction, pagination, and dedupe over a stubbed client."""

    def _stub(self, respond):
        """A fake client answering ``respond(params) -> page``, and its recorded params."""
        client = FakeClient(lambda path, params: respond(params))
        calls = _ParamsView(client)
        return client, calls

    def test_builds_one_query_per_mode_and_dedupes(self, monkeypatch):
        from datetime import date

        from researcher_profiles.openalex import fetch_new_works

        # Every query returns the SAME work: the result must carry it once.
        client, calls = self._stub(
            lambda p: {"results": [_fetch_work()], "meta": {"next_cursor": None}}
        )
        out = fetch_new_works(
            client,
            since=date(2026, 1, 1),
            topics=["chromatin biology", "T10002"],
            seed_work_ids=["https://openalex.org/W9", "W8"],
        )
        filters = [c["filter"] for c in calls]
        assert all(f.startswith("from_publication_date:2026-01-01,") for f in filters)
        assert any("topics.id:T10002" in f for f in filters)
        assert any("title_and_abstract.search:chromatin biology" in f for f in filters)
        assert any("cites:W9|W8" in f for f in filters)
        assert all(path == "/works" for path, _ in client.calls)
        assert all(c["per-page"] == 100 for c in calls)
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
        client, calls = self._stub(lambda p: pages[p["cursor"]])
        out = fetch_new_works(client, since="2026-01-01", topics=["chromatin"])
        assert [c["cursor"] for c in calls] == ["*", "abc"]
        assert [w.openalex_id for w in out] == ["W1", "W2"]

    def test_max_pages_caps_a_runaway_cursor(self, monkeypatch):
        from researcher_profiles.openalex import fetch_new_works

        client, calls = self._stub(
            lambda p: {"results": [_fetch_work(f"W{len(calls)}")], "meta": {"next_cursor": "more"}},
        )
        out = fetch_new_works(client, since="2026-01-01", topics=["chromatin"], max_pages=3)
        assert len(calls) == 3
        assert len(out) == 3

    def test_unusable_works_are_dropped(self, monkeypatch):
        from researcher_profiles.openalex import fetch_new_works

        page = {
            "results": [_fetch_work("W1"), {"id": "https://openalex.org/W2", "title": ""}],
            "meta": {"next_cursor": None},
        }
        client, _ = self._stub(lambda p: page)
        out = fetch_new_works(client, since="2026-01-01", topics=["chromatin"])
        assert [w.openalex_id for w in out] == ["W1"]

    def test_no_topics_and_no_seeds_is_an_error(self):
        from researcher_profiles.openalex import fetch_new_works

        with pytest.raises(ValueError, match="topics and/or seed_work_ids"):
            fetch_new_works(FakeClient(lambda path, params: {}), since="2026-01-01")

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
        client, calls = self._stub(lambda p: page)
        out = fetch_new_works(client, since="2026-01-01", topics=["chromatin"])
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
        client, calls = self._stub(lambda p: page)
        out = fetch_new_works(client, since="2026-01-01", topics=["chromatin"], exclude_types=set())
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
        client, calls = self._stub(lambda p: page)
        out = fetch_new_works(
            client, since="2026-01-01", topics=["chromatin"], exclude_types={"preprint"}
        )
        assert [w.openalex_id for w in out] == ["W1"]
        assert "type:!preprint" in calls[0]["filter"]
        assert "type:!software" not in calls[0]["filter"]

    def test_tags_each_work_with_the_queries_that_found_it(self, monkeypatch):
        from researcher_profiles.openalex import fetch_new_works

        def respond(params):
            f = params["filter"]
            if "topics.id:" in f:
                results = [_fetch_work("W1", "By topic")]
            elif "title_and_abstract.search:" in f:
                results = [_fetch_work("W1", "By topic"), _fetch_work("W2", "By text")]
            else:
                cited = _fetch_work("W3", "Cites you")
                cited["referenced_works"] = ["https://openalex.org/S1", "https://openalex.org/X"]
                results = [cited]
            return {"results": results, "meta": {"next_cursor": None}}

        client, _ = self._stub(respond)
        out = fetch_new_works(
            client, since="2026-01-01", topics=["T10", "chromatin"], seed_work_ids=["S1", "S2"]
        )
        by_id = {w.openalex_id: w for w in out}
        assert [w.openalex_id for w in out] == ["W1", "W2", "W3"]
        assert by_id["W1"].found_by == ["topic", "text"]
        assert by_id["W2"].found_by == ["text"]
        assert by_id["W3"].found_by == ["cites"]
        assert by_id["W3"].cites_works == ["S1"]


class TestProfileQueryTerms:
    def test_terms_from_fixture_profile(self, jane_doe_readonly):
        from researcher_profiles.openalex import profile_query_terms

        terms = profile_query_terms(jane_doe_readonly)
        # subfields + interests, order-preserving; the fixture papers carry no
        # OpenAlex ids, so the citation seed list is empty.
        assert "synthetic data science" in terms["topics"]
        assert "vector-representations-of-example-regions" in terms["topics"]
        assert terms["seed_work_ids"] == []

    def test_typed_interests_query_by_topic_id(self):
        from types import SimpleNamespace

        from researcher_profiles.openalex import profile_query_terms
        from researcher_profiles.schema import ProfileDocument

        def topic(code, weight=None, share=None):
            entry = {
                "concept": {
                    "@id": f"https://openalex.org/{code}",
                    "system": "https://openalex.org/topics",
                    "code": code,
                    "display": code,
                },
                "method": "declared" if weight is not None else "inferred",
                "generator": "user" if weight is not None else "openalex-topics@2026-09",
                "assertedAt": "2026-09-25T12:00:00Z",
            }
            if weight is not None:
                entry["weight"] = weight
            if share is not None:
                entry["evidence"] = {"papers": ["W1"], "share": share}
            return entry

        doc = ProfileDocument.model_validate(
            {
                "name": "T",
                "rid": "local:t-a1b2c3",
                "provenance": "self_published",
                "subfields": ["broad field"],
                "rp:researchInterests": [
                    topic("T1", 0.5),
                    topic("T2", share=0.2),
                    topic("T3", share=0.6),
                    topic("T4", share=0.9),
                    topic("T4", -1),  # declared exclude beats the paper count
                    topic("T5", 0),  # neutral: not queried
                    {
                        "concept": {"label": "graph methods", "unmapped": True},
                        "weight": 0.5,
                        "method": "inferred",
                        "generator": "llm",
                        "assertedAt": "2026-09-25T12:00:00Z",
                    },
                ],
            }
        )
        terms = profile_query_terms(SimpleNamespace(metadata=doc, papers=[]))
        # Declared positive topics, then inferred by share; three topic ids
        # drop the broad subfields; positive free text stays.
        assert terms["topics"] == ["T1", "T3", "T2", "graph methods"]

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
        assert terms["source"] == "subfields"


def _md(**kw):
    from types import SimpleNamespace

    base = {
        "research_interests": [],
        "subfields": [],
        "interests": [],
        "expertise": [],
        "field": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _paper(title, summary=None, openalex_id=None):
    from types import SimpleNamespace

    return SimpleNamespace(title=title, summary=summary, openalex_id=openalex_id)


class TestQueryTermFallback:
    """Each source is used only when every earlier one is empty."""

    PAPERS = [
        _paper(
            "Region set enrichment for chromatin accessibility", "We test region set enrichment."
        ),
        _paper("Fast region set enrichment in Python"),
        _paper("Chromatin accessibility in single cells"),
    ]

    def _terms(self, md, papers=()):
        from types import SimpleNamespace

        from researcher_profiles.openalex import profile_query_terms

        return profile_query_terms(SimpleNamespace(metadata=md, papers=list(papers)))

    def test_expertise_when_no_interests_or_subfields(self):
        t = self._terms(_md(expertise=["Genomic intervals", "ATAC-seq"], field="Genomics"))
        assert t["source"] == "expertise"
        assert t["topics"] == ["Genomic intervals", "ATAC-seq"]

    def test_field_when_no_expertise(self):
        t = self._terms(_md(field="Genomics"), self.PAPERS)
        assert t == {"topics": ["Genomics"], "seed_work_ids": [], "source": "field"}

    def test_paper_text_last(self):
        t = self._terms(_md(field="  "), self.PAPERS)
        assert t["source"] == "paper_text"
        assert t["topics"][:2] == ["region set enrichment", "chromatin accessibility"]

    def test_none_when_nothing(self):
        t = self._terms(_md(), [_paper("One lonely paper")])
        assert t["source"] == "none"
        assert t["topics"] == []

    def test_free_text_capped_ids_not(self):
        from researcher_profiles.openalex import MAX_FREE_TEXT_TERMS

        t = self._terms(_md(subfields=[f"term {i}" for i in range(20)] + ["T123"]))
        text = [x for x in t["topics"] if x != "T123"]
        assert len(text) == MAX_FREE_TEXT_TERMS
        assert "T123" in t["topics"]

    def test_seed_ids_kept_with_any_source(self):
        t = self._terms(_md(field="Genomics"), [_paper("x", openalex_id="W9")])
        assert t["seed_work_ids"] == ["W9"]


class TestPaperKeyPhrases:
    def test_shared_phrases_counted_once_per_paper(self):
        from researcher_profiles.openalex import paper_key_phrases

        papers = [
            _paper("Region set enrichment", "Region set enrichment, again region set enrichment."),
            _paper("Region set enrichment for all"),
            _paper("Unrelated topic entirely"),
        ]
        assert paper_key_phrases(papers) == ["region set enrichment"]

    def test_min_papers_and_stopword_breaks(self):
        from researcher_profiles.openalex import paper_key_phrases

        papers = [
            _paper("A model of the genome"),
            _paper("A model of the genome"),
        ]
        # "of the" breaks the run: no phrase spans a stopword.
        assert paper_key_phrases(papers) == []
        assert paper_key_phrases(papers, min_papers=1) == []

    def test_top_n_and_dicts(self):
        from researcher_profiles.openalex import paper_key_phrases

        papers = [{"name": "deep learning; gene regulation"}] * 3
        assert paper_key_phrases(papers, top_n=1) == ["deep learning"]


class TestTopicMatches:
    def test_declared_positive_and_inferred_count(self):
        from researcher_profiles.openalex import topic_matches
        from researcher_profiles.schema import ResearchInterest

        def ri(code, weight=None, share=None):
            return ResearchInterest.model_validate(
                {
                    "concept": {
                        "@id": f"https://openalex.org/{code}",
                        "system": "https://openalex.org/topics",
                        "code": code,
                        "display": f"Topic {code}",
                    },
                    "weight": weight,
                    "method": "declared" if weight is not None else "inferred",
                    "generator": "user" if weight is not None else "openalex-topics@2026-09",
                    "assertedAt": "2026-09-25T12:00:00Z",
                    "evidence": {"papers": ["W1"], "share": share} if share else None,
                }
            )

        interests = [ri("T1", 1), ri("T2", -0.5), ri("T3", share=0.3), ri("T4")]
        got = topic_matches(["T4", "https://openalex.org/T3", "T2", "T1", "T9"], interests)
        assert got == ["Topic T3", "Topic T1"]


# ---------------------------------------------------------------------------
# fetch_work: single-work point lookup (HTTP layer stubbed)
# ---------------------------------------------------------------------------


def _raises_http_error(path, params):
    """A client ``get`` stub that fails the way a real 404 does."""
    from researcher_profiles.openalex_client import OpenAlexHTTPError

    raise OpenAlexHTTPError(f"OpenAlex {path}: HTTP 404")


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
    def test_normalizes_the_ref_and_parses(self, ref):
        import researcher_profiles.openalex as oa

        client = FakeClient(lambda path, params: self._raw())
        rec = oa.fetch_work(client, ref)
        assert rec is not None
        assert rec.title == "A point-lookup paper"
        assert rec.openalex_id == "W2741809807"
        assert rec.abstract == "Novel method"
        assert client.calls == [("/works/W2741809807", {})]

    @pytest.mark.parametrize(
        "stub,ref",
        [
            (_raises_http_error, "W_missing"),
            # No title/year -> parse_work returns None -> fetch_work returns None.
            (lambda path, params: {"id": "x"}, "W_bad"),
            # An empty ref never reaches the client.
            (None, ""),
        ],
        ids=["http-error", "unparseable-work", "empty-id"],
    )
    def test_returns_none(self, stub, ref):
        import researcher_profiles.openalex as oa

        client = FakeClient(stub or (lambda path, params: pytest.fail("no call expected")))
        assert oa.fetch_work(client, ref) is None

    def test_budget_error_still_raises(self):
        import researcher_profiles.openalex as oa
        from researcher_profiles.openalex_client import OpenAlexBudgetError

        def over_budget(path, params):
            raise OpenAlexBudgetError("OpenAlex /works/W1: HTTP 429")

        with pytest.raises(OpenAlexBudgetError):
            oa.fetch_work(FakeClient(over_budget), "W1")


# ---------------------------------------------------------------------------
# topic_prevalence: inferred interests from a corpus's topics
# ---------------------------------------------------------------------------


class TestTopicPrevalence:
    def test_share_counts_each_paper_once_and_carries_no_weight(self):
        from researcher_profiles.openalex import topic_prevalence

        papers = [
            {"openalex_id": "W1", "topics": ["T10222", "T11289", "T10222"]},
            {"openalex_id": "W2", "topics": ["T10222"]},
            {"openalex_id": "W3", "topics": ["T11289"]},
            {"openalex_id": "W4", "topics": ["T12345"]},
            {"openalex_id": "W5"},  # no topic data: neither helps nor hurts
        ]
        out = topic_prevalence(papers, min_share=0.3)
        assert [(e.concept.code, e.evidence.share, e.evidence.papers) for e in out] == [
            ("T10222", 0.5, ["W1", "W2"]),
            ("T11289", 0.5, ["W1", "W3"]),
        ]
        assert all(e.weight is None and e.method == "inferred" for e in out)
        assert out[0].generator.startswith("openalex-topics@")
        assert out[0].concept.version  # resolved against the pinned copy

    def test_an_unknown_topic_is_kept_unversioned(self, caplog):
        from researcher_profiles.openalex import topic_prevalence

        out = topic_prevalence(
            [{"openalex_id": "W1", "topics": ["T99999999"]}], names={"T99999999": "New topic"}
        )
        [entry] = out
        assert (entry.concept.code, entry.concept.display) == ("T99999999", "New topic")
        assert entry.concept.version is None
        assert "not in the pinned topic list" in caplog.text


class TestTopicScore:
    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            ({"weight": 1}, 1.0),
            ({"weight": -0.5}, -0.5),
            ({"weight": -1}, None),
            ({"method": "inferred", "evidence": {"papers": ["W1"], "share": 0.4}}, 0.2),
            ({"method": "inferred"}, 0.0),
        ],
        ids=["boost", "push-down", "hard-exclude", "inferred-share-at-half", "no-weight-no-share"],
    )
    def test_score_for_one_shared_topic(self, entry, expected):
        from researcher_profiles.openalex import topic_score
        from researcher_profiles.schema import ResearchInterest

        interest = ResearchInterest.model_validate(
            {
                "concept": {
                    "@id": "https://openalex.org/T1",
                    "system": "https://openalex.org/topics",
                    "code": "T1",
                    "display": "T1",
                },
                "method": "declared",
                "generator": "user",
                "assertedAt": "2026-09-25T12:00:00Z",
                **entry,
            }
        )
        score = topic_score(["https://openalex.org/T1", "T2"], [interest])
        assert score == (None if expected is None else pytest.approx(expected))


class TestMatchPapers:
    def test_exact_first_then_close_same_year(self):
        from researcher_profiles.openalex import match_papers
        from researcher_profiles.schema import PaperRecord

        works = [
            PaperRecord(title="Region sets: a new method!", year=2020, openalex_id="W1"),
            PaperRecord(
                title="Chromatin accessibility in single cells", year=2021, openalex_id="W2"
            ),
            PaperRecord(
                title="Chromatin accessibility in single cell", year=2019, openalex_id="W3"
            ),
        ]
        papers = [
            _paper("Region Sets - A New Method"),
            _paper("Chromatin accessibility in single cell"),
            _paper("Unmatched"),
        ]
        papers[0].year = 2020
        papers[1].year = 2021
        papers[2].year = 2021
        pairs = match_papers(papers, works)
        got = {p.title: w.openalex_id for p, w in pairs}
        # The second paper's exact twin is W3 (different year is fine for exact).
        assert got == {
            "Region Sets - A New Method": "W1",
            "Chromatin accessibility in single cell": "W3",
        }

    def test_close_needs_same_year(self):
        from researcher_profiles.openalex import title_match

        a = _paper("Chromatin accessibility in single cells")
        b = _paper("Chromatin accessibility in single-cell")
        a.year, b.year = 2021, 2021
        assert title_match(a, b)
        b.year = 2020
        assert not title_match(a, b)

    def test_author_works_queries_by_orcid(self):
        import researcher_profiles.openalex as oa

        client = FakeClient(
            lambda path, params: {
                "results": [_fetch_work("W1", "Mine")],
                "meta": {"next_cursor": None},
            }
        )
        out = oa.fetch_author_works(client, "https://orcid.org/0000-0001-2345-6789")
        assert [w.openalex_id for w in out] == ["W1"]
        assert client.calls[0][1]["filter"] == "author.orcid:0000-0001-2345-6789"
