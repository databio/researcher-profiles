"""Career level: the coarse "where are you in your career" answer, and the rule
that keeps a person's own answer ahead of a model's guess.

The level lives on :class:`~._parts.CareerStage` (``career_level`` plus
``source``). Everything here is pure: no I/O, no clock (callers pass
``today``).

- :func:`career_level_of` reads a level from any of the shapes a caller holds
  (a document, a stage, a raw document dict, a trimmed API record), falling
  back to ``current_rank`` / ``independence`` for stages written before the
  field existed.
- :func:`self_reported_stage` turns an owner's answer into a full stage whose
  other facts agree with it.
- :func:`merge_career_stage` is the one rule every overwrite path (build,
  push, merge, lens) applies: a self-reported stage is kept
  and only gains facts it lacks.

The TypeScript mirror is ``prosopia/frontend/src/manage/careerLevel.ts``;
keep the two in sync.
"""

from datetime import date
from typing import Any, Literal, Mapping

from ._parts import CareerStage

CareerLevel = Literal[
    "undergraduate",
    "graduate_student",
    "postdoc",
    "faculty",
    "clinician",
    "industry",
    "other",
]

#: Every level, in display order.
CAREER_LEVELS: tuple[str, ...] = (
    "undergraduate",
    "graduate_student",
    "postdoc",
    "faculty",
    "clinician",
    "industry",
    "other",
)

#: Levels for people still in training.
TRAINEE_LEVELS: frozenset[str] = frozenset({"undergraduate", "graduate_student", "postdoc"})

_FACULTY_RANKS = frozenset(
    {"assistant_professor", "associate_professor", "professor", "research_faculty"}
)
_RANK_TO_LEVEL: dict[str, str] = {
    "postdoc": "postdoc",
    **{rank: "faculty" for rank in _FACULTY_RANKS},
    "student": "graduate_student",
    "staff_scientist": "other",
    "other": "other",
}
#: Keys that mark a bare mapping as a stage (not a document without one).
_STAGE_KEYS = frozenset(
    {"career_level", "current_rank", "independence", "tenure_status", "as_of"}
)

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


def career_level_of(obj: Any) -> str:
    """The person's career level, or ``"unknown"``.

    Returns a :data:`CareerLevel` value or ``"unknown"``. ``career_level``
    wins when set; otherwise the level is read from ``current_rank``, then
    from ``independence == "independent"`` (faculty).
    """
    stage = _stage_dict(obj)
    if not stage:
        return "unknown"
    level = stage.get("career_level")
    if level in CAREER_LEVELS:
        return level
    rank = stage.get("current_rank")
    if rank in _RANK_TO_LEVEL:
        return _RANK_TO_LEVEL[rank]
    if stage.get("independence") == "independent":
        return "faculty"
    return "unknown"


def is_trainee(level: str | None) -> bool:
    """True for undergraduates, graduate students and postdocs."""
    return level in TRAINEE_LEVELS


def career_level_is_self_reported(obj: Any) -> bool:
    """True when the stage is the owner's own answer and names a level."""
    stage = _stage_dict(obj)
    return bool(
        stage and stage.get("source") == "self_reported" and stage.get("career_level") is not None
    )


def self_reported_stage(
    level: str, existing: CareerStage | None, today: date
) -> CareerStage:
    """The stage recording an owner's answer ``level``, consistent with it.

    Starts from ``existing`` (or a blank stage), stamps it as the owner's
    answer, and sets ``current_rank`` / ``independence`` to what the level
    implies so eligibility code sees agreeing facts.
    """
    if level not in CAREER_LEVELS:
        raise ValueError(f"unknown career level {level!r}; allowed: {', '.join(CAREER_LEVELS)}")
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
            "current_rank": None,
        }
    base.update(
        career_level=level,
        source="self_reported",
        confidence="high",
        as_of=today.isoformat(),
        evidence=SELF_REPORT_EVIDENCE,
    )
    if level in ("undergraduate", "graduate_student"):
        base.update(current_rank="student", independence="trainee")
    elif level == "postdoc":
        base.update(current_rank="postdoc", independence="trainee")
    elif level == "faculty":
        base["independence"] = "independent"
        if base.get("current_rank") not in _FACULTY_RANKS:
            base["current_rank"] = None
    else:  # clinician, industry, other
        if not base.get("current_rank"):
            base["current_rank"] = "other"
        if not base.get("independence"):
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
    "CAREER_LEVELS",
    "CareerLevel",
    "SELF_REPORT_EVIDENCE",
    "TRAINEE_LEVELS",
    "career_level_is_self_reported",
    "career_level_of",
    "is_trainee",
    "merge_career_stage",
    "self_reported_stage",
]
