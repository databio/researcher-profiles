"""Tests for scholarcore.identity."""

import pytest

from scholarcore.identity import (
    is_local,
    is_rid,
    mint_local_rid,
    normalize_arxiv_id,
    normalize_doi,
    normalize_pmcid,
    normalize_pmid,
    orcid_of,
    validate_rid,
)

VALID_ORCID = "0000-0002-1825-0097"
VALID_LOCAL_RID = "local:carberry-josiah-a1b2c3"


@pytest.mark.parametrize(
    "rid",
    [
        VALID_ORCID,
        "0000-0004-1003-0000",  # check digit 0
        "0000-0004-4600-113X",  # check digit 10, written as X
        VALID_LOCAL_RID,
        "local:a-000000",  # the shortest legal local rid
        f"  {VALID_ORCID}  ",  # surrounding whitespace is stripped
        f"  {VALID_LOCAL_RID}  ",
    ],
)
def test_validate_rid_accepts_both_forms_and_strips_whitespace(rid):
    assert validate_rid(rid) == rid.strip()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-an-orcid",
        "0000-0002-1825-009",  # too short
        "0000-0002-1825-00979",  # too long
        "000-0002-1825-0097",  # wrong grouping
        "0000-0002-1694-233x",  # the check digit for 10 is uppercase X
        "local:",
        "local:no-hex-suffix",
        "local:abcdef",  # a suffix with no slug
        "local:person-ABCDEF",  # hex suffix must be lowercase
        "local:person-abcde",  # 5 hex chars, not 6
        "local:person-abcdefg",  # 7 hex chars, not 6
        "local:person-ghijkl",  # right length, not hex
        "local:-person-abcdef",  # slug may not start with a hyphen
        "local:person_underscore-abcdef",  # underscores are not slug characters
        "LOCAL:uppercase-not-allowed-a1b2c3",
    ],
)
def test_validate_rid_rejects_malformed(bad):
    with pytest.raises(ValueError):
        validate_rid(bad)


@pytest.mark.parametrize("orcid", ["0000-0002-1825-0098", "0000-0003-1006-0000"])
def test_validate_rid_rejects_bad_checksum(orcid):
    """One mistyped digit still matches the regex, so only the checksum catches it."""
    with pytest.raises(ValueError, match="checksum"):
        validate_rid(orcid)


@pytest.mark.parametrize("bad", [None, 12345, ["0000-0002-1825-0097"]])
def test_validate_rid_rejects_non_strings(bad):
    with pytest.raises(ValueError, match="must be a string"):
        validate_rid(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fn,arg,expected",
    [
        (is_rid, VALID_ORCID, True),
        (is_rid, VALID_LOCAL_RID, True),
        (is_rid, "garbage", False),
        (is_rid, "", False),
        (is_rid, 12345, False),
        (is_local, VALID_LOCAL_RID, True),
        (is_local, VALID_ORCID, False),
        (orcid_of, VALID_ORCID, VALID_ORCID),
        (orcid_of, VALID_LOCAL_RID, None),
        (orcid_of, None, None),
    ],
    ids=lambda v: v.__name__ if callable(v) else repr(v),
)
def test_rid_predicates(fn, arg, expected):
    assert fn(arg) == expected


@pytest.mark.parametrize(
    "name,expected_prefix",
    [
        ("Josiah Carberry", "local:josiah-carberry-"),
        ("José Ángel Gutiérrez-Núñez", "local:jose-angel-gutierrez-nunez-"),
        ("  Van  der   Berg, Jan  ", "local:van-der-berg-jan-"),
        ("", "local:person-"),  # nothing to slugify
        ("!!!", "local:person-"),  # no ASCII alphanumerics
        ("李 雷", "local:person-"),  # no ASCII at all
    ],
)
def test_mint_local_rid_slugifies_the_name_and_stays_valid(name, expected_prefix):
    minted = mint_local_rid(name)
    assert minted.startswith(expected_prefix)
    assert validate_rid(minted) == minted


def test_mint_local_rid_is_unique_across_calls():
    """The random suffix is what keeps two same-named people distinguishable."""
    assert len({mint_local_rid("Josiah Carberry") for _ in range(10)}) == 10


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.1234/example.doi", "10.1234/example.doi"),
        ("https://doi.org/10.1234/EXAMPLE.DOI", "10.1234/EXAMPLE.DOI"),  # case preserved
        ("http://dx.doi.org/10.1234/example.doi", "10.1234/example.doi"),
        ("DOI:10.1234/example.doi", "10.1234/example.doi"),  # prefix match is case-insensitive
        ("  10.1234/example.doi  ", "10.1234/example.doi"),
        ("10.1234/doi.org/x", "10.1234/doi.org/x"),  # only a LEADING prefix is stripped
        ("https://doi.org/", None),  # a prefix with nothing after it
        ("doi.org/10.1/X", "10.1/X"),  # a bare resolver prefix, no scheme
        ("https://doi.org/doi:10.1/x", "10.1/x"),  # a label after the resolver
        ("doi:/10.1/x", "10.1/x"),  # a leading slash after the label
        ("   ", None),
        (None, None),
        ("", None),
    ],
)
def test_normalize_doi(raw, expected):
    assert normalize_doi(raw) == expected
    assert normalize_doi(expected) == expected, "normalization must be idempotent"


def test_normalize_doi_lowercase_option():
    """The lowercase option is for deduplication where case should be ignored."""
    assert normalize_doi("10.1234/EXAMPLE.DOI", lowercase=True) == "10.1234/example.doi"
    assert normalize_doi("https://doi.org/10.1234/MixedCase", lowercase=True) == "10.1234/mixedcase"
    assert (
        normalize_doi("https://doi.org/10.1038/Nature12373", lowercase=True)
        == "10.1038/nature12373"
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12345678", "12345678"),
        (12345678, "12345678"),
        ("pmid:12345678", "12345678"),
        ("https://www.ncbi.nlm.nih.gov/pubmed/12345678", "12345678"),
        ("https://ncbi.nlm.nih.gov/pubmed/12345678", "12345678"),  # www is optional
        ("  12345678  ", "12345678"),
        (0, "0"),  # falsy, but a real value
        ("PMC1234567", None),  # a PMCID is not a PMID
        ("12345678.0", None),  # not all digits
        ("0034416909", "0034416909"),  # leading zeros kept: padding is a defect, not an alias
        ("PMID: 12,345", None),  # stray characters fail closed, never scrubbed
        ("not-a-number", None),
        (None, None),
        ("", None),
    ],
)
def test_normalize_pmid(raw, expected):
    assert normalize_pmid(raw) == expected
    assert normalize_pmid(expected) == expected, "normalization must be idempotent"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("PMCID:PMC9", "PMC9"),
        ("PMCID: PMC123", "PMC123"),
        ("PMCPMC9999", "PMC9999"),  # repeated prefix collapsed
        ("pmc0001234", "PMC0001234"),  # leading zeros kept
        ("123456", "PMC123456"),
        ("PMC123x", None),  # stray characters fail closed
        ("PMC", None),
        (None, None),
        ("", None),
    ],
)
def test_normalize_pmcid(raw, expected):
    assert normalize_pmcid(raw) == expected
    assert normalize_pmcid(expected) == expected, "normalization must be idempotent"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("arXiv:2401.12345v2", "2401.12345"),
        ("  2401.12345V1 ", "2401.12345"),
        ("hep-th/9901001v2", "hep-th/9901001"),
        ("2401.00001/", "2401.00001"),
        ("2401.00001", "2401.00001"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_arxiv_id(raw, expected):
    assert normalize_arxiv_id(raw) == expected
    assert normalize_arxiv_id(expected) == expected, "normalization must be idempotent"
