"""Current rank: reading it from every shape, stamping an owner's answer, and the merge rule."""

from datetime import date

import pytest

from researcher_profiles import (
    CURRENT_RANKS,
    TRAINEE_RANKS,
    CareerStage,
    current_rank_is_self_reported,
    current_rank_of,
    is_trainee,
    merge_career_stage,
    self_reported_stage,
)
from researcher_profiles.schema import ProfileDocument

TODAY = date(2026, 10, 20)
DOC = {"name": "A", "rid": "0000-0002-1825-0097", "provenance": "third_party"}

BASE = {
    "as_of": "2025-01-01",
    "tenure_status": "unknown",
    "independence": "unknown",
    "evidence": "ORCID.",
    "confidence": "medium",
}


def _stage(**kw) -> CareerStage:
    return CareerStage.model_validate({**BASE, **kw})


SHAPES = {
    "stage": lambda d: CareerStage.model_validate(d),
    "document": lambda d: ProfileDocument.model_validate({**DOC, "career_stage": d}),
    "raw-doc": lambda d: {"name": "A", "career_stage": d},
    "record": lambda d: {"slug": "a", "fields": {"career_stage": d}},
}

READS = [
    ({"current_rank": "industry", "independence": "independent"}, "industry"),
    ({"current_rank": "postdoc"}, "postdoc"),
    ({"current_rank": "graduate_student"}, "graduate_student"),
    ({"current_rank": "professor"}, "professor"),
    ({"current_rank": None}, "unknown"),
    ({"independence": "independent"}, "unknown"),
    ({}, "unknown"),
]


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("facts, want", READS)
def test_current_rank_of_every_shape(shape, facts, want):
    assert current_rank_of(SHAPES[shape]({**BASE, **facts})) == want


@pytest.mark.parametrize(
    "obj",
    [None, {"name": "A"}, {"fields": {"career_stage": None}}, {"career_stage": None}],
    ids=["none", "doc-without-stage", "record-withheld", "doc-null-stage"],
)
def test_missing_stage_is_unknown(obj):
    assert current_rank_of(obj) == "unknown"
    assert current_rank_is_self_reported(obj) is False


def test_document_without_stage_is_unknown():
    assert current_rank_of(ProfileDocument.model_validate(DOC)) == "unknown"


def test_trimmed_record_scalars_read():
    # The record drops None and lists, so only some keys survive.
    record = {"fields": {"career_stage": {"current_rank": "postdoc", "source": "self_reported"}}}
    assert current_rank_of(record) == "postdoc"
    assert current_rank_is_self_reported(record)


def test_self_reported_needs_a_rank():
    assert not current_rank_is_self_reported(_stage(source="self_reported"))
    assert not current_rank_is_self_reported(_stage(current_rank="postdoc"))
    assert current_rank_is_self_reported(_stage(current_rank="postdoc", source="self_reported"))


def test_trainee_ranks():
    assert set(TRAINEE_RANKS) == {"undergraduate", "graduate_student", "postdoc"}
    assert all(rank in CURRENT_RANKS for rank in TRAINEE_RANKS)
    assert is_trainee("postdoc") and not is_trainee("professor") and not is_trainee("unknown")


def test_student_is_not_a_rank():
    assert "student" not in CURRENT_RANKS
    with pytest.raises(ValueError):
        _stage(current_rank="student")


@pytest.mark.parametrize(
    "rank, independence",
    [
        ("undergraduate", "trainee"),
        ("graduate_student", "trainee"),
        ("postdoc", "trainee"),
        ("assistant_professor", "independent"),
        ("research_faculty", "independent"),
        ("staff_scientist", "unknown"),
        ("clinician", "unknown"),
        ("industry", "unknown"),
        ("other", "unknown"),
    ],
)
def test_self_reported_stage_from_nothing(rank, independence):
    stage = self_reported_stage(rank, None, TODAY)
    assert stage.current_rank == rank
    assert stage.source == "self_reported"
    assert stage.confidence == "high"
    assert stage.as_of == "2026-10-20"
    assert stage.evidence == "Reported by the profile owner."
    assert stage.independence == independence
    assert stage.tenure_status == "unknown"
    assert stage.sources_checked == []
    assert stage.terminal_degree_year is None


@pytest.mark.parametrize(
    "rank, existing, independence",
    [
        ("professor", {"current_rank": "postdoc", "independence": "trainee"}, "independent"),
        ("graduate_student", {"current_rank": "professor"}, "trainee"),
        ("industry", {"current_rank": "staff_scientist", "independence": "mentored"}, "mentored"),
        ("clinician", {}, "unknown"),
    ],
)
def test_self_reported_stage_over_existing(rank, existing, independence):
    prior = _stage(terminal_degree_year=2010, terminal_degree_type="research", **existing)
    stage = self_reported_stage(rank, prior, TODAY)
    assert (stage.current_rank, stage.independence) == (rank, independence)
    assert stage.terminal_degree_year == 2010
    assert stage.source == "self_reported"


def test_self_reported_stage_rejects_bad_rank():
    with pytest.raises(ValueError, match="allowed"):
        self_reported_stage("wizard", None, TODAY)


class TestMergeCareerStage:
    def test_none_sides(self):
        s = _stage()
        assert merge_career_stage(None, s) is s
        assert merge_career_stage(s, None) is s
        assert merge_career_stage(None, None) is None

    def test_inferred_kept_is_replaced(self):
        kept, incoming = _stage(current_rank="postdoc"), _stage(current_rank="professor")
        assert merge_career_stage(kept, incoming) is incoming

    def test_both_self_reported_takes_incoming(self):
        kept = _stage(current_rank="postdoc", source="self_reported")
        incoming = _stage(current_rank="professor", source="self_reported")
        assert merge_career_stage(kept, incoming) is incoming

    def test_self_reported_is_kept_and_filled(self):
        kept = self_reported_stage("graduate_student", None, TODAY)
        kept = kept.model_copy(update={"sources_checked": ["training"]})
        incoming = _stage(
            current_rank="professor",
            independence="independent",
            tenure_status="tenured",
            first_independent_appointment_year=2015,
            clinical_training_end_year=2012,
            first_r01_equivalent_year=2018,
            terminal_degree_year=2010,
            terminal_degree_type="research",
            sources_checked=["grants", "training"],
            confidence="low",
        )
        out = merge_career_stage(kept, incoming)
        assert out.source == "self_reported"
        assert out.current_rank == "graduate_student"
        assert out.independence == "trainee"
        assert out.confidence == "high"
        assert out.evidence == "Reported by the profile owner."
        assert out.as_of == "2026-10-20"
        assert out.first_independent_appointment_year == 2015
        assert out.clinical_training_end_year == 2012
        assert out.first_r01_equivalent_year == 2018
        assert out.tenure_status == "tenured"
        assert (out.terminal_degree_year, out.terminal_degree_type) == (2010, "research")
        assert out.sources_checked == ["training", "grants"]

    def test_kept_facts_are_not_overwritten(self):
        kept = _stage(
            current_rank="postdoc",
            source="self_reported",
            tenure_status="non_tenure_track",
            first_r01_equivalent_year=2020,
        )
        incoming = _stage(tenure_status="tenured", first_r01_equivalent_year=2010)
        out = merge_career_stage(kept, incoming)
        assert out.tenure_status == "non_tenure_track"
        assert out.first_r01_equivalent_year == 2020

    @pytest.mark.parametrize(
        "kept_pair, want",
        [
            ((None, None), (2010, "research")),
            ((2014, None), (2014, None)),
            ((None, "clinical"), (None, "clinical")),
        ],
        ids=["both-null-fills-pair", "year-set-keeps", "type-set-keeps"],
    )
    def test_degree_pair_is_atomic(self, kept_pair, want):
        year, kind = kept_pair
        kept = _stage(
            current_rank="postdoc",
            source="self_reported",
            terminal_degree_year=year,
            terminal_degree_type=kind,
        )
        incoming = _stage(terminal_degree_year=2010, terminal_degree_type="research")
        out = merge_career_stage(kept, incoming)
        assert (out.terminal_degree_year, out.terminal_degree_type) == want


def test_old_stage_without_new_keys_is_inferred():
    stage = CareerStage.model_validate(BASE)
    assert stage.current_rank is None
    assert stage.source == "inferred"
