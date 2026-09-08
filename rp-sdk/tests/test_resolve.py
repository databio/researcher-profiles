"""The rid resolver: cautious name matching, deterministic mint-on-miss.

Everything here runs on both store backends (the ``empty_store`` param
fixture), because the resolver is the entry point consumer services join through and
a behavior that held on only one backend would split identities the moment a
deployment switched stores.

Several tests here are regression tests for failures found by adversarial
review of earlier versions, each named for its scenario: the John/Jane silent
merge, the short-name mint 400, the mint-key/match-key mismatch, compact
initials ("J.A. Doe") read as a different person, the name-then-ORCID
identity split, the affiliation that contradicted a match being ignored, the
malformed neighbor that killed every resolve, and the create-race
interleavings that escaped the absorb.
"""

import pytest

from researcher_profiles.errors import ProfileWriteError
from researcher_profiles.resolve import (
    ResolveError,
    ResolveResult,
    _deterministic_local_rid,
    resolve_person,
)
from researcher_profiles.schema import ProfileDocument, is_local, is_rid
from researcher_profiles.store import FilesystemProfileStore, ProfileStore
from researcher_profiles.store.sql import SqlProfileStore

# ORCID's documented sample ids, all checksum-valid.
ORCID_A = "0000-0002-1825-0097"
ORCID_B = "0000-0004-4600-113X"
ORCID_C = "0000-0004-1003-0000"


def _document(rid: str, name: str, affiliation: str | None = None) -> ProfileDocument:
    provenance = "synthetic" if rid.startswith("local:") else "third_party"
    return ProfileDocument(name=name, rid=rid, provenance=provenance, affiliation=affiliation)


@pytest.fixture(params=["filesystem", "sql"])
def empty_store(request, tmp_path) -> ProfileStore:
    """An empty store, once per backend."""
    if request.param == "filesystem":
        return FilesystemProfileStore(tmp_path)
    store = SqlProfileStore("sqlite://")
    store.create_all()
    return store


class TestRidFirst:
    def test_orcid_hit_returns_the_existing_rid(self, empty_store):
        empty_store.create(_document(ORCID_A, "Jane A. Doe"), slug="doe-jane")
        r = resolve_person(empty_store, rid=ORCID_A)
        assert r == ResolveResult(rid=ORCID_A, created=False, confidence="exact")

    def test_orcid_miss_mints_a_stub_under_that_orcid(self, empty_store):
        r = resolve_person(empty_store, rid=ORCID_B, name="Ada Lovelace")
        assert r.rid == ORCID_B  # never a local: id when a valid ORCID is supplied
        assert r.created is True
        assert r.confidence == "exact"
        doc = empty_store.get(ORCID_B).metadata
        assert doc.provenance == "third_party"
        assert doc.level == "lite"
        assert doc.visibility == "internal"

    def test_orcid_miss_without_a_name_is_an_error_not_a_mint(self, empty_store):
        with pytest.raises(ResolveError, match="name"):
            resolve_person(empty_store, rid=ORCID_B)
        assert not empty_store.exists(ORCID_B)

    def test_a_malformed_orcid_is_refused(self, empty_store):
        with pytest.raises(ResolveError):
            resolve_person(empty_store, rid="0000-0002-1825-0090", name="X Y")

    def test_an_existing_local_rid_resolves_exactly(self, empty_store):
        """The disambiguation round-trip: re-resolve by a candidate's rid."""
        rid = _deterministic_local_rid("Grace Hopper")
        empty_store.create(_document(rid, "Grace Hopper"), slug="hopper-grace")
        r = resolve_person(empty_store, rid=rid)
        assert r == ResolveResult(rid=rid, created=False, confidence="exact")

    def test_an_unknown_local_rid_is_refused_not_minted(self, empty_store):
        """A caller must not be able to plant a profile at a chosen local id."""
        with pytest.raises(ResolveError, match="local"):
            resolve_person(empty_store, rid="local:mallory-abc123", name="Mallory")
        assert not empty_store.exists("local:mallory-abc123")

    def test_nothing_supplied_is_refused(self, empty_store):
        with pytest.raises(ResolveError):
            resolve_person(empty_store)


class TestNameMatch:
    def test_full_given_name_agreement_binds(self, empty_store):
        empty_store.create(_document(ORCID_A, "Jane A. Doe"), slug="doe-jane")
        for spelling in ("Doe, Jane", "Jane Doe", "jane DOE", "Jane B. Doe"):
            r = resolve_person(empty_store, name=spelling)
            assert r == ResolveResult(rid=ORCID_A, created=False, confidence="high")

    def test_a_different_given_name_is_a_different_person(self, empty_store):
        """The silent-merge case: John must not resolve to Jane merely because
        both fold to "doe j". He gets his own rid."""
        empty_store.create(_document(ORCID_A, "Jane Doe"), slug="doe-jane")
        r = resolve_person(empty_store, name="John Doe")
        assert r.created is True
        assert r.rid != ORCID_A
        assert is_local(r.rid)

    def test_compact_initials_are_initials_not_a_different_person(self, empty_store):
        """ "J.A. Doe" and "W.-K. Ng" are bibliographic initials. They must
        come back as weak candidates, not be excluded as a different person
        (which would mint a duplicate)."""
        empty_store.create(_document(ORCID_A, "Jane Doe"), slug="doe-jane")
        r = resolve_person(empty_store, name="J.A. Doe")
        assert r.rid is None
        assert [c.rid for c in r.candidates] == [ORCID_A]

        empty_store.create(_document(ORCID_C, "Wai-Kin Ng"), slug="ng-wai-kin")
        r2 = resolve_person(empty_store, name="Ng, W.-K.")
        assert r2.rid is None
        assert [c.rid for c in r2.candidates] == [ORCID_C]

    def test_an_initial_only_name_defers_without_corroboration(self, empty_store):
        """ "J. Doe" could be Jane or John; with no affiliation to corroborate,
        the one fold hit comes back as a candidate, not a bind."""
        empty_store.create(_document(ORCID_A, "Jane Doe"), slug="doe-jane")
        r = resolve_person(empty_store, name="J. Doe")
        assert r.rid is None
        assert r.confidence == "low"
        assert [c.rid for c in r.candidates] == [ORCID_A]
        assert len(empty_store.list_slugs()) == 1  # and nothing was minted

    def test_an_initial_only_name_binds_when_affiliation_corroborates(self, empty_store):
        empty_store.create(
            _document(ORCID_A, "Jane Doe", affiliation="University of Virginia"),
            slug="doe-jane",
        )
        r = resolve_person(empty_store, name="Doe, J.", affiliation="university of virginia")
        assert r == ResolveResult(rid=ORCID_A, created=False, confidence="high")

    def test_a_conflicting_affiliation_vetoes_a_full_name_match(self, empty_store):
        """Two different Wei Zhangs: the caller handed us the disconfirming
        affiliation, so a strong name match must defer, not merge."""
        empty_store.create(
            _document(ORCID_A, "Wei Zhang", affiliation="University of Virginia"),
            slug="zhang-wei",
        )
        r = resolve_person(empty_store, name="Wei Zhang", affiliation="MIT")
        assert r.rid is None
        assert r.confidence == "low"
        assert [c.rid for c in r.candidates] == [ORCID_A]
        assert len(empty_store.list_slugs()) == 1  # deferred, not minted

    def test_no_affiliation_still_binds_a_full_name_match(self, empty_store):
        empty_store.create(
            _document(ORCID_A, "Wei Zhang", affiliation="University of Virginia"),
            slug="zhang-wei",
        )
        r = resolve_person(empty_store, name="Zhang, Wei")
        assert r.rid == ORCID_A

    def test_multiple_compatible_profiles_defer_with_candidates(self, empty_store):
        empty_store.create(_document(ORCID_A, "Jane Doe"), slug="doe-jane")
        empty_store.create(_document(ORCID_C, "John Doe"), slug="doe-john")
        before = set(empty_store.list_slugs())
        r = resolve_person(empty_store, name="J. Doe")
        assert r.rid is None
        assert r.created is False
        assert r.confidence == "low"
        assert {c.rid for c in r.candidates} == {ORCID_A, ORCID_C}
        assert set(empty_store.list_slugs()) == before  # nothing minted

    def test_a_true_miss_mints_a_local_stub(self, empty_store):
        r = resolve_person(empty_store, name="Grace Hopper", affiliation="Yale")
        assert r.created is True
        assert r.confidence == "high"
        assert is_local(r.rid)
        assert is_rid(r.rid)
        doc = empty_store.get(r.rid).metadata
        assert doc.provenance == "third_party"
        assert doc.affiliation == "Yale"

    @pytest.mark.parametrize("name", ["Doe, J.", "Madonna", "J.-P. Sartre", "J.A. Doe"])
    def test_short_names_can_still_mint(self, empty_store, name):
        """Slug derivation needs two long tokens; these names have none. A
        display concern must never 400 an identity operation: the slug
        falls back to the rid body."""
        r = resolve_person(empty_store, name=name)
        assert r.created is True
        assert is_rid(r.rid)
        assert empty_store.exists(r.rid)

    def test_a_malformed_neighbor_is_skipped_not_fatal(self, tmp_path):
        """One garbage profile.jsonld in the store must not take down every
        resolve (both backends load lazily, so the parse error surfaces at
        .name; the scan must catch it there)."""
        store = FilesystemProfileStore(tmp_path)
        store.create(_document(ORCID_A, "Jane Doe"), slug="doe-jane")
        bad = tmp_path / "garbage"
        bad.mkdir()
        (bad / "profile.jsonld").write_text("{not json", encoding="utf-8")
        r = resolve_person(store, name="Jane Doe")
        assert r.rid == ORCID_A

    def test_an_infrastructure_error_aborts_the_resolve(self, empty_store, monkeypatch):
        """A failed read must not quietly shrink the candidate set: a
        shrunken set changes an identity decision. Fail closed."""
        empty_store.create(_document(ORCID_A, "Jane Doe"), slug="doe-jane")
        monkeypatch.setattr(
            empty_store, "get", lambda ref: (_ for _ in ()).throw(RuntimeError("db down"))
        )
        with pytest.raises(RuntimeError):
            resolve_person(empty_store, name="Jane Doe")


class TestIdempotency:
    def test_resolving_the_same_name_twice_mints_once(self, empty_store):
        first = resolve_person(empty_store, name="Grace Hopper")
        second = resolve_person(empty_store, name="Hopper, Grace")
        assert first.created is True
        assert second.created is False
        assert second.rid == first.rid
        assert len(empty_store.list_slugs()) == 1

    def test_resolving_the_same_orcid_twice_mints_once(self, empty_store):
        first = resolve_person(empty_store, rid=ORCID_C, name="Alice Wong")
        second = resolve_person(empty_store, rid=ORCID_C)
        assert (first.created, second.created) == (True, False)
        assert first.rid == second.rid == ORCID_C

    def test_the_mint_key_never_merges_two_people(self):
        """The mint key is at least as FINE as the matcher: names the
        matcher keeps apart (or merely cannot equate) must never mint the
        same rid. A race-collapse onto one rid is a merge, and merges are
        the unrecoverable failure. Equivalent spellings still converge."""
        same = {_deterministic_local_rid(n) for n in ("Jane Doe", "Doe, Jane", "Jane Doe Jr")}
        assert len(same) == 1
        distinct = [
            _deterministic_local_rid(n)
            for n in ("Jane Doe", "John Doe", "J.A. Doe", "J.B. Doe", "Jane A. Doe")
        ]
        assert len(set(distinct)) == len(distinct)

    def test_an_initials_only_name_is_idempotent_against_its_own_stub(self, empty_store):
        """ "Doe, J." mints; resolving it again must re-bind to that stub
        (recognized by its deterministic rid), not defer forever against
        our own mint."""
        first = resolve_person(empty_store, name="Doe, J.")
        second = resolve_person(empty_store, name="J. Doe")
        assert first.created is True
        assert second == ResolveResult(rid=first.rid, created=False, confidence="high")
        assert len(empty_store.list_slugs()) == 1

    def test_a_conflicting_affiliation_still_defers_the_self_bind(self, empty_store):
        """Two different "J. Doe"s at two institutions share the
        deterministic rid by construction; the affiliation conflict is the
        one signal that can tell them apart, so it must defer."""
        first = resolve_person(empty_store, name="J. Doe", affiliation="MIT")
        r = resolve_person(empty_store, name="J. Doe", affiliation="Stanford")
        assert r.rid is None
        assert [c.rid for c in r.candidates] == [first.rid]

    def test_a_very_long_name_mints_the_same_capped_rid_on_both_backends(self, empty_store):
        """The rid's readable part is capped (a filesystem caps directory
        names); the hash over the full key keeps it unique, and both
        backends must agree on the capped form."""
        name = "Maximiliana Alexandrina Bernadetta Konstantyna " * 4 + "Wolszczanowska"
        r = resolve_person(empty_store, name=name)
        assert r.created is True
        assert is_rid(r.rid)
        assert len(r.rid) < 80
        assert r.rid == _deterministic_local_rid(name)

    def test_a_second_bucket_member_does_not_poison_self_binding(self, empty_store):
        """The round-4 deadlock: once "J.A. Doe" is minted, creating "J.B.
        Doe" in the same fold bucket must not stop the FIRST name from
        resolving. Self-identification (the candidate at this name's own
        deterministic rid) binds past weak neighbors; the create_new person
        re-resolves by rid or affiliation."""
        ja = resolve_person(empty_store, name="J.A. Doe")
        assert ja.created is True

        # J.B. defers (correct: it may or may not be the same person)...
        deferred = resolve_person(empty_store, name="J.B. Doe")
        assert deferred.rid is None

        # ...and create_new creates the second person at a fresh id.
        jb = resolve_person(empty_store, name="J.B. Doe", create_new=True, affiliation="MIT")
        assert jb.created is True
        assert jb.rid != ja.rid

        # The first name still self-binds despite the crowded bucket; the
        # new person resolves by rid. Initials + institution alone never
        # picks between two weak candidates (the documented tradeoff for a
        # create_new person), and the ambiguous form defers with both.
        assert resolve_person(empty_store, name="J.A. Doe").rid == ja.rid
        assert resolve_person(empty_store, rid=jb.rid).rid == jb.rid
        by_aff = resolve_person(empty_store, name="J.B. Doe", affiliation="MIT")
        assert by_aff.rid is None
        assert {c.rid for c in by_aff.candidates} == {ja.rid, jb.rid}
        ambiguous = resolve_person(empty_store, name="J. Doe")
        assert ambiguous.rid is None
        assert {c.rid for c in ambiguous.candidates} == {ja.rid, jb.rid}

    def test_a_full_name_blocked_by_an_initials_stub_can_still_be_created(self, empty_store):
        """ "J. Doe" was minted; a real "Jane Doe" who is not that person
        must be creatable via create_new: the resolver must be total."""
        stub = resolve_person(empty_store, name="J. Doe")
        deferred = resolve_person(empty_store, name="Jane Doe")
        assert deferred.rid is None
        jane = resolve_person(empty_store, name="Jane Doe", create_new=True)
        assert jane.created is True
        assert jane.rid != stub.rid
        # And thereafter Jane binds by unique strong match.
        assert resolve_person(empty_store, name="Doe, Jane").rid == jane.rid

    def test_create_new_refuses_a_supplied_rid(self, empty_store):
        """A caller holding a rid resolves it; create_new alongside a rid
        would silently discard the rid and mint a duplicate."""
        with pytest.raises(ResolveError, match="mutually exclusive"):
            resolve_person(empty_store, rid=ORCID_A, name="Ada Lovelace", create_new=True)
        assert empty_store.list_slugs() == []

    def test_a_silent_orcid_candidate_keeps_the_deferral_from_the_selector(self, empty_store):
        """Round 6: a local stub carries the affiliation the caller itself
        stamped at mint; a richer ORCID profile often records none. The
        selector must treat the ORCID's silence as silence, not as
        disconfirmation, so the name-then-ORCID deferral stands even when
        the caller repeats its own affiliation."""
        stub = resolve_person(empty_store, name="Jane Doe", affiliation="UVA")
        empty_store.create(_document(ORCID_A, "Jane Doe"), slug="doe-jane-orcid")
        r = resolve_person(empty_store, name="Jane Doe", affiliation="UVA")
        assert r.rid is None
        assert {c.rid for c in r.candidates} == {stub.rid, ORCID_A}

    def test_two_people_sharing_a_full_name_resolve_by_affiliation(self, empty_store):
        """The round-5 merge trap: a second real "Wei Zhang" must be
        creatable WITHOUT merging into the first, and each must resolve
        with their affiliation; without one, the deferral is honest."""
        uva = resolve_person(empty_store, name="Wei Zhang", affiliation="UVA")
        assert uva.created is True

        deferred = resolve_person(empty_store, name="Wei Zhang", affiliation="MIT")
        assert deferred.rid is None  # conflict defers, never merges

        mit = resolve_person(empty_store, name="Wei Zhang", affiliation="MIT", create_new=True)
        assert mit.created is True
        assert mit.rid != uva.rid

        # Affiliation selects among the two same-named people.
        assert resolve_person(empty_store, name="Wei Zhang", affiliation="UVA").rid == uva.rid
        assert resolve_person(empty_store, name="Zhang, Wei", affiliation="MIT").rid == mit.rid
        bare = resolve_person(empty_store, name="Wei Zhang")
        assert bare.rid is None
        assert {c.rid for c in bare.candidates} == {uva.rid, mit.rid}

    def test_self_binding_yields_to_a_compatible_orcid_profile(self, empty_store):
        """A stub must not outrank a better-evidenced identity: with an
        ORCID profile also compatible, the initials name defers (the
        name-then-ORCID rule)."""
        stub = resolve_person(empty_store, name="J. Doe")
        empty_store.create(_document(ORCID_A, "Jane Doe"), slug="doe-jane")
        r = resolve_person(empty_store, name="J. Doe")
        assert r.rid is None
        assert {c.rid for c in r.candidates} == {stub.rid, ORCID_A}

    def test_no_local_rid_can_be_planted_not_even_the_deterministic_one(self, empty_store):
        """The round-5 planting vector: the deterministic rid is computable
        from a public name, so accepting it from outside would let any
        caller pre-plant a rival stub for a real person. EVERY unknown
        local rid is refused; new people go through create_new, where the
        resolver picks the id."""
        with pytest.raises(ResolveError, match="minted"):
            resolve_person(empty_store, rid="local:mallory-abc123", name="Jane Doe")
        with pytest.raises(ResolveError, match="minted"):
            resolve_person(empty_store, rid=_deterministic_local_rid("Jane Doe"), name="Jane Doe")
        assert empty_store.list_slugs() == []

    @pytest.mark.parametrize(
        "junk",
        ["Unknown", "et al", "Dr", "Corresponding Author", "Unknown Unknown", "Anonymous Author"],
    )
    def test_placeholder_names_are_refused_not_minted(self, empty_store, junk):
        """Token-aware: multi-token placeholders such as "Unknown Unknown" are
        refused too, not minted as a person."""
        with pytest.raises(ResolveError, match="placeholder"):
            resolve_person(empty_store, name=junk)
        with pytest.raises(ResolveError, match="placeholder"):
            resolve_person(empty_store, name=junk, create_new=True)
        assert empty_store.list_slugs() == []

    def test_a_junk_token_beside_a_real_name_is_fine(self, empty_store):
        r = resolve_person(empty_store, name="Unknown Smith")
        assert r.created is True

    def test_a_known_rid_wins_over_a_junk_display_name(self, empty_store):
        """rid-first means rid-first: a good ORCID with a garbage display
        string must return the exact hit, not a placeholder refusal."""
        empty_store.create(_document(ORCID_A, "Jane Doe"), slug="doe-jane")
        r = resolve_person(empty_store, rid=ORCID_A, name="Unknown")
        assert r == ResolveResult(rid=ORCID_A, created=False, confidence="exact")

    def test_restricted_profiles_never_appear_in_candidates(self, empty_store):
        """The restricted tier never leaves the machine, not even as a
        candidate name, and not via the mint path either: round 5 showed a
        restricted profile AT the deterministic rid being handed back by
        the collision absorb. A separate stub is minted instead."""
        restricted_rid = _deterministic_local_rid("Jenna Doe")
        doc = ProfileDocument(
            name="Jenna Doe",
            rid=restricted_rid,
            provenance="synthetic",
            visibility="restricted",
        )
        empty_store.create(doc, slug="doe-jenna")
        r = resolve_person(empty_store, name="Jenna Doe")
        assert r.created is True
        assert r.rid != restricted_rid
        assert all(c.rid != restricted_rid for c in r.candidates)

    def test_a_wedged_directory_does_not_make_a_person_unmintable(self, tmp_path):
        """A create that died between mkdir and writing profile.jsonld
        leaves a directory list_slugs cannot see; the mint must move to the
        next slug candidate instead of failing that name forever."""
        store = FilesystemProfileStore(tmp_path)
        (tmp_path / "hopper-grace").mkdir()  # the corpse of a dead create
        r = resolve_person(store, name="Grace Hopper")
        assert r.created is True
        assert store.exists(r.rid)

    def test_the_mint_is_deterministic_across_stores(self, tmp_path):
        """Two services (two stores) resolving the same unknown person
        compute the SAME rid: the race cannot mint two ids because there is
        only one id to mint."""
        a_root = tmp_path / "a"
        a_root.mkdir()
        a = FilesystemProfileStore(a_root)
        b = SqlProfileStore("sqlite://")
        b.create_all()
        ra = resolve_person(a, name="Grace Hopper")
        rb = resolve_person(b, name="Hopper, Grace")
        assert ra.rid == rb.rid
        assert ra.created and rb.created


class TestCreateRace:
    """The mint must absorb ANY create failure that leaves the rid
    resolvable. The concrete exception differs per backend and per
    interleaving (ProfileWriteError, FileExistsError from the directory,
    IntegrityError from the unique index). An occupant that is not
    this person must never be handed out."""

    def test_a_mid_race_winner_with_the_same_name_is_bound_not_duplicated(
        self, empty_store, monkeypatch
    ):
        """A real race winner carries the SAME name (both callers resolved
        the same person). Simulated by hiding the winner from the scan, as
        it would be mid-race: the loser binds to it, created=False."""
        rid = _deterministic_local_rid("Grace Hopper")
        empty_store.create(_document(rid, "Grace Hopper"), slug="hopper-grace")
        import researcher_profiles.resolve as resolve_mod

        monkeypatch.setattr(resolve_mod, "_compatible_profiles", lambda *a, **k: [])
        r = resolve_person(empty_store, name="Grace Hopper")
        assert r == ResolveResult(rid=rid, created=False, confidence="high")

    def test_a_fold_compatible_occupant_cannot_capture_the_name_either(self, empty_store):
        """The round-7 hole: a profile planted at the computable rid under
        a merely fold-COMPATIBLE name ("W. Zhang" at Wei Zhang's rid) must
        get no more deference than at any other rid: self-identification
        requires the occupant's OWN name to compute the rid. It defers as
        an ordinary weak candidate."""
        rid = _deterministic_local_rid("Wei Zhang")
        empty_store.create(_document(rid, "W. Zhang"), slug="zhang-w")
        r = resolve_person(empty_store, name="Wei Zhang")
        assert r.rid is None
        assert [c.rid for c in r.candidates] == [rid]

    def test_an_unreadable_occupant_at_the_deterministic_rid_mints_fresh(
        self, empty_store, monkeypatch
    ):
        """A profile at the deterministic rid that cannot be parsed must
        not be handed out on faith: the mint falls back to a fresh id."""
        from researcher_profiles.errors import ProfileLoadError

        rid = _deterministic_local_rid("Grace Hopper")
        real_exists, real_get = empty_store.exists, empty_store.get
        monkeypatch.setattr(
            empty_store, "exists", lambda ref: True if ref == rid else real_exists(ref)
        )

        def corrupt_get(ref):
            if ref == rid:
                raise ProfileLoadError("x", "corrupt document")
            return real_get(ref)

        monkeypatch.setattr(empty_store, "get", corrupt_get)
        r = resolve_person(empty_store, name="Grace Hopper")
        assert r.created is True
        assert r.rid != rid

    def test_a_name_mismatched_occupant_is_never_returned(self, empty_store):
        """The planting/supersede hole: a profile sitting at this
        name's deterministic rid whose name does not compute that rid
        (renamed since mint, or pushed there by someone else) must not
        capture the name: a fresh id is minted instead."""
        rid = _deterministic_local_rid("Bob Smith")
        empty_store.create(_document(rid, "Roberta Nguyen"), slug="nguyen-roberta")
        r = resolve_person(empty_store, name="Bob Smith", affiliation="Michigan")
        assert r.created is True
        assert r.rid != rid

    @pytest.mark.parametrize(
        "boom",
        [ProfileWriteError("x", "taken"), FileExistsError("dir")],
    )
    def test_a_slug_shaped_create_failure_with_the_rid_present_is_absorbed(
        self, empty_store, monkeypatch, boom
    ):
        """Direct contract of _create_stub: a collision surfaces as a
        ProfileWriteError or a FileExistsError, and if the rid exists
        afterward the race resolved itself. Any other exception is not a
        race and propagates."""
        from researcher_profiles.resolve import _create_stub

        rid = _deterministic_local_rid("Grace Hopper")
        empty_store.create(_document(rid, "Grace Hopper"), slug="hopper-grace")

        def losing_create(document, *, slug):
            raise boom

        monkeypatch.setattr(empty_store, "create", losing_create)
        assert _create_stub(empty_store, rid=rid, name="Grace Hopper", affiliation=None) is False

    def test_a_hook_refusal_propagates_even_when_the_rid_exists(self, empty_store, monkeypatch):
        """A host hook refusing the write is POLICY, not a race: it must
        never be absorbed into created=False."""
        from researcher_profiles.errors import WriteHookError
        from researcher_profiles.resolve import _create_stub

        rid = _deterministic_local_rid("Grace Hopper")
        empty_store.create(_document(rid, "Grace Hopper"), slug="hopper-grace")

        def refusing_create(document, *, slug):
            raise WriteHookError("policy_hook", RuntimeError("refused"))

        monkeypatch.setattr(empty_store, "create", refusing_create)
        with pytest.raises(WriteHookError):
            _create_stub(empty_store, rid=rid, name="Grace Hopper", affiliation=None)

    def test_a_failure_with_no_profile_behind_it_propagates(self, empty_store, monkeypatch):
        """If the create failed and the rid does not exist, nothing won any
        race: surfacing the error (so the caller retries) beats inventing
        an identity that was never persisted."""

        def broken_create(document, *, slug):
            raise RuntimeError("disk full")

        monkeypatch.setattr(empty_store, "create", broken_create)
        with pytest.raises(RuntimeError, match="disk full"):
            resolve_person(empty_store, name="Grace Hopper")


class TestNameThenOrcid:
    def test_an_orcid_arriving_later_defers_the_name_with_both_candidates(self, empty_store):
        """The normal lifecycle: a PI resolved by name (local stub) whose
        ORCID arrives later. The resolver does not auto-merge
        the two (every automatic merge heuristic is a silent-merge vector);
        the name defers with both rids as candidates, both rids keep
        resolving exactly, and the caller picks. That split is accepted
        and recoverable."""
        by_name = resolve_person(empty_store, name="Jane Doe", affiliation="UVA")
        assert is_local(by_name.rid)

        by_orcid = resolve_person(empty_store, rid=ORCID_A, name="Jane Doe")
        assert by_orcid.rid == ORCID_A
        assert by_orcid.created is True

        again = resolve_person(empty_store, name="Doe, Jane")
        assert again.rid is None
        assert again.confidence == "low"
        assert {c.rid for c in again.candidates} == {by_name.rid, ORCID_A}

        # Both identities keep resolving exactly, so stored references and
        # the disambiguation round-trip both work.
        assert resolve_person(empty_store, rid=by_name.rid).rid == by_name.rid
        assert resolve_person(empty_store, rid=ORCID_A).rid == ORCID_A
