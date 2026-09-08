"""``prof.coverage``: what this profile covers, and how fresh it is.

Defines :class:`CoverageManager`, the object ``ResearcherProfile.coverage``
hands back (``prof.coverage.get()`` / ``.staleness()`` / ``.recent_work()`` /
``.last_updated``), together with the functions behind it: computing (or
loading the cached) :class:`~researcher_profiles.models.results.Coverage`,
staleness scoring, and recent-work selection. Importing this module has no
side effect on the profile class.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from ..models.results import Coverage
from ..utils.paths import cache_dir

_SINCE_RE = re.compile(r"^(\d+)([dwmy])$")


def _first_sentence(text: str | None, *, cap: int = 240) -> str | None:
    if not text:
        return None
    text = text.strip()
    m = re.search(r"([\.!?])(\s|$)", text)
    if m:
        end = m.start(1) + 1
        sent = text[:end].strip()
    else:
        sent = text
    return sent[:cap]


def _file_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _root(profile) -> Path:
    """The directory this profile's coverage cache and file mtimes live under."""
    return profile.require_directory("coverage")


def _last_updated(self) -> datetime:
    """``max(mtime)`` across profile files + newest paper year (Jan 1)."""
    root = _root(self)
    paths = [
        root / "profile.jsonld",
        root / "personality" / "expertise.md",
        root / "personality" / "SOUL.md",
        root / "sources" / "papers.jsonld",
    ]
    mtimes = [_file_mtime(p) for p in paths]
    candidate = max(mtimes) if mtimes else 0.0
    dt_file = (
        datetime.fromtimestamp(candidate, tz=timezone.utc)
        if candidate
        else datetime.fromtimestamp(0, tz=timezone.utc)
    )

    # Newest paper year as Jan 1 floor.
    years = [p.year for p in self.papers if getattr(p, "year", None)]
    if years:
        dt_paper = datetime(max(years), 1, 1, tzinfo=timezone.utc)
        if dt_paper > dt_file:
            return dt_paper
    return dt_file


def _staleness(self) -> dict:
    now = datetime.now(timezone.utc)
    lu = _last_updated(self)
    days_since_update = (now - lu).days
    years = [p.year for p in self.papers if getattr(p, "year", None)]
    if years:
        dt_paper = datetime(max(years), 1, 1, tzinfo=timezone.utc)
        days_since_newest_paper = (now - dt_paper).days
    else:
        days_since_newest_paper = days_since_update
    score = min(1.0, max(days_since_update, days_since_newest_paper) / 365.0)
    if score < 0.25:
        label = "fresh"
    elif score < 0.75:
        label = "aging"
    else:
        label = "stale"
    return {
        "days_since_update": days_since_update,
        "days_since_newest_paper": days_since_newest_paper,
        "score": score,
        "label": label,
    }


def _parse_since(since: str | int) -> int:
    """Return cutoff year given a `since` spec.

    Accepts ``"1y"``, ``"6m"``, ``"30d"``, ``"4w"`` or a bare int (years).
    """
    now = datetime.now(timezone.utc)
    if isinstance(since, int):
        return now.year - since
    m = _SINCE_RE.match(since)
    if not m:
        raise ValueError(f"unparseable since spec: {since!r}")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "y":
        return now.year - n
    if unit == "m":
        # Approximate: anything within roughly N months converts to a year floor.
        cutoff_dt = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        # subtract n months
        total_months = cutoff_dt.year * 12 + cutoff_dt.month - 1 - n
        cutoff_year = total_months // 12
        return cutoff_year
    if unit == "w":
        days = n * 7
    else:
        days = n
    # day-resolution: convert by approximating cutoff year
    cutoff_ts = now.timestamp() - days * 86400
    return datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).year


def _recent_work(self, since: str | int = "1y", limit: int = 10):
    from .cite import _paper_to_citation

    cutoff_year = _parse_since(since)
    eligible = [p for p in self.papers if getattr(p, "year", None) and p.year >= cutoff_year]

    def _key(p):
        # primary: year desc; secondary: corresponding/last authors first.
        is_corr = bool(getattr(p, "is_corresponding", False))
        pos = getattr(p, "author_position", "") or ""
        prio = 0 if (is_corr or pos in ("last", "first")) else 1
        return (-p.year, prio)

    eligible.sort(key=_key)
    return [_paper_to_citation(p) for p in eligible[:limit]]


def _extract_topics(expertise_md: str, metadata) -> list[str]:
    """Topics from `## ` headings in expertise, falling back to metadata."""
    topics: list[str] = []
    for line in (expertise_md or "").splitlines():
        s = line.strip()
        if s.startswith("## ") and not s.startswith("### "):
            topics.append(s[3:].strip())
    if topics:
        return topics
    # Fall back to metadata.topics (additive field if present) or subfields.
    extra = getattr(metadata, "topics", None) if metadata else None
    if extra:
        return list(extra)
    return list(getattr(metadata, "subfields", []) or [])


def _load_cached_coverage(cache_path: Path, current_lu: datetime) -> Coverage | None:
    """The cached :class:`Coverage`, or None when absent, stale, or unreadable."""
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        cached_lu = data.get("last_updated")
        if cached_lu and cached_lu == current_lu.isoformat():
            yr = data.get("year_range")
            return Coverage(
                name=data["name"],
                affiliation=data.get("affiliation"),
                one_liner=data.get("one_liner"),
                topics=list(data.get("topics", [])),
                expertise_summary=data.get("expertise_summary", ""),
                year_range=tuple(yr) if yr else None,
                paper_count=data.get("paper_count", 0),
                last_updated=datetime.fromisoformat(cached_lu),
                staleness_label=data.get("staleness_label", ""),
                suggested_questions=list(data.get("suggested_questions", [])),
                coverage_caveats=list(data.get("coverage_caveats", [])),
            )
    except (OSError, ValueError, KeyError):
        pass
    return None


def _expertise_summary(expertise: str | None) -> str:
    """First ~3 sentences (~500 char cap) of the expertise body, no LLM."""
    text = (expertise or "").strip()
    # Strip leading title heading if present.
    body_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("# "):
            continue
        body_lines.append(line)
    body = " ".join(line.strip() for line in body_lines if line.strip())
    sentences = re.split(r"(?<=[\.!?])\s+", body)
    return " ".join(sentences[:3])[:500]


def _suggested_questions(self, name: str, topics: list[str]) -> list[str]:
    """Deterministic question templates from topics and recent work (max 5)."""
    suggested: list[str] = []
    for t in topics[:3]:
        suggested.append(f"What is {name}'s approach to {t}?")
    try:
        recent = _recent_work(self, since="2y", limit=2)
    except (OSError, ValueError, ValidationError):
        recent = []
    for r in recent:
        if topics:
            suggested.append(f"How does {r.title!r} relate to {topics[0]}?")
        else:
            suggested.append(f"Tell me about the paper {r.title!r}.")
    return suggested[:5]


def _write_coverage_cache(cache_path: Path, coverage_obj: Coverage) -> None:
    """Atomic write to meta/coverage.json; a failed write is not an error."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(coverage_obj.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, cache_path)
    except OSError:
        pass


def _coverage(self) -> Coverage:
    """Compute (or load cached) :class:`Coverage` for this profile."""
    cache_path = cache_dir(_root(self)) / "coverage.json"
    current_lu = _last_updated(self)

    cached = _load_cached_coverage(cache_path, current_lu)
    if cached is not None:
        return cached

    meta = self.metadata
    name = meta.name
    affiliation = getattr(meta, "affiliation", None)
    one_liner = _first_sentence(getattr(meta, "summary", None))
    topics = _extract_topics(self.expertise, meta)
    expertise_summary = _expertise_summary(self.expertise)

    years = [p.year for p in self.papers if getattr(p, "year", None)]
    year_range = (min(years), max(years)) if years else None
    paper_count = len(self.papers)

    stale = _staleness(self)
    staleness_label = stale["label"]

    suggested = _suggested_questions(self, name, topics)

    caveats: list[str] = []
    if year_range and paper_count:
        caveats.append(
            f"Index covers {paper_count} papers from {year_range[0]}-{year_range[1]}; "
            "preprints since then may be missing."
        )
    caveats.append(
        f"Profile last refreshed {stale['days_since_update']} days ago (label: {staleness_label})."
    )

    coverage_obj = Coverage(
        name=name,
        affiliation=affiliation,
        one_liner=one_liner,
        topics=topics,
        expertise_summary=expertise_summary,
        year_range=year_range,
        paper_count=paper_count,
        last_updated=current_lu,
        staleness_label=staleness_label,
        suggested_questions=suggested,
        coverage_caveats=caveats,
    )

    _write_coverage_cache(cache_path, coverage_obj)
    return coverage_obj


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class CoverageManager:
    """``prof.coverage``: what this profile covers, and how fresh it is."""

    def __init__(self, profile):
        self._profile = profile

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CoverageManager(slug={self._profile.slug!r})"

    @property
    def last_updated(self) -> datetime:
        """``max(mtime)`` across profile files, floored at the newest paper year."""
        return _last_updated(self._profile)

    def staleness(self) -> dict:
        """``{days_since_update, days_since_newest_paper, score, label}``."""
        return _staleness(self._profile)

    def recent_work(self, since: str | int = "1y", limit: int = 10):
        """Papers since ``since``, newest first, corresponding authorships first."""
        return _recent_work(self._profile, since=since, limit=limit)

    def get(self) -> Coverage:
        """The computed (or cached) :class:`Coverage`."""
        return _coverage(self._profile)


__all__ = ["CoverageManager"]
