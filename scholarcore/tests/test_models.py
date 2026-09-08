"""Round-trip, extra-field, and unknown-enum tolerance tests for the core
entity models.
"""

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


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize(
    "model,fixture",
    [
        (Person, "person"),
        (Paper, "paper"),
        (Award, "award"),
        (Opportunity, "opportunity"),
    ],
)
def test_round_trip_equality(model, fixture):
    data = _load(fixture)
    instance = model.model_validate(data)
    dumped = instance.model_dump(mode="json")
    round_tripped = model.model_validate(dumped)
    assert round_tripped == instance


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


def test_unknown_role_loads_as_none_rather_than_raising():
    data = _load("award")
    data["role"] = "some_future_role_not_yet_in_the_vocabulary"
    award = Award.model_validate(data)
    assert award.role is None


def test_unknown_status_loads_as_none_rather_than_raising():
    data = _load("award")
    data["status"] = "some_future_status_not_yet_in_the_vocabulary"
    award = Award.model_validate(data)
    assert award.status is None


def test_organization_minimal():
    org = Organization.model_validate({"name": "Brown University"})
    assert org.name == "Brown University"
    assert org.ror_id is None


def test_authorship_round_trip():
    data = _load("paper")
    authorship = Authorship.model_validate(data["authors"][0])
    dumped = authorship.model_dump(mode="json")
    round_tripped = Authorship.model_validate(dumped)
    assert round_tripped == authorship
    assert authorship.person.rid == "0000-0002-1825-0097"
    assert authorship.corresponding is True
    assert CreditRole.conceptualization in authorship.credit_roles


def test_paper_authors_are_authorship_instances():
    paper = Paper.model_validate(_load("paper"))
    assert all(isinstance(a, Authorship) for a in paper.authors)
    assert paper.authors[0].affiliations[0].organization.name == "Brown University"


def test_unknown_submission_type_loads_as_none_rather_than_raising():
    data = _load("award")
    data["submission_type"] = "some_future_type_not_yet_in_the_vocabulary"
    award = Award.model_validate(data)
    assert award.submission_type is None


def test_award_dates_are_real_dates():
    award = Award.model_validate(_load("award"))
    assert award.start == datetime.date(2026, 1, 1)
    assert award.end == datetime.date(2029, 12, 31)
    assert award.model_dump(mode="json")["start"] == "2026-01-01"


def test_opportunity_dates_are_real_dates():
    data = _load("opportunity")
    opportunity = Opportunity.model_validate(data)
    assert isinstance(opportunity.posted_date, datetime.date)
    assert opportunity.model_dump(mode="json")["posted_date"] == data["posted_date"]


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


def test_affiliation_round_trip():
    data = _load("person")
    affiliation = Affiliation.model_validate(data["affiliations"][0])
    dumped = affiliation.model_dump(mode="json")
    round_tripped = Affiliation.model_validate(dumped)
    assert round_tripped == affiliation


def test_affiliation_dates_are_real_dates():
    data = _load("person")
    affiliation = Affiliation.model_validate(data["affiliations"][1])
    assert affiliation.start == datetime.date(1920, 9, 1)
    assert affiliation.end == datetime.date(1928, 6, 30)
    assert affiliation.model_dump(mode="json")["start"] == "1920-09-01"


def test_person_affiliations_are_affiliation_instances():
    person = Person.model_validate(_load("person"))
    assert all(isinstance(a, Affiliation) for a in person.affiliations)
    assert person.affiliations[0].department == "Psychoceramics"


def test_person_ref_accepts_a_freshly_minted_local_rid():
    """The mint and the validator must agree, or people with no ORCID are unusable."""
    minted = mint_local_rid("Josiah Carberry")
    assert PersonRef.model_validate({"rid": minted}).rid == minted


# --- Paper identity: doi/pmid are normalized on load ----------------------
#
# The normalizers themselves are covered by tests/test_identity.py. What is
# tested here is only that Paper actually wires them onto its two id fields,
# and what that buys a consumer.


def test_paper_normalizes_doi_and_pmid_on_load():
    paper = Paper.model_validate(
        {"title": "T", "doi": "https://doi.org/10.1234/AB", "pmid": "pmid:99"}
    )
    # DOI case is preserved (some systems like OpenAlex preserve original case);
    # use .lower() for case-insensitive comparison when joining across systems.
    assert paper.doi == "10.1234/AB"
    assert paper.pmid == "99"


def test_paper_pmid_carrying_no_digits_loads_as_none_rather_than_raising():
    assert Paper.model_validate({"title": "T", "pmid": "not-a-number"}).pmid is None


def test_papers_pasted_in_different_forms_share_one_join_key():
    """The whole point: two systems paste a DOI differently and still match.

    Case is preserved in storage (some systems like OpenAlex preserve original
    case), but for join purposes compare case-insensitively: DOIs are case-
    insensitive per the DOI spec.
    """
    a = Paper.model_validate({"title": "T", "doi": "https://doi.org/10.1234/AB"})
    b = Paper.model_validate({"title": "T", "doi": "  doi:10.1234/Ab  "})
    # Prefixes stripped, case preserved
    assert a.doi == "10.1234/AB"
    assert b.doi == "10.1234/Ab"
    # For join keys, compare case-insensitively
    assert a.doi.lower() == b.doi.lower() == "10.1234/ab"


def test_paper_normalization_survives_a_round_trip():
    paper = Paper.model_validate(
        {"title": "T", "doi": "https://doi.org/10.1234/AB", "pmid": "pmid:99"}
    )
    assert Paper.model_validate(paper.model_dump(mode="json")) == paper
