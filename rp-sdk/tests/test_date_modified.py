"""``dateModified``: the stamp advances when, and only when, the profile's content advances.

A stamp that moved on every build would pass a happy-path test and still be the lie
databio/researcher-profiles#1 reports, so the unchanged-content cases are the ones that matter.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from researcher_profiles.schema import ProfileDocument
from researcher_profiles.schema.jsonld import canonical_dumps
from researcher_profiles.utils.date_modified import (
    DATE_MODIFIED_KEY,
    content_changed,
    read_published_document,
    stamp_date_modified,
)

T1 = datetime(2026, 3, 4, 9, 30, 0, tzinfo=timezone.utc)
T2 = T1 + timedelta(days=90)

DOC = {
    "@context": "https://profiles.databio.org/context/v1.jsonld",
    "@type": "Person",
    "name": "Ada Lovelace",
    "rid": "0000-0002-1825-0097",
    "provenance": "third_party",
    "level": "full",
}


T2_STAMP = "2026-06-02T09:30:00+00:00"


def _first_write():
    return DOC, None, T1, "2026-03-04T09:30:00+00:00"


def _changed():
    prior = stamp_date_modified(DOC, None, now=T1)
    return dict(prior) | {"field": "Computational mathematics"}, prior, T2, T2_STAMP


def _member_removed():
    prior = stamp_date_modified(DOC | {"field": "x"}, None, now=T1)
    return {k: v for k, v in prior.items() if k != "field"}, prior, T2, T2_STAMP


class TestStampPolicy:
    @pytest.mark.parametrize(
        "build", [_first_write, _changed, _member_removed], ids=lambda f: f.__name__.strip("_")
    )
    def test_stamp_is_set_to_now(self, build) -> None:
        new, prior, now, expected = build()
        assert stamp_date_modified(new, prior, now=now)[DATE_MODIFIED_KEY] == expected

    @pytest.mark.parametrize(
        "rewrite",
        [dict, lambda d: dict(reversed(list(d.items())))],
        ids=["unchanged", "reordered"],
    )
    def test_unchanged_content_keeps_the_stamp(self, rewrite) -> None:
        """THE anti-trap case: a rebuild that changes nothing changes nothing."""
        first = stamp_date_modified(DOC, None, now=T1)
        second = stamp_date_modified(rewrite(first), first, now=T2)
        assert second[DATE_MODIFIED_KEY] == first[DATE_MODIFIED_KEY]

    def test_repeated_no_op_writes_never_drift(self) -> None:
        doc = stamp_date_modified(DOC, None, now=T1)
        previous = doc
        for i in range(10):
            doc = stamp_date_modified(dict(previous), previous, now=T2 + timedelta(days=i))
            previous = doc
        assert doc[DATE_MODIFIED_KEY] == "2026-03-04T09:30:00+00:00"

    @pytest.mark.parametrize("prior", [DOC, DOC | {DATE_MODIFIED_KEY: "  "}], ids=["none", "blank"])
    def test_no_stamp_is_invented_for_undatable_content(self, prior) -> None:
        assert DATE_MODIFIED_KEY not in stamp_date_modified(DOC, prior, now=T2)

    def test_content_changed_ignores_the_stamp_itself(self) -> None:
        a = DOC | {DATE_MODIFIED_KEY: "2020-01-01T00:00:00+00:00"}
        b = DOC | {DATE_MODIFIED_KEY: "2026-01-01T00:00:00+00:00"}
        assert not content_changed(a, b)
        assert content_changed(a, b | {"level": "lite"})


class TestAgainstDisk:
    @pytest.mark.parametrize(
        "content", [None, "{not json", "[1, 2]"], ids=["absent", "bad-json", "array"]
    )
    def test_unusable_predecessor_reads_as_none(self, tmp_path: Path, content) -> None:
        if content is not None:
            (tmp_path / "profile.jsonld").write_text(content, encoding="utf-8")
        assert read_published_document(tmp_path) == {}


class TestSchemaCompatibility:
    """The stamped document must still be a valid profile."""

    def test_stamped_document_validates_and_round_trips(self) -> None:
        stamped = stamp_date_modified(DOC, None, now=T1)
        model = ProfileDocument.model_validate(stamped)
        assert model.date_modified == "2026-03-04T09:30:00+00:00"
        assert model.model_dump(mode="json")[DATE_MODIFIED_KEY] == model.date_modified

    def test_unstamped_document_serializes_with_the_field_absent(self) -> None:
        model = ProfileDocument.model_validate(DOC)
        assert model.date_modified is None
        assert DATE_MODIFIED_KEY not in model.model_dump(mode="json")

    def test_stamp_lands_in_its_canonical_key_slot(self) -> None:
        stamped = stamp_date_modified(DOC, None, now=T1)
        keys = list(json.loads(canonical_dumps(stamped)))
        assert keys.index(DATE_MODIFIED_KEY) < keys.index("level")
