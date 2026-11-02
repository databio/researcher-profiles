"""Current rank: the "where are you in your career" answer, and the rule that
keeps a person's own answer ahead of a model's guess.

The rank lives on :class:`~._parts.CareerStage` (``current_rank`` plus
``source``). Everything here is pure: no I/O, no clock (callers pass
``today``).

- :func:`current_rank_of` reads the rank from any of the shapes a caller
  holds (a document, a stage, a raw document dict, a trimmed API record).
- :func:`self_reported_stage` turns an owner's answer into a full stage whose
  other facts agree with it.
- :func:`merge_career_stage` is the one rule every overwrite path (build,
  push, merge, lens) applies: a self-reported stage is kept
  and only gains facts it lacks.

The TypeScript mirror is ``prosopia/frontend/src/manage/currentRank.ts``;
keep the two in sync.
"""

from datetime import date
from typing import Any, Mapping, get_args

from ._parts import CareerStage, CurrentRank

#: Every rank, in the schema's order.
CURRENT_RANKS: tuple[str, ...] = get_args(CurrentRank)

#: Ranks for people still in training.
TRAINEE_RANKS: frozenset[str] = frozenset({"undergraduate", "graduate_student", "postdoc"})

#: Ranks for faculty, who run an independent program.
FACULTY_RANKS: frozenset[str] = frozenset(
    {"assistant_professor", "associate_professor", "professor", "research_faculty"}
)
#: Keys that mark a bare mapping as a stage (not a document without one).
_STAGE_KEYS = frozenset({"current_rank", "independence", "tenure_status", "as_of"})

SELF_REPORT_EVIDENCE = "Reported by the profile owner."


def _stage_dict(obj: Any) -> Mapping[str, Any] | None:
    """The career stage of ``obj`` as a mapping, or ``None`` when it has none.

    Accepts a ``ProfileDocument``, a ``CareerStage``, a raw document dict, a
    trimmed record (``{"fields": {"career_stage": ...}}``), a stage dict, or
    ``None``.
    """
    if obj is None:
        return None
    if isinstance(obj, CareerStage):
        return obj.model_dump(mode="json")
    if isinstance(obj, Mapping):
        if "fields" in obj and isinstance(obj["fields"], Mapping):
            return _stage_dict(obj["fields"].get("career_stage"))
        if "career_stage" in obj:
            return _stage_dict(obj["career_stage"])
        if _STAGE_KEYS & set(obj):
            return obj
        return None
    stage = getattr(obj, "career_stage", None)
    if stage is not None:
        return _stage_dict(stage)
    fields = getattr(obj, "fields", None)
    if isinstance(fields, Mapping):
        return _stage_dict(fields.get("career_stage"))
    return None


def current_rank_of(obj: Any) -> str:
    """The person's current rank, or ``"unknown"``.

    Returns a :data:`CurrentRank` value or ``"unknown"``.
    """
    stage = _stage_dict(obj)
    rank = stage.get("current_rank") if stage else None
    return rank if rank in CURRENT_RANKS else "unknown"


def is_trainee(rank: str | None) -> bool:
    """True for undergraduates, graduate students and postdocs."""
    return rank in TRAINEE_RANKS


def current_rank_is_self_reported(obj: Any) -> bool:
    """True when the stage is the owner's own answer and names a rank."""
    stage = _stage_dict(obj)
    return bool(
        stage and stage.get("source") == "self_reported" and stage.get("current_rank") is not None
    )


def self_reported_stage(rank: str, existing: CareerStage | None, today: date) -> CareerStage:
    """The stage recording an owner's answer ``rank``, consistent with it.

    Starts from ``existing`` (or a blank stage), stamps it as the owner's
    answer, and sets ``independence`` to what the rank implies so eligibility
    code sees agreeing facts.
    """
    if rank not in CURRENT_RANKS:
        raise ValueError(f"unknown current rank {rank!r}; allowed: {', '.join(CURRENT_RANKS)}")
    if existing is not None:
        base = existing.model_dump(mode="json")
    else:
        base = {
            "tenure_status": "unknown",
            "independence": "unknown",
            "sources_checked": [],
            "terminal_degree_year": None,
            "terminal_degree_type": None,
            "clinical_training_end_year": None,
            "first_independent_appointment_year": None,
            "first_r01_equivalent_year": None,
        }
    base.update(
        current_rank=rank,
        source="self_reported",
        confidence="high",
        as_of=today.isoformat(),
        evidence=SELF_REPORT_EVIDENCE,
    )
    if rank in TRAINEE_RANKS:
        base["independence"] = "trainee"
    elif rank in FACULTY_RANKS:
        base["independence"] = "independent"
    elif not base.get("independence"):
        base["independence"] = "unknown"
    return CareerStage.model_validate(base)


_FILLABLE_YEARS = (
    "first_independent_appointment_year",
    "clinical_training_end_year",
    "first_r01_equivalent_year",
)


def merge_career_stage(
    kept: CareerStage | None, incoming: CareerStage | None
) -> CareerStage | None:
    """Combine a stored stage with a newly written one.

    A self-reported ``kept`` beats a non-self-reported ``incoming``: it stays,
    and only its empty facts are filled from ``incoming`` (the degree year and
    type move together, so the pair never mixes two authors). In every other
    case ``incoming`` wins.
    """
    if kept is None:
        return incoming
    if incoming is None:
        return kept
    if kept.source != "self_reported" or incoming.source == "self_reported":
        return incoming
    out = kept.model_dump(mode="json")
    for key in _FILLABLE_YEARS:
        if out.get(key) is None:
            out[key] = getattr(incoming, key)
    if out.get("tenure_status") == "unknown":
        out["tenure_status"] = incoming.tenure_status
    if out.get("terminal_degree_year") is None and out.get("terminal_degree_type") is None:
        out["terminal_degree_year"] = incoming.terminal_degree_year
        out["terminal_degree_type"] = incoming.terminal_degree_type
    checked = list(out.get("sources_checked") or [])
    for s in incoming.sources_checked:
        if s not in checked:
            checked.append(s)
    out["sources_checked"] = checked
    return CareerStage.model_validate(out)


__all__ = [
    "CURRENT_RANKS",
    "CurrentRank",
    "FACULTY_RANKS",
    "SELF_REPORT_EVIDENCE",
    "TRAINEE_RANKS",
    "current_rank_is_self_reported",
    "current_rank_of",
    "is_trainee",
    "merge_career_stage",
    "self_reported_stage",
]
