"""Round-trip, extra-field, and unknown-enum tolerance tests for the core entity models."""

import datetime
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from scholarcore.affil import Affiliation
from scholarcore.biblio import Authorship, CreditRole, Paper
from scholarcore.funding import Award, AwardStatus, GrantRole, Opportunity, SubmissionType
from scholarcore.identity import mint_local_rid
from scholarcore.org import Organization
from scholarcore.person import Person, PersonRef

FIXTURES = Path(__file__).parent / "fixtures"


def _identity(d):
    return d


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize(
    "model,fixture,pick",
    [
        (Person, "person", _identity),
        (Paper, "paper", _identity),
        (Award, "award", _identity),
        (Opportunity, "opportunity", _identity),
        (Authorship, "paper", lambda d: d["authors"][0]),
        (Affiliation, "person", lambda d: d["affiliations"][0]),
    ],
)
def test_round_trip_equality(model, fixture, pick):
    instance = model.model_validate(pick(_load(fixture)))
    dumped = instance.model_dump(mode="json")
    assert model.model_validate(dumped) == instance


def test_award_enum_fields_are_typed():
    award = Award.model_validate(_load("award"))
    assert award.role == GrantRole.pi
    assert award.status == AwardStatus.active
    assert award.submission_type == SubmissionType.new


def test_person_ref_rejects_invalid_rid():
    with pytest.raises(ValidationError):
        PersonRef.model_validate({"rid": "not-a-valid-rid"})


def test_extra_fields_are_tolerated_and_preserved():
    data = _load("person")
    data["favorite_pot"] = "cracked"
    person = Person.model_validate(data)
    assert person.model_dump()["favorite_pot"] == "cracked"


@pytest.mark.parametrize("field", ["role", "status", "submission_type"])
def test_unknown_award_enum_loads_as_none(field):
    data = _load("award")
    data[field] = "some_future_value"
    assert getattr(Award.model_validate(data), field) is None


def test_organization_minimal():
    org = Organization.model_validate({"name": "Brown University"})
    assert org.name == "Brown University"
    assert org.ror_id is None


def test_authorship_fields_load():
    authorship = Authorship.model_validate(_load("paper")["authors"][0])
    assert authorship.person.rid == "0000-0002-1825-0097"
    assert authorship.corresponding is True
    assert CreditRole.conceptualization in authorship.credit_roles


@pytest.mark.parametrize(
    "model,fixture,pick,field,expected",
    [
        (Award, "award", _identity, "start", datetime.date(2026, 1, 1)),
        (Award, "award", _identity, "end", datetime.date(2029, 12, 31)),
        (Opportunity, "opportunity", _identity, "posted_date", datetime.date(2025, 1, 15)),
        (Affiliation, "person", lambda d: d["affiliations"][1], "start", datetime.date(1920, 9, 1)),
        (Affiliation, "person", lambda d: d["affiliations"][1], "end", datetime.date(1928, 6, 30)),
    ],
)
def test_dates_are_real_dates(model, fixture, pick, field, expected):
    obj = model.model_validate(pick(_load(fixture)))
    assert getattr(obj, field) == expected
    assert obj.model_dump(mode="json")[field] == expected.isoformat()


@pytest.mark.parametrize(
    "model,fixture,get,expected",
    [
        (
            Paper,
            "paper",
            lambda m: m.authors[0].affiliations[0].organization.name,
            "Brown University",
        ),
        (Person, "person", lambda m: m.affiliations[0].department, "Psychoceramics"),
    ],
)
def test_nested_records_load(model, fixture, get, expected):
    assert get(model.model_validate(_load(fixture))) == expected


def test_award_budget_fields_reject_non_int_drift():
    data = _load("award")
    data["directs"] = "not-a-number"
    with pytest.raises(ValidationError):
        Award.model_validate(data)


def test_affiliation_coerces_bare_string():
    affiliation = Affiliation.model_validate("Brown University")
    assert affiliation.organization.name == "Brown University"
    assert affiliation.department is None


def test_affiliation_current_is_derived_from_end_date():
    current = Affiliation.model_validate({"organization": {"name": "Brown University"}})
    assert current.current is True
    past = Affiliation.model_validate(
        {"organization": {"name": "Brown University"}, "end": "2020-01-01"}
    )
    assert past.current is False


def test_person_ref_accepts_a_freshly_minted_local_rid():
    """The mint and the validator must agree, or people with no ORCID are unusable."""
    minted = mint_local_rid("Josiah Carberry")
    assert PersonRef.model_validate({"rid": minted}).rid == minted


def test_paper_normalizes_identifiers_on_load():
    paper = Paper.model_validate(
        {
            "title": "T",
            "doi": "https://doi.org/10.1234/AB",
            "pmid": "pmid:99",
            "pmcid": "pmc9",
            "arxiv_id": "arXiv:2401.1v2",
        }
    )
    # Case is preserved on load; join keys compare with .lower().
    assert paper.doi == "10.1234/AB"
    assert paper.pmid == "99"
    assert paper.pmcid == "PMC9"
    assert paper.arxiv_id == "2401.1"
    assert Paper.model_validate(paper.model_dump(mode="json")) == paper


def test_paper_pmid_carrying_no_digits_loads_as_none_rather_than_raising():
    assert Paper.model_validate({"title": "T", "pmid": "not-a-number"}).pmid is None
    assert Paper.model_validate({"title": "T", "pmcid": "PMC12x"}).pmcid is None


def test_papers_pasted_in_different_forms_share_one_join_key():
    """Two systems paste a DOI differently and still match (DOIs are case-insensitive)."""
    a = Paper.model_validate({"title": "T", "doi": "https://doi.org/10.1234/AB"})
    b = Paper.model_validate({"title": "T", "doi": "  doi:10.1234/Ab  "})
    assert a.doi == "10.1234/AB"
    assert b.doi == "10.1234/Ab"
    assert a.doi.lower() == b.doi.lower() == "10.1234/ab"
