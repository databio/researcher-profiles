"""The build clock: every timestamp the SDK stamps onto its own work.

:func:`now_iso` honours ``SOURCE_DATE_EPOCH``, so a rebuild of the same inputs
is byte-identical. :func:`utc_now_iso` never reads the environment; it is the
``dateModified`` clock (see :mod:`researcher_profiles.utils.date_modified`
for why that stamp must not use a pinned epoch).

Stdlib only, no package imports.
"""

import os
from datetime import datetime, timezone

__all__ = ["now_iso", "utc_now_iso"]


def utc_now_iso(moment: datetime | None = None) -> str:
    """An ISO-8601 UTC timestamp at second precision.

    Second precision: sub-second digits claim a resolution the stamp does not
    have. Shape matches ``docs/rp-spec/index.md`` (``2026-07-30T00:00:00+00:00``).
    """
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def now_iso(now: str | None = None) -> str:
    """The build timestamp: an explicit override, ``SOURCE_DATE_EPOCH``, or now.

    ``now`` is a pre-formatted string that passes straight through.
    """
    if now:
        return now
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    return utc_now_iso()
