"""The build clock, every timestamp the SDK stamps onto its own work.

Caches, indexes, registries, graph builds, export bundles and site manifests
all record *when this build ran*. That is one contract, and this module is its
one implementation, so no module keeps a private copy that could drift.

:func:`now_iso` honours ``SOURCE_DATE_EPOCH``: with it set, a rebuild of the
same inputs produces byte-identical outputs, which is what a reproducible build
means. Every build stamp goes through it.

This is not the ``dateModified`` clock.
:mod:`researcher_profiles.utils.date_modified` stamps a claim about a profile's
*content* (for example, "this document was last regenerated at…"), which a
consumer reads out loud as a staleness disclosure. Feeding that field a
pinned epoch could only make it lie, so :func:`utc_now_iso` never consults
the environment and ``stamp_date_modified`` calls it, not :func:`now_iso`.
See that module's docstring for why its trigger is content rather than time.

Pure stdlib, no package imports: this is a leaf, importable from anywhere
including the core-only, import-cheap paths in ``export.py``.
"""

import os
from datetime import datetime, timezone

__all__ = ["now_iso", "utc_now_iso"]


def utc_now_iso(moment: datetime | None = None) -> str:
    """An ISO-8601 UTC timestamp at second precision.

    Second precision, not microsecond: the value is written into a
    version-controlled document and read by humans, and sub-second digits
    claim a resolution that "when this document was regenerated" does not
    have. Matches the shape of the worked example in
    ``docs/rp-spec/index.md`` (``2026-07-30T00:00:00+00:00``).
    """
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def now_iso(now: str | None = None) -> str:
    """The build timestamp: an explicit override, ``SOURCE_DATE_EPOCH``, or now.

    ``now`` is a pre-formatted string that passes straight through, so a caller
    that already has a build stamp (a bundle being re-rendered, a test pinning
    an instant) does not have to reach for the clock at all.
    """
    if now:
        return now
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    return utc_now_iso()
