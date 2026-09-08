"""The two clocks, and the line between them.

``clock.now_iso`` is the build clock and honours ``SOURCE_DATE_EPOCH`` so a
reproducible build is reproducible. ``clock.utc_now_iso`` is the wall clock and
must not, because ``dateModified`` is a claim about a profile's content and a
pinned epoch would make that claim false. The last class below asserts that
split end to end, since it is the guarantee that rots silently if untested.
"""

from datetime import datetime, timedelta, timezone

import pytest

from researcher_profiles.utils.clock import now_iso, utc_now_iso
from researcher_profiles.utils.date_modified import DATE_MODIFIED_KEY, stamp_date_modified

DOC = {"name": "Ada Lovelace", "level": "full"}


class TestClock:
    def test_utc_now_iso_drops_sub_second_precision(self) -> None:
        stamped = utc_now_iso(datetime(2026, 3, 4, 9, 30, 0, 123456, tzinfo=timezone.utc))
        assert stamped == "2026-03-04T09:30:00+00:00"

    def test_naive_datetimes_are_read_as_utc(self) -> None:
        assert utc_now_iso(datetime(2026, 3, 4, 9, 30, 0)) == "2026-03-04T09:30:00+00:00"

    def test_other_offsets_are_normalized_to_utc(self) -> None:
        eastern = timezone(timedelta(hours=-5))
        moment = datetime(2026, 3, 4, 4, 30, 0, tzinfo=eastern)
        assert utc_now_iso(moment) == "2026-03-04T09:30:00+00:00"

    def test_default_clock_is_now(self) -> None:
        assert utc_now_iso().startswith(str(datetime.now(timezone.utc).year))


class TestBuildClock:
    def test_source_date_epoch_pins_the_build_stamp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
        assert now_iso() == "2023-11-14T22:13:20+00:00"

    def test_without_the_epoch_the_build_stamp_is_the_wall_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
        assert now_iso().startswith(str(datetime.now(timezone.utc).year))

    def test_an_explicit_stamp_wins_over_the_epoch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
        assert now_iso("2026-03-04T09:30:00+00:00") == "2026-03-04T09:30:00+00:00"


class TestDateModifiedIgnoresTheEpoch:
    """``dateModified`` is content, not build machinery, so it cannot be pinned."""

    def test_wall_clock_ignores_source_date_epoch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
        assert not utc_now_iso().startswith("2023-11-14")

    def test_stamp_date_modified_ignores_source_date_epoch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
        stamped = stamp_date_modified(DOC, None)[DATE_MODIFIED_KEY]
        assert not stamped.startswith("2023-11-14")
        assert stamped.startswith(str(datetime.now(timezone.utc).year))
