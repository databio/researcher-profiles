"""The rid resolver: cautious name matching, deterministic mint-on-miss.

Everything runs on both store backends (``any_store``): the resolver is the join point for consumer
services, and a behavior on only one backend would split identities when a deployment switches stores.
"""

import pytest

import researcher_profiles.resolve as resolve_mod
from researcher_profiles.errors import ProfileLoadError, ProfileWriteError, WriteHookError
from researcher_profiles.resolve import (
    ResolveError,
    ResolveResult,
    _create_stub,
    _deterministic_local_rid,
    resolve_person,
)
from researcher_profiles.schema import ProfileDocument, is_local, is_rid
from researcher_profiles.store import FilesystemProfileStore

# ORCID's documented sample ids, all checksum-valid.
ORCID_A = "0000-0002-1825-0097"
ORCID_B = "0000-0004-4600-113X"
ORCID_C = "0000-0004-1003-0000"

# Seeds are (rid, name, affiliation, slug).
JANE_A = (ORCID_A, "Jane Doe", None, "doe-jane")
JANE_A_MID = (ORCID_A, "Jane A. Doe", None, "doe-jane")
JANE_A_UVA = (ORCID_A, "Jane Doe", "University of Virginia", "doe-jane")
WEI_A_UVA = (ORCID_A, "Wei Zhang", "University of Virginia", "zhang-wei")
JOHN_C = (ORCID_C, "John Doe", None, "doe-john")
NG_C = (ORCID_C, "Wai-Kin Ng", None, "ng-wai-kin")
HOPPER_LOCAL = (_deterministic_local_rid("Grace Hopper"), "Grace Hopper", None, "hopper-grace")
LONG_NAME = "Maximiliana Alexandrina Bernadetta Konstantyna " * 4 + "Wolszczanowska"


def _document(rid: str, name: str, affiliation: str | None = None) -> ProfileDocument:
    provenance = "synthetic" if rid.startswith("local:") else "third_party"
    return ProfileDocument(name=name, rid=rid, provenance=provenance, affiliation=affiliation)


def _seed(store, *docs):
    for rid, name, aff, slug in docs:
        store.create(_document(rid, name, aff), slug=slug)


# --- rid first ---


@pytest.mark.parametrize(
    "seed, kwargs",
    [
        (JANE_A_MID, {"rid": ORCID_A}),
        (JANE_A, {"rid": ORCID_A, "name": "Unknown"}),  # rid-first beats a placeholder name
        (HOPPER_LOCAL, {"rid": HOPPER_LOCAL[0]}),  # the disambiguation round-trip
    ],
    ids=["orcid", "orcid-with-junk-name", "local"],
)
def test_a_known_rid_resolves_exactly(any_store, seed, kwargs):
    _seed(any_store, seed)
    assert resolve_person(any_store, **kwargs) == ResolveResult(
        rid=seed[0], created=False, confidence="exact"
    )


# An unknown local rid is always refused: the deterministic rid is computable from a public name,
# so accepting it would let anyone plant a rival stub for a real person.
@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"rid": ORCID_B}, "name"),
        ({"rid": "0000-0002-1825-0090", "name": "X Y"}, None),
        ({"rid": "local:mallory-abc123", "name": "Mallory"}, "local"),
        ({}, None),
        ({"rid": "local:mallory-abc123", "name": "Jane Doe"}, "minted"),
        ({"rid": _deterministic_local_rid("Jane Doe"), "name": "Jane Doe"}, "minted"),
        ({"rid": ORCID_A, "name": "Ada Lovelace", "create_new": True}, "mutually exclusive"),
    ],
    ids=[
        "orcid-no-name",
        "bad-checksum",
        "unknown-local",
        "nothing",
        "plant-local",
        "plant-deterministic",
        "rid-and-create-new",
    ],
)
def test_refused_and_nothing_minted(any_store, kwargs, match):
    with pytest.raises(ResolveError, match=match):
        resolve_person(any_store, **kwargs)
    assert any_store.list_slugs() == []


@pytest.mark.parametrize(
    "junk",
    ["Unknown", "et al", "Dr", "Corresponding Author", "Unknown Unknown", "Anonymous Author"],
)
def test_placeholder_names_are_refused_not_minted(any_store, junk):
    """Token-aware: multi-token placeholders such as "Unknown Unknown" are refused too."""
    with pytest.raises(ResolveError, match="placeholder"):
        resolve_person(any_store, name=junk)
    with pytest.raises(ResolveError, match="placeholder"):
        resolve_person(any_store, name=junk, create_new=True)
    assert any_store.list_slugs() == []


def test_an_orcid_miss_mints_once_under_that_orcid(any_store):
    first = resolve_person(any_store, rid=ORCID_B, name="Ada Lovelace")
    assert first.rid == ORCID_B  # never a local: id when a valid ORCID is supplied
    assert (first.created, first.confidence) == (True, "exact")
    doc = any_store.get(ORCID_B).metadata
    assert (doc.provenance, doc.level, doc.visibility) == ("third_party", "lite", "limited")

    second = resolve_person(any_store, rid=ORCID_B)
    assert (second.rid, second.created) == (ORCID_B, False)


# --- name matching ---


@pytest.mark.parametrize(
    "seed, name, affiliation",
    [
        (JANE_A_MID, "Doe, Jane", None),
        (JANE_A_MID, "Jane Doe", None),
        (JANE_A_MID, "jane DOE", None),
        (JANE_A_MID, "Jane B. Doe", None),
        (WEI_A_UVA, "Zhang, Wei", None),  # silence on affiliation is not a veto
        (JANE_A_UVA, "Doe, J.", "university of virginia"),  # initials need corroboration
    ],
    ids=["comma-order", "plain", "case", "other-middle", "no-affiliation", "initial-corroborated"],
)
def test_name_binds(any_store, seed, name, affiliation):
    _seed(any_store, seed)
    r = resolve_person(any_store, name=name, affiliation=affiliation)
    assert r == ResolveResult(rid=ORCID_A, created=False, confidence="high")


@pytest.mark.parametrize(
    "seeds, name, affiliation, expected",
    [
        (
            [JANE_A],
            "J.A. Doe",
            None,
            {ORCID_A},
        ),  # compact initials are initials, not another person
        ([NG_C], "Ng, W.-K.", None, {ORCID_C}),
        ([JANE_A], "J. Doe", None, {ORCID_A}),  # no affiliation to corroborate an initial
        ([WEI_A_UVA], "Wei Zhang", "MIT", {ORCID_A}),  # a conflicting affiliation vetoes a merge
        ([JANE_A, JOHN_C], "J. Doe", None, {ORCID_A, ORCID_C}),
    ],
    ids=[
        "compact-initials",
        "compact-hyphen-initials",
        "initial-only",
        "affiliation-conflict",
        "two-compatible",
    ],
)
def test_name_defers(any_store, seeds, name, affiliation, expected):
    _seed(any_store, *seeds)
    before = set(any_store.list_slugs())
    r = resolve_person(any_store, name=name, affiliation=affiliation)
    assert (r.rid, r.created, r.confidence) == (None, False, "low")
    assert {c.rid for c in r.candidates} == expected
    assert set(any_store.list_slugs()) == before  # nothing minted


@pytest.mark.parametrize(
    "seeds, name, affiliation",
    [
        ([JANE_A], "John Doe", None),  # John must not merge into Jane
        ([], "Grace Hopper", "Yale"),
        ([], "Doe, J.", None),  # short names: slug derivation falls back to the rid body
        ([], "Madonna", None),
        ([], "J.-P. Sartre", None),
        ([], "J.A. Doe", None),
        ([], "Unknown Smith", None),  # a junk token beside a real name is fine
        ([], LONG_NAME, None),  # readable part capped, hash keeps it unique, both backends agree
    ],
    ids=[
        "other-given-name",
        "true-miss",
        "initial",
        "mononym",
        "hyphen-initial",
        "compact",
        "junk-token",
        "long",
    ],
)
def test_a_new_person_is_minted(any_store, seeds, name, affiliation):
    _seed(any_store, *seeds)
    r = resolve_person(any_store, name=name, affiliation=affiliation)
    assert (r.created, r.confidence) == (True, "high")
    assert is_local(r.rid) and is_rid(r.rid) and len(r.rid) < 80
    assert r.rid == _deterministic_local_rid(name)
    doc = any_store.get(r.rid).metadata
    assert (doc.provenance, doc.affiliation) == ("third_party", affiliation)


@pytest.mark.parametrize(
    "first, second", [("Grace Hopper", "Hopper, Grace"), ("Doe, J.", "J. Doe")]
)
def test_resolving_twice_mints_once(any_store, first, second):
    a = resolve_person(any_store, name=first)
    assert a.created is True
    assert resolve_person(any_store, name=second) == ResolveResult(
        rid=a.rid, created=False, confidence="high"
    )
    assert len(any_store.list_slugs()) == 1


def test_a_malformed_neighbor_is_skipped_not_fatal(tmp_path):
    """Stores load lazily, so a garbage profile.jsonld surfaces at ``.name``; the scan must catch it."""
    store = FilesystemProfileStore(tmp_path)
    store.create(_document(ORCID_A, "Jane Doe"), slug="doe-jane")
    bad = tmp_path / "garbage"
    bad.mkdir()
    (bad / "profile.jsonld").write_text("{not json", encoding="utf-8")
    assert resolve_person(store, name="Jane Doe").rid == ORCID_A


def test_an_infrastructure_error_aborts_the_resolve(any_store, monkeypatch):
    """A failed read must not quietly shrink the candidate set: fail closed."""
    _seed(any_store, JANE_A)
    monkeypatch.setattr(
        any_store, "get", lambda ref: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    with pytest.raises(RuntimeError):
        resolve_person(any_store, name="Jane Doe")


def test_the_mint_key_never_merges_two_people():
    """The mint key is at least as FINE as the matcher; equivalent spellings still converge."""
    same = {_deterministic_local_rid(n) for n in ("Jane Doe", "Doe, Jane", "Jane Doe Jr")}
    assert len(same) == 1
    distinct = [
        _deterministic_local_rid(n)
        for n in ("Jane Doe", "John Doe", "J.A. Doe", "J.B. Doe", "Jane A. Doe")
    ]
    assert len(set(distinct)) == len(distinct)


def test_a_conflicting_affiliation_still_defers_the_self_bind(any_store):
    """Two "J. Doe"s share the deterministic rid; only an affiliation conflict can tell them apart."""
    first = resolve_person(any_store, name="J. Doe", affiliation="MIT")
    r = resolve_person(any_store, name="J. Doe", affiliation="Stanford")
    assert r.rid is None
    assert [c.rid for c in r.candidates] == [first.rid]


def test_a_second_bucket_member_does_not_poison_self_binding(any_store):
    """Minting "J.B. Doe" beside "J.A. Doe" must not stop the first name from re-binding to itself."""
    ja = resolve_person(any_store, name="J.A. Doe")
    assert ja.created is True
    assert (
        resolve_person(any_store, name="J.B. Doe").rid is None
    )  # may or may not be the same person

    jb = resolve_person(any_store, name="J.B. Doe", create_new=True, affiliation="MIT")
    assert jb.created is True
    assert jb.rid != ja.rid

    assert resolve_person(any_store, name="J.A. Doe").rid == ja.rid
    assert resolve_person(any_store, rid=jb.rid).rid == jb.rid
    by_aff = resolve_person(any_store, name="J.B. Doe", affiliation="MIT")
    assert by_aff.rid is None
    assert {c.rid for c in by_aff.candidates} == {ja.rid, jb.rid}
    ambiguous = resolve_person(any_store, name="J. Doe")
    assert ambiguous.rid is None
    assert {c.rid for c in ambiguous.candidates} == {ja.rid, jb.rid}


def test_a_full_name_blocked_by_an_initials_stub_can_still_be_created(any_store):
    """The resolver must be total: a real "Jane Doe" is creatable beside a minted "J. Doe"."""
    stub = resolve_person(any_store, name="J. Doe")
    assert resolve_person(any_store, name="Jane Doe").rid is None
    jane = resolve_person(any_store, name="Jane Doe", create_new=True)
    assert jane.created is True
    assert jane.rid != stub.rid
    assert resolve_person(any_store, name="Doe, Jane").rid == jane.rid


def test_a_silent_orcid_candidate_keeps_the_deferral_from_the_selector(any_store):
    """An ORCID profile's silence on affiliation is silence, not disconfirmation."""
    stub = resolve_person(any_store, name="Jane Doe", affiliation="UVA")
    any_store.create(_document(ORCID_A, "Jane Doe"), slug="doe-jane-orcid")
    r = resolve_person(any_store, name="Jane Doe", affiliation="UVA")
    assert r.rid is None
    assert {c.rid for c in r.candidates} == {stub.rid, ORCID_A}


def test_two_people_sharing_a_full_name_resolve_by_affiliation(any_store):
    """A second real "Wei Zhang" is creatable without merging, and affiliation then selects between them."""
    uva = resolve_person(any_store, name="Wei Zhang", affiliation="UVA")
    assert uva.created is True
    assert resolve_person(any_store, name="Wei Zhang", affiliation="MIT").rid is None

    mit = resolve_person(any_store, name="Wei Zhang", affiliation="MIT", create_new=True)
    assert mit.created is True
    assert mit.rid != uva.rid

    assert resolve_person(any_store, name="Wei Zhang", affiliation="UVA").rid == uva.rid
    assert resolve_person(any_store, name="Zhang, Wei", affiliation="MIT").rid == mit.rid
    bare = resolve_person(any_store, name="Wei Zhang")
    assert bare.rid is None
    assert {c.rid for c in bare.candidates} == {uva.rid, mit.rid}


def test_self_binding_yields_to_a_compatible_orcid_profile(any_store):
    """A stub must not outrank a better-evidenced identity (the name-then-ORCID rule)."""
    stub = resolve_person(any_store, name="J. Doe")
    _seed(any_store, JANE_A)
    r = resolve_person(any_store, name="J. Doe")
    assert r.rid is None
    assert {c.rid for c in r.candidates} == {stub.rid, ORCID_A}


def test_private_profiles_never_appear_in_candidates(any_store):
    """The private tier is served to nobody but the owner, not even as a candidate or via the mint path."""
    private_rid = _deterministic_local_rid("Jenna Doe")
    doc = ProfileDocument(
        name="Jenna Doe", rid=private_rid, provenance="synthetic", visibility="private"
    )
    any_store.create(doc, slug="doe-jenna")
    r = resolve_person(any_store, name="Jenna Doe")
    assert r.created is True
    assert r.rid != private_rid
    assert all(c.rid != private_rid for c in r.candidates)


def test_a_wedged_directory_does_not_make_a_person_unmintable(tmp_path):
    """A create that died after mkdir leaves a directory list_slugs cannot see; the mint moves to the next slug."""
    store = FilesystemProfileStore(tmp_path)
    (tmp_path / "hopper-grace").mkdir()
    r = resolve_person(store, name="Grace Hopper")
    assert r.created is True
    assert store.exists(r.rid)


def test_the_mint_is_deterministic_across_stores(tmp_path, empty_sql_store):
    """Two services resolving the same unknown person compute the SAME rid, so a race cannot mint two."""
    a_root = tmp_path / "a"
    a_root.mkdir()
    ra = resolve_person(FilesystemProfileStore(a_root), name="Grace Hopper")
    rb = resolve_person(empty_sql_store, name="Hopper, Grace")
    assert ra.rid == rb.rid
    assert ra.created and rb.created


class TestCreateRace:
    """The mint must absorb ANY create failure that leaves the rid resolvable, never an occupant that is not this person."""

    def test_a_mid_race_winner_with_the_same_name_is_bound_not_duplicated(
        self, any_store, monkeypatch
    ):
        """Hide the winner from the scan, as mid-race: the loser binds to it, created=False."""
        _seed(any_store, HOPPER_LOCAL)
        monkeypatch.setattr(resolve_mod, "_compatible_profiles", lambda *a, **k: [])
        r = resolve_person(any_store, name="Grace Hopper")
        assert r == ResolveResult(rid=HOPPER_LOCAL[0], created=False, confidence="high")

    def test_a_fold_compatible_occupant_cannot_capture_the_name_either(self, any_store):
        """Self-identification needs the occupant's OWN name to compute the rid; "W. Zhang" there is a weak candidate."""
        rid = _deterministic_local_rid("Wei Zhang")
        any_store.create(_document(rid, "W. Zhang"), slug="zhang-w")
        r = resolve_person(any_store, name="Wei Zhang")
        assert r.rid is None
        assert [c.rid for c in r.candidates] == [rid]

    def test_an_unreadable_occupant_at_the_deterministic_rid_mints_fresh(
        self, any_store, monkeypatch
    ):
        """A profile at the deterministic rid that cannot be parsed is not handed out on faith."""
        rid = _deterministic_local_rid("Grace Hopper")
        real_exists, real_get = any_store.exists, any_store.get
        monkeypatch.setattr(
            any_store, "exists", lambda ref: True if ref == rid else real_exists(ref)
        )

        def corrupt_get(ref):
            if ref == rid:
                raise ProfileLoadError("x", "corrupt document")
            return real_get(ref)

        monkeypatch.setattr(any_store, "get", corrupt_get)
        r = resolve_person(any_store, name="Grace Hopper")
        assert r.created is True
        assert r.rid != rid

    def test_a_name_mismatched_occupant_is_never_returned(self, any_store):
        """A profile at this name's rid whose name does not compute it (renamed, or pushed there) cannot capture it."""
        rid = _deterministic_local_rid("Bob Smith")
        any_store.create(_document(rid, "Roberta Nguyen"), slug="nguyen-roberta")
        r = resolve_person(any_store, name="Bob Smith", affiliation="Michigan")
        assert r.created is True
        assert r.rid != rid

    @pytest.mark.parametrize(
        "boom, absorbed",
        [
            (ProfileWriteError("x", "taken"), True),
            (FileExistsError("dir"), True),
            (WriteHookError("policy_hook", RuntimeError("refused")), False),  # policy, not a race
        ],
        ids=["write-error", "file-exists", "hook-refusal"],
    )
    def test_create_stub_on_a_failed_create_with_the_rid_present(
        self, any_store, monkeypatch, boom, absorbed
    ):
        """A collision means the race resolved itself if the rid now exists; any other error propagates."""
        rid = HOPPER_LOCAL[0]
        _seed(any_store, HOPPER_LOCAL)

        def losing_create(document, *, slug):
            raise boom

        monkeypatch.setattr(any_store, "create", losing_create)
        if absorbed:
            assert _create_stub(any_store, rid=rid, name="Grace Hopper", affiliation=None) is False
        else:
            with pytest.raises(type(boom)):
                _create_stub(any_store, rid=rid, name="Grace Hopper", affiliation=None)

    def test_a_failure_with_no_profile_behind_it_propagates(self, any_store, monkeypatch):
        """If the create failed and the rid does not exist, nothing won: surface the error so the caller retries."""

        def broken_create(document, *, slug):
            raise RuntimeError("disk full")

        monkeypatch.setattr(any_store, "create", broken_create)
        with pytest.raises(RuntimeError, match="disk full"):
            resolve_person(any_store, name="Grace Hopper")


class TestNameThenOrcid:
    def test_an_orcid_arriving_later_defers_the_name_with_both_candidates(self, any_store):
        """A name-resolved stub whose ORCID arrives later is never auto-merged: the name defers with both, the caller picks."""
        by_name = resolve_person(any_store, name="Jane Doe", affiliation="UVA")
        assert is_local(by_name.rid)

        by_orcid = resolve_person(any_store, rid=ORCID_A, name="Jane Doe")
        assert by_orcid.rid == ORCID_A
        assert by_orcid.created is True

        again = resolve_person(any_store, name="Doe, Jane")
        assert again.rid is None
        assert again.confidence == "low"
        assert {c.rid for c in again.candidates} == {by_name.rid, ORCID_A}

        # Both identities keep resolving exactly.
        assert resolve_person(any_store, rid=by_name.rid).rid == by_name.rid
        assert resolve_person(any_store, rid=ORCID_A).rid == ORCID_A
