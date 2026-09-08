"""``dateModified``: the content-change stamp (databio/researcher-profiles#1).

The property under test is not "a date gets written". It is "the date advances
when, and only when, the profile's content advances". A stamp that moved on
every build would satisfy a happy-path test and still be the lie the issue
reports, so the anti-trap case below is the one that matters.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def _write(profile_dir: Path, doc: dict) -> None:
    import json

    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "profile.jsonld").write_text(json.dumps(doc), encoding="utf-8")


class TestStampPolicy:
    """The pure decision: given old and new content, what date is honest?"""

    def test_first_write_is_stamped(self) -> None:
        out = stamp_date_modified(DOC, None, now=T1)
        assert out[DATE_MODIFIED_KEY] == "2026-03-04T09:30:00+00:00"

    def test_unchanged_content_keeps_the_original_stamp(self) -> None:
        """THE anti-trap case: a rebuild that changes nothing changes nothing."""
        first = stamp_date_modified(DOC, None, now=T1)
        second = stamp_date_modified(dict(first), first, now=T2)
        assert second[DATE_MODIFIED_KEY] == first[DATE_MODIFIED_KEY]

    def test_repeated_no_op_writes_never_drift(self) -> None:
        doc = stamp_date_modified(DOC, None, now=T1)
        previous = doc
        for i in range(10):
            doc = stamp_date_modified(dict(previous), previous, now=T2 + timedelta(days=i))
            previous = doc
        assert doc[DATE_MODIFIED_KEY] == "2026-03-04T09:30:00+00:00"

    def test_changed_content_advances_the_stamp(self) -> None:
        first = stamp_date_modified(DOC, None, now=T1)
        changed = dict(first) | {"field": "Computational mathematics"}
        second = stamp_date_modified(changed, first, now=T2)
        assert second[DATE_MODIFIED_KEY] == "2026-06-02T09:30:00+00:00"

    def test_removing_a_member_is_also_a_change(self) -> None:
        first = stamp_date_modified(DOC | {"field": "x"}, None, now=T1)
        shrunk = {k: v for k, v in first.items() if k != "field"}
        second = stamp_date_modified(shrunk, first, now=T2)
        assert second[DATE_MODIFIED_KEY] == "2026-06-02T09:30:00+00:00"

    def test_unchanged_content_with_no_prior_stamp_stays_absent(self) -> None:
        """No stamp is invented for content we cannot date."""
        out = stamp_date_modified(DOC, DOC, now=T2)
        assert DATE_MODIFIED_KEY not in out

    def test_an_incoming_stamp_is_carried_over_not_authoritative(self) -> None:
        previous = stamp_date_modified(DOC, None, now=T1)
        edited = dict(previous) | {"field": "Analytical engines"}
        out = stamp_date_modified(edited, previous, now=T2)
        assert out[DATE_MODIFIED_KEY] == "2026-06-02T09:30:00+00:00"

    def test_key_order_is_not_content(self) -> None:
        previous = stamp_date_modified(DOC, None, now=T1)
        reordered = dict(reversed(list(previous.items())))
        out = stamp_date_modified(reordered, previous, now=T2)
        assert out[DATE_MODIFIED_KEY] == previous[DATE_MODIFIED_KEY]

    def test_content_changed_ignores_the_stamp_itself(self) -> None:
        a = DOC | {DATE_MODIFIED_KEY: "2020-01-01T00:00:00+00:00"}
        b = DOC | {DATE_MODIFIED_KEY: "2026-01-01T00:00:00+00:00"}
        assert not content_changed(a, b)
        assert content_changed(a, b | {"level": "lite"})

    def test_blank_prior_stamp_is_treated_as_absent(self) -> None:
        out = stamp_date_modified(DOC, DOC | {DATE_MODIFIED_KEY: "  "}, now=T2)
        assert DATE_MODIFIED_KEY not in out


class TestAgainstDisk:
    def test_absent_profile_reads_as_no_predecessor(self, tmp_path: Path) -> None:
        assert read_published_document(tmp_path) == {}

    def test_unparseable_predecessor_is_treated_as_none(self, tmp_path: Path) -> None:
        (tmp_path / "profile.jsonld").write_text("{not json", encoding="utf-8")
        assert read_published_document(tmp_path) == {}

    def test_json_that_is_not_an_object_is_treated_as_none(self, tmp_path: Path) -> None:
        (tmp_path / "profile.jsonld").write_text("[1, 2]", encoding="utf-8")
        assert read_published_document(tmp_path) == {}


class TestSchemaCompatibility:
    """The stamped document must still be a valid profile."""

    def test_stamped_document_validates_and_round_trips(self) -> None:
        from researcher_profiles.schema import ProfileDocument

        stamped = stamp_date_modified(DOC, None, now=T1)
        model = ProfileDocument.model_validate(stamped)
        assert model.date_modified == "2026-03-04T09:30:00+00:00"
        assert model.model_dump(mode="json")[DATE_MODIFIED_KEY] == model.date_modified

    def test_unstamped_document_serializes_with_the_field_absent(self) -> None:
        from researcher_profiles.schema import ProfileDocument

        model = ProfileDocument.model_validate(DOC)
        assert model.date_modified is None
        assert DATE_MODIFIED_KEY not in model.model_dump(mode="json")

    def test_stamp_lands_in_its_canonical_key_slot(self) -> None:
        import json

        from researcher_profiles.schema.jsonld import canonical_dumps

        stamped = stamp_date_modified(DOC, None, now=T1)
        keys = list(json.loads(canonical_dumps(stamped)))
        assert keys.index(DATE_MODIFIED_KEY) < keys.index("level")
