"""``dateModified``: the only timestamp a published profile carries.

The consumer contract reads it as *when the profile document was last
regenerated* (``docs/rp-spec/index.md`` §3.1, ``docs/rp-spec/static-api.md``
§9) and feeds it to the staleness
disclosure a persona must deliver. That makes it a claim about the document's
**content**, not about the machinery that produced it. A build time, or
``datetime.now()`` at serialization time, would make an untouched profile
announce "last updated today", which converts a useful vintage into a
confident lie (databio/researcher-profiles#1).

So the trigger is the content itself: :func:`stamp_date_modified` compares the
document about to be written against the one already on disk, ignoring
``dateModified``, and advances the stamp only when they differ. Two
consequences follow:

* Unchanged content keeps its original date, forever, across any number of
  rewrites.
* An unstamped document does not acquire a stamp from a no-op write. If the
  bytes on disk already match, we do not know when they were written and must
  not guess. The spec's absent-``dateModified`` path
  (``researcher_profiles/skill/reference/failure-modes.md``) exists for exactly
  this case.

Comparison is on the *serialized* document, so a caller must apply this after
``ProfileDocument`` validation and dump: pydantic prunes ``None`` and empty
collections, and comparing an un-normalized input dict against a normalized
file would report a change on every write.

This module does not consult ``SOURCE_DATE_EPOCH``. Every
*build* stamp in the package does, through
:func:`researcher_profiles.utils.clock.now_iso`. But ``dateModified`` is a claim
about content, and a pinned epoch could only make that claim false. The wall
clock it does use, :func:`researcher_profiles.utils.clock.utc_now_iso`, is defined
next to ``now_iso`` so the split between the two is visible in one place.

Pure stdlib: a policy function plus one disk read.
"""

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .clock import utc_now_iso

__all__ = [
    "DATE_MODIFIED_KEY",
    "PROFILE_FILE",
    "content_changed",
    "read_published_document",
    "stamp_date_modified",
]

#: The published key. The pydantic field is ``date_modified``; everything on
#: disk and on the wire uses the schema.org alias.
DATE_MODIFIED_KEY = "dateModified"

#: The profile document's filename, relative to a profile directory.
PROFILE_FILE = "profile.jsonld"


def _published_content(doc: Any) -> dict[str, Any]:
    """Everything a consumer reads except the stamp itself.

    ``dateModified`` is excluded because it is the *answer*: including it
    would make every stamped document differ from its unstamped predecessor
    and the field would advance on every write, which is the bug.

    Nothing else is excluded. Manifest ``bytes``/``sha256`` entries look
    volatile but are not: they move only when an artifact's bytes move, and
    that is a content change the vintage should reflect.
    """
    if not isinstance(doc, Mapping):
        return {}
    return {k: v for k, v in doc.items() if k != DATE_MODIFIED_KEY}


def content_changed(new_doc: Any, previous_doc: Any) -> bool:
    """Whether ``new_doc`` says anything different from ``previous_doc``.

    Key order is not content (both operands are compared as mappings), so a
    change to the canonical key order in ``researcher_profiles.schema.jsonld`` does
    not fraudulently advance every profile's vintage.
    """
    return _published_content(new_doc) != _published_content(previous_doc)


def stamp_date_modified(
    new_doc: Mapping[str, Any],
    previous_doc: Mapping[str, Any] | None,
    *,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    """Return ``new_doc`` carrying the honest ``dateModified``.

    Three outcomes, and only the first one writes a new value:

    ``content changed``
        Stamp ``now`` (default: this instant, UTC).
    ``content unchanged, a previous stamp exists``
        Carry the previous stamp forward untouched.
    ``content unchanged, no previous stamp``
        Leave the field absent. We do not know when this content was written
        and the spec forbids guessing.

    Any ``dateModified`` already present in ``new_doc`` is treated as carried
    over from the document that was loaded, never as an authoritative value:
    a caller typically loads ``profile.jsonld``, mutates a few keys and saves
    it back, so honouring the incoming value would freeze the stamp at whatever
    the first write produced.

    ``now`` accepts a datetime (injected by tests, so no test has to sleep) or
    a pre-formatted string.
    """
    out = dict(new_doc)
    if content_changed(new_doc, previous_doc):
        out[DATE_MODIFIED_KEY] = now if isinstance(now, str) else utc_now_iso(now)
        return out

    previous = _published_stamp(previous_doc)
    if previous is None:
        out.pop(DATE_MODIFIED_KEY, None)
    else:
        out[DATE_MODIFIED_KEY] = previous
    return out


def _published_stamp(doc: Any) -> str | None:
    if not isinstance(doc, Mapping):
        return None
    value = doc.get(DATE_MODIFIED_KEY)
    return value if isinstance(value, str) and value.strip() else None


def read_published_document(profile_dir: str | Path) -> dict[str, Any]:
    """The ``profile.jsonld`` currently on disk; ``{}`` when absent or broken.

    An unreadable predecessor is treated as no predecessor, which makes the
    next write a content change and stamps it. That is the right way round: a
    document we cannot compare against is a document we cannot claim is
    unchanged.
    """
    path = Path(profile_dir) / PROFILE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
