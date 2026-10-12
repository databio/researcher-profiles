"""The two clocks, and the line between them.

``now_iso`` is the build clock and honours ``SOURCE_DATE_EPOCH``; ``utc_now_iso`` is the
wall clock and must not, since ``dateModified`` is a claim about content a pin would falsify.
"""

from datetime import datetime, timedelta, timezone

import pytest

from researcher_profiles.utils.clock import now_iso, utc_now_iso
from researcher_profiles.utils.date_modified import DATE_MODIFIED_KEY, stamp_date_modified

DOC = {"name": "Ada Lovelace", "level": "full"}


NOW_YEAR = object()
PINNED = "2026-03-04T09:30:00+00:00"


class TestClock:
    @pytest.mark.parametrize(
        "moment",
        [
            datetime(2026, 3, 4, 9, 30, 0, 123456, tzinfo=timezone.utc),
            datetime(2026, 3, 4, 9, 30, 0),
            datetime(2026, 3, 4, 4, 30, 0, tzinfo=timezone(timedelta(hours=-5))),
        ],
        ids=["drops-sub-second", "naive-read-as-utc", "offset-normalized"],
    )
    def test_utc_now_iso_normalizes(self, moment) -> None:
        assert utc_now_iso(moment) == PINNED

    def test_default_clock_is_now(self) -> None:
        assert utc_now_iso().startswith(str(datetime.now(timezone.utc).year))


class TestBuildClock:
    @pytest.mark.parametrize(
        "epoch,explicit,expected_prefix",
        [
            ("1700000000", None, "2023-11-14T22:13:20+00:00"),
            (None, None, NOW_YEAR),
            ("1700000000", PINNED, PINNED),
        ],
        ids=["epoch-pins-stamp", "no-epoch-is-wall-clock", "explicit-wins-over-epoch"],
    )
    def test_now_iso(
        self, monkeypatch: pytest.MonkeyPatch, epoch, explicit, expected_prefix
    ) -> None:
        if epoch is None:
            monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
        else:
            monkeypatch.setenv("SOURCE_DATE_EPOCH", epoch)
        if expected_prefix is NOW_YEAR:
            expected_prefix = str(datetime.now(timezone.utc).year)
        assert now_iso(explicit).startswith(expected_prefix)


class TestDateModifiedIgnoresTheEpoch:
    """``dateModified`` is content, not build machinery, so it cannot be pinned."""

    def test_stamp_date_modified_ignores_source_date_epoch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
        stamped = stamp_date_modified(DOC, None)[DATE_MODIFIED_KEY]
        assert not stamped.startswith("2023-11-14")
        assert stamped.startswith(str(datetime.now(timezone.utc).year))
