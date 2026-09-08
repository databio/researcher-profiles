"""Authoritative name/rid resolution, with mint-on-miss.

This is the entity-resolution entry point consumer services (grant
trackers, knowledge bases, program managers) call to turn the person data
they hold (a free-text PI name, a bare ORCID) into a consistent ``rid``. It
is not ``/match``: that route is read-only semantic search and
non-deterministic. Resolution here is deterministic and cautious. The same
person, resolved the same way twice, converges on the same ``rid``. When
the evidence cannot decide, the resolver defers rather than guessing,
because an identity system's worst failure is a silent merge.

The pipeline (:func:`resolve_person`):

1. rid given (an ORCID, or a ``local:`` id this resolver minted): a store hit
   returns it exactly. An ORCID miss mints a stub profile at that ORCID (a
   usable name is required). An unknown ``local:`` rid is refused, because
   local ids are chosen only by this resolver; accepting one would let a
   caller plant identities at chosen (and, for the deterministic form,
   publicly computable) ids.
2. Name only: placeholder strings ("Unknown", "et al", "Anonymous
   Author") are refused outright. Profiles sharing the coarse fold
   (surname + first initial, via
   :func:`~researcher_profiles.graph.identity.normalize_name`, the one
   name-fold in the package) are compared on their given names:

   - full given names agree ("Jane Doe" vs "Doe, Jane"; middle names are
     ignored): a ``"strong"`` match.
   - full given names differ ("Jane Doe" vs "John Doe"): not the same
     person, however the surname folds, so it is excluded outright.
   - either side has only initials ("J. Doe", "J.A. Doe", "W.-K. Ng"):
     ``"weak"``, because the evidence cannot distinguish Jane from John.

   A unique strong match binds, unless the caller's ``affiliation``
   conflicts with the profile's. With several strong matches (several real
   people sharing a full name), the affiliation selects only when it fully
   separates them: exactly one candidate agrees and every other strong
   candidate conflicts. A candidate with no affiliation on record is
   silent, not disconfirmed, and keeps the deferral. With no strong match,
   a unique self-identification binds past weak neighbors: a candidate at
   this exact name's deterministic mint rid (see below) whose own name
   computes that rid, with both halves checked because the rid is publicly
   computable. This yields to a better-evidenced identity (a compatible
   ORCID profile) when one is also present. A unique weak candidate binds
   on affiliation agreement, the one sole-evidence bind (see the
   tradeoffs below). Binding on a bare fold hit is never done.
3. Deferral: anything the rules above cannot decide comes back with
   ``rid=None`` and the compatible profiles as ``candidates`` (name and
   affiliation included). The caller confirms and re-resolves with the
   chosen profile's rid, or, when none of the candidates is their person,
   re-resolves with ``create_new=True`` to mint a fresh identity past the
   deferral. ``create_new`` is an explicit human-confirmed decision: the
   resolver picks the id (the caller supplies none, so nothing can be
   planted), and calling it twice mints twice.
4. True miss: a ``local:`` rid is minted and a stub profile created. The
   mint is deterministic: the rid derives from the fully folded name
   (surname + every given token), so the same name always computes the
   same rid. A concurrent duplicate collides in ``store.create`` and is
   absorbed, and a later resolve of the same name recognizes the stub by
   its rid (the self-identification above) even from a crowded bucket.
   The key is at least as fine as the strong match, so a race collapse can
   never merge two people the matcher keeps apart. Matcher-tolerated
   spelling differences (a middle initial) can race-split instead; this is
   accepted, because splits are recoverable and merges are not. If the
   deterministic rid is already occupied by a profile the scan withheld
   (a restricted one), the mint falls back to a
   fresh random id rather than returning the hidden profile's rid.

Accepted tradeoffs: a person resolved by name
first and by ORCID later ends up with a local stub and an ORCID profile.
The name resolves, then defers with both as candidates, and the caller
picks (usually the ORCID); the resolver never merges records on its own.
Two real people sharing a full name resolve only by affiliation, and defer
without one. A person created via ``create_new`` (or behind a restricted
occupant) has a random rid, so an initials-only spelling of their name
defers rather than self-binding; re-resolve by rid instead. Nickname and
particle-surname spelling variants can split. The unique-weak bind accepts
affiliation as the sole deciding evidence for an initials-only name:
"Doe, J." plus institution is common input for a typical deployment, and a
second J. Doe at the same institution not yet in the store would be wrongly
matched. ``visibility="restricted"`` profiles are invisible to name
matching (that tier never leaves the machine), so resolving a restricted
person's name mints a separate stub. Likewise a hidden occupant of the
deterministic rid whose affiliation conflicts is silently split into a
fresh person at high confidence, where the same evidence on a visible
profile would defer. This is the safe direction (splits recover, merges
do not), but it is an asymmetry. A ``create_new`` random id can collide
with a name's deterministic rid at ~2^-24 per call (shared namespace),
surfacing as ``created=False`` with a same-named person's rid. The
candidate scan loads every profile document (not an indexed query):
~1.5s per resolve at 800 profiles. This is fine at a typical deployment's
hundreds, and the store needs a fold-keyed index before that grows by an
order of magnitude. Every minted stub adds to that cost for every later
resolve, so a runaway consumer loop degrades the whole identity plane
(quotas are currently a no-op).

Stub profiles are minted ``provenance="third_party"`` (a real person
asserted by a calling service, not ``synthetic``, which means "not a real
person", and not ``orcid_verified``, which requires the ORCID round-trip
this resolver never performs), ``level="lite"`` and
``visibility="internal"``, so a later full build supersedes them by rid
and nothing minted here leaks onto the public read plane.
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import ProfileError, ProfileWriteError, WriteHookError
from .graph.identity import _SUFFIXES, _fold, normalize_name
from .schema import ProfileDocument, is_local, mint_local_rid, orcid_of, validate_rid
from .utils.slug import SlugError
from .utils.slug import resolve_slug as _derive_slug

__all__ = [
    "Candidate",
    "ResolveError",
    "ResolveResult",
    "resolve_person",
]


class ResolveError(ValueError):
    """The resolve request is unusable: no identity fields, a malformed
    rid, an unknown ``local:`` rid, a placeholder name, or an ORCID miss
    with no usable name to mint a stub from."""


#: Folded tokens that are placeholders, not name parts. A name is refused
#: when every token folds into this set ("Unknown Unknown", "Anonymous
#: Author", "et al"), because each such string would mint a "person" that
#: every later placeholder silently resolves to. "Unknown Smith" passes
#: because Smith is a name.
_JUNK_TOKENS = frozenset(
    {
        "al",
        "anon",
        "anonymous",
        "author",
        "authors",
        "corresponding",
        "dr",
        "et",
        "etal",
        "na",
        "none",
        "null",
        "staff",
        "tbd",
        "test",
        "the",
        "unknown",
        "unnamed",
        "various",
    }
)


def _is_junk_name(name: str) -> bool:
    """True when ``name`` is a placeholder, not a person."""
    parts = _name_parts(name)
    if parts is None:
        return False  # unusable for other reasons; other checks report that
    surname, givens = parts
    return all(token in _JUNK_TOKENS for token in [surname, *givens])


@dataclass(frozen=True)
class Candidate:
    """One profile an undecidable name could mean."""

    rid: str
    name: str
    affiliation: Optional[str] = None


@dataclass(frozen=True)
class ResolveResult:
    """The outcome of one resolve.

    ``rid`` is ``None`` only in the deferred case, distinct from "no
    match", which mints. ``created`` says whether this call minted the
    profile. ``confidence`` is ``"exact"`` for a rid identity, ``"high"``
    for a corroborated name match or a fresh mint, ``"low"`` for a
    deferral. When a deferral's candidates are all wrong, re-resolve with
    ``create_new=True`` to mint a fresh identity.
    """

    rid: Optional[str]
    created: bool
    confidence: str  # "exact" | "high" | "low"
    candidates: tuple[Candidate, ...] = field(default=())


# ---------------------------------------------------------------------------
# Name parsing and comparison
# ---------------------------------------------------------------------------


def _given_tokens(raw_tokens: list[str]) -> list[str]:
    """Folded given-name tokens, with initials expanded and suffixes dropped.

    ``"J.A."`` and ``"W.-K."`` become ``["j", "a"]`` / ``["w", "k"]``. A
    dotted or hyphenated token whose pieces are all single letters is a run
    of initials, and folding it whole ("ja") would masquerade as a full
    given name and wrongly exclude the person it abbreviates. Suffix tokens
    (Jr, PhD) carry no identity and are dropped wherever they appear.
    """
    out: list[str] = []
    for token in raw_tokens:
        if _fold(token) in _SUFFIXES:
            continue
        pieces = [_fold(p) for p in re.split(r"[.\-]", token)]
        pieces = [p for p in pieces if p]
        if len(pieces) > 1 and all(len(p) == 1 for p in pieces):
            out.extend(pieces)
        else:
            folded = _fold(token)
            if folded:
                out.append(folded)
    return out


def _name_parts(name: str) -> Optional[tuple[str, list[str]]]:
    """``(folded_surname, folded_given_tokens)`` for a display name.

    Uses the same surname detection as :func:`normalize_name` (a comma
    marks the surname; otherwise it is the last non-suffix token), but
    keeps the given tokens rather than reducing to one initial. The fold
    decides which profiles are worth comparing; this decides whether they
    are the same person.
    """
    raw = str(name or "").strip()
    if not raw:
        return None
    if "," in raw:
        surname_part, _, given_part = raw.partition(",")
        surname = _fold(surname_part)
        raw_givens = [t for t in re.split(r"\s+", given_part.strip()) if t]
    else:
        tokens = [t for t in re.split(r"\s+", raw) if t]
        while len(tokens) > 1 and _fold(tokens[-1]) in _SUFFIXES:
            tokens.pop()
        if not tokens:
            return None
        if len(tokens) == 1:
            folded = _fold(tokens[0])
            return (folded, []) if folded else None
        surname = _fold(tokens[-1])
        raw_givens = tokens[:-1]
    givens = _given_tokens(raw_givens)
    if not surname:
        # Nothing usable as a surname (all punctuation); mirror
        # normalize_name and treat the folded remainder as the identity.
        folded = _fold("".join(raw_givens))
        return (folded, []) if folded else None
    return (surname, givens)


def _given_strength(query_givens: list[str], profile_givens: list[str]) -> Optional[str]:
    """How the given names of two fold-key sharers compare.

    ``"strong"``: full first given names agree (or neither has one) and
    the fold hit is the same person. ``"weak"``: at least one side carries
    only an initial, so Jane vs John is indistinguishable and the match
    needs corroboration. ``None``: full given names disagree, meaning
    different people whatever the surname says. Middle names are
    ignored: "Jane A. Doe" and "Jane Doe" are one person in every corpus
    this serves.
    """
    q = query_givens[0] if query_givens else ""
    p = profile_givens[0] if profile_givens else ""
    if not q and not p:
        return "strong"
    if not q or not p:
        return "weak"
    if len(q) > 1 and len(p) > 1:
        return "strong" if q == p else None
    return "weak" if q[0] == p[0] else None


def _mint_key(name: str) -> Optional[str]:
    """The deterministic-mint key: surname plus every given token.

    At least as fine as the strong-match class: two names this key equates
    ("Jane Doe" / "Doe, Jane" / "Jane Doe Jr") are ones the matcher also
    calls the same person, so the concurrent-race collapse can never merge
    two people the matcher keeps apart ("J.A. Doe" vs "J.B. Doe" carry
    different keys). The cost is the reverse: matcher-tolerated spelling
    differences (a middle initial present or absent) mint different rids
    if they race, a recoverable split, which is preferred over a merge.
    """
    parts = _name_parts(name)
    if parts is None:
        return None
    surname, givens = parts
    return " ".join([surname, *givens])


def _rid_for_key(key: str) -> str:
    """The ``local:`` rid a mint key always produces.

    The hash is over the full key (that is the identity). The readable
    slug part is capped so a very long name cannot exceed a filesystem's
    name limit; the two backends must mint the same rid for the same
    person.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:6]
    slug_part = "-".join(key.split())[:48].rstrip("-")
    return f"local:{slug_part}-{digest}"


def _deterministic_local_rid(name: str) -> str:
    """Mint the ``local:`` rid this name always mints.

    This differs from ``schema.mint_local_rid`` (random suffix), which is
    what ``create_new`` and the occupied-rid fallback use for an id not
    keyed to a name.
    """
    key = _mint_key(name)
    if not key:
        raise ResolveError(f"name {name!r} folds to nothing usable")
    return _rid_for_key(key)


# ---------------------------------------------------------------------------
# Stub minting
# ---------------------------------------------------------------------------


def _stub_document(rid: str, name: str, affiliation: Optional[str]) -> ProfileDocument:
    return ProfileDocument(
        name=name,
        rid=rid,
        provenance="third_party",
        level="lite",
        # A resolver stub must not appear on the public read plane: nobody
        # has claimed it and nothing in it has been verified.
        visibility="internal",
        affiliation=affiliation or None,
        hasCitationGraph=False,
        hasEmbeddingIndex=False,
    )


def _slug_candidates(store: Any, rid: str, name: str) -> list[str]:
    """Directory names to try for the stub, never a reason to fail on its own.

    The name-derived slug first, then the rid body, which always satisfies
    the slug grammar (short names like "Doe, J." or "Madonna" derive no
    name slug at all, and a display concern must not 400 an identity
    operation). More than one candidate exists because a slug can be
    unusable for reasons ``list_slugs`` cannot see, for example a directory
    left behind by a create that died mid-write. Trying the next name
    heals that wedge instead of making the person permanently unmintable.
    """
    taken = set(store.list_slugs())
    out: list[str] = []
    try:
        out.append(_derive_slug({"name": name, "orcid": orcid_of(rid)}, taken=taken))
    except SlugError:
        pass
    base = rid.split(":", 1)[1] if is_local(rid) else rid.lower()
    for candidate in (base, *(f"{base}-{n}" for n in range(2, 10))):
        if candidate not in taken and candidate not in out:
            out.append(candidate)
    return out


def _create_stub(store: Any, *, rid: str, name: str, affiliation: Optional[str]) -> bool:
    """Create the stub profile for ``rid``; returns True when this call created it.

    ``store.create`` runs in one write unit, so a host's pre-commit hooks
    (ownership, staleness) fire exactly as they do for any other
    create. A concurrent resolve minting the same deterministic rid surfaces
    as a ``ProfileWriteError`` or as a raw ``FileExistsError`` from the
    directory (a backend that raises anything else for a losing race must
    wrap it in one of those). Whatever the shape, if the rid now exists the
    race resolved itself and this call lost. Only such a slug-shaped failure
    earns the next candidate; anything else (a hook's policy refusal, a dead
    database) would fail identically under every name, so it propagates on
    the first attempt.
    """
    document = _stub_document(rid, name, affiliation)
    last_error: Optional[Exception] = None
    for slug in _slug_candidates(store, rid, name):
        try:
            store.create(document, slug=slug)
            return True
        except WriteHookError:
            # A host hook refused the write on policy. That is a decision,
            # not a race. It propagates even when the rid happens to
            # exist, and it is never retried under another slug.
            raise
        except (ProfileWriteError, FileExistsError) as e:
            if store.exists(rid):
                return False
            last_error = e
    raise ResolveError(
        f"could not create a profile for {name!r} (rid {rid!r}): every "
        f"directory-name candidate failed; last error: {last_error}"
    ) from last_error


# ---------------------------------------------------------------------------
# The candidate scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Scored:
    rid: str
    name: str
    affiliation: Optional[str]
    strength: str  # "strong" | "weak"


def _compatible_profiles(store: Any, fold_key: str, query_givens: list[str]) -> list[_Scored]:
    """Profiles that could be this person, scored strong/weak.

    A linear scan via the coarse fold, then a given-name comparison;
    profiles whose full given names conflict with the query are excluded
    here, not deferred on. Every profile read happens inside the try: both
    backends load lazily, so the parse error a malformed neighbor raises
    surfaces at ``.name``, not at ``get``. A malformed or concurrently
    deleted neighbor is skipped; an infrastructure error aborts the resolve
    rather than quietly shrinking the candidate set, because a shrunken set
    changes an identity decision.
    """
    out: list[_Scored] = []
    for slug in store.list_slugs():
        try:
            prof = store.get(slug)
            pname = str(prof.name or "")
            rid = prof.rid
            affiliation = prof.metadata.affiliation
            visibility = prof.metadata.visibility
        except ProfileError:  # malformed or concurrently-deleted neighbor
            continue
        if visibility == "restricted":
            # The restricted tier never leaves the machine, not even as a
            # candidate's name in a resolve response. The mint path also
            # refuses to hand back a restricted occupant's rid (see the
            # occupied-rid fallback in resolve_person).
            continue
        if not rid or normalize_name(pname) != fold_key:
            continue
        parts = _name_parts(pname)
        strength = _given_strength(query_givens, parts[1] if parts else [])
        if strength is None:
            continue
        out.append(_Scored(rid=rid, name=pname, affiliation=affiliation, strength=strength))
    return out


def _affiliations_conflict(query: Optional[str], profile: Optional[str]) -> bool:
    q, p = _fold(query or ""), _fold(profile or "")
    return bool(q) and bool(p) and q != p


def _affiliations_agree(query: Optional[str], profile: Optional[str]) -> bool:
    q, p = _fold(query or ""), _fold(profile or "")
    return bool(q) and q == p


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def _resolve_create_new(store: Any, *, name: str, affiliation: Optional[str]) -> ResolveResult:
    """An explicit new-person mint: no matching, resolver-chosen random id."""
    if _is_junk_name(name):
        raise ResolveError(
            f"name {name!r} is a placeholder, not a person; refusing to mint an identity for it"
        )
    if _mint_key(name) is None:
        # The same usability bar as the name path: an identity nothing
        # can ever match by name is permanent junk in the scan.
        raise ResolveError(f"name {name!r} folds to nothing usable")
    minted = mint_local_rid(name)
    created = _create_stub(store, rid=minted, name=name, affiliation=affiliation)
    return ResolveResult(rid=minted, created=created, confidence="high")


def _resolve_by_rid(
    store: Any, *, rid: str, name: str, affiliation: Optional[str]
) -> ResolveResult:
    """rid-first: an ORCID is the identity; a minted ``local:`` id is too."""
    try:
        rid = validate_rid(rid)
    except ValueError as e:
        raise ResolveError(str(e)) from e
    if store.exists(rid):
        return ResolveResult(rid=rid, created=False, confidence="exact")
    if is_local(rid):
        # A local: rid is only ever chosen by this resolver, never
        # supplied from outside: accepting one for a profile that does
        # not exist would let a caller plant identities at chosen (and,
        # for the deterministic form, publicly computable) ids.
        raise ResolveError(
            f"no profile for {rid!r}: a local: rid can be resolved only "
            "after the resolver has minted it (to create a new person "
            "past a deferral, re-resolve with create_new=true instead)"
        )
    if not name:
        raise ResolveError(
            f"no profile for {rid!r} and no name supplied: a stub profile "
            "needs a name. Pass name alongside the rid."
        )
    if _is_junk_name(name):
        raise ResolveError(
            f"name {name!r} is a placeholder, not a person; a stub "
            f"profile for {rid!r} needs a real display name"
        )
    created = _create_stub(store, rid=rid, name=name, affiliation=affiliation)
    return ResolveResult(rid=rid, created=created, confidence="exact")


def _self_identified(
    candidates: list[_Scored],
    *,
    affiliation: Optional[str],
    mint_key: Optional[str],
    mint_rid: Optional[str],
) -> Optional[str]:
    """The rid of a candidate that is this name's own identity, if any.

    A candidate at this name's own deterministic rid is this name's identity
    by construction, and it binds even with weak neighbors in the fold bucket.
    Otherwise a second bucket member would permanently un-resolve a name
    whose resolution was idempotent until then. It yields to a better-evidenced identity (a
    compatible ORCID profile: the name-then-ORCID deferral stands) and to a
    conflicting affiliation.

    Both halves of the check matter: the rid must match and the occupant's own
    name must compute it. The rid is publicly computable, so a profile pushed
    onto it under a merely fold-compatible name ("W. Zhang" at Wei Zhang's
    rid) must get no more deference than it would at any other rid.
    """
    own = [
        c
        for c in candidates
        if mint_rid is not None and c.rid == mint_rid and _mint_key(c.name) == mint_key
    ]
    better = [c for c in candidates if not is_local(c.rid)]
    if len(own) == 1 and not better and not _affiliations_conflict(affiliation, own[0].affiliation):
        return own[0].rid
    return None


def _affiliation_selected(strong: list[_Scored], *, affiliation: Optional[str]) -> Optional[str]:
    """Affiliation as selector among several same-named people.

    Exactly one strong candidate agrees and every other strong candidate
    conflicts. Both halves matter: a candidate with no affiliation on record
    is silent, not disconfirmed, so it must keep the deferral. Otherwise a
    local stub carrying the affiliation the caller itself stamped at mint
    would outrank a richer ORCID profile that never recorded one. Weak
    (initials-only) pools never select; see the unique-weak rule for the one
    corroborated weak bind.
    """
    agreeing = [c for c in strong if _affiliations_agree(affiliation, c.affiliation)]
    if len(agreeing) == 1 and all(
        _affiliations_conflict(affiliation, c.affiliation)
        for c in strong
        if c.rid != agreeing[0].rid
    ):
        return agreeing[0].rid
    return None


def _bind_by_name(
    candidates: list[_Scored],
    *,
    affiliation: Optional[str],
    mint_key: Optional[str],
    mint_rid: Optional[str],
) -> Optional[str]:
    """The rid the name evidence binds to, or ``None`` to defer.

    Pure: no store, no I/O. All four bind rules live here (unique strong
    candidate, self-identification, unique weak candidate with affiliation
    agreement, affiliation as selector among several strong candidates).
    """
    strong = [c for c in candidates if c.strength == "strong"]
    if len(strong) == 1 and not _affiliations_conflict(affiliation, strong[0].affiliation):
        return strong[0].rid

    if not strong:
        own_rid = _self_identified(
            candidates, affiliation=affiliation, mint_key=mint_key, mint_rid=mint_rid
        )
        if own_rid is not None:
            return own_rid
        # A unique weak match binds on affiliation agreement, the one
        # sole-evidence bind (a documented tradeoff): "Doe, J."
        # plus an institution column from the same source is common input
        # for consumer services, and requiring a human for every such row
        # would make the resolver useless for it. The risk (a second J. Doe
        # at the same institution, not yet in the store) is accepted; set the
        # affiliation aside to force a deferral instead.
        if len(candidates) == 1 and _affiliations_agree(affiliation, candidates[0].affiliation):
            return candidates[0].rid

    if len(strong) > 1:
        return _affiliation_selected(strong, affiliation=affiliation)
    return None


def _mint_on_miss(
    store: Any, *, name: str, affiliation: Optional[str], mint_key: str, mint_rid: str
) -> ResolveResult:
    """Mint deterministically, so the same name always computes the same rid.

    A concurrent duplicate collides in ``store.create`` and is absorbed. A rid
    that turns out to be occupied despite the empty scan is bound only when
    the occupant provably is this person: visible, its name computing this
    exact mint key, and no affiliation conflict. That is the concurrent race
    winner. Any other occupant (restricted, malformed, renamed since mint, or
    a profile pushed onto the computable rid by someone else) must not be
    handed out; a fresh random id is minted instead.
    """
    if store.exists(mint_rid):
        occupant = None
        try:
            prof = store.get(mint_rid)
            occupant = (
                str(prof.name or ""),
                prof.metadata.visibility,
                prof.metadata.affiliation,
            )
        except ProfileError:
            pass
        if (
            occupant is not None
            and occupant[1] != "restricted"
            and _mint_key(occupant[0]) == mint_key
            and not _affiliations_conflict(affiliation, occupant[2])
        ):
            return ResolveResult(rid=mint_rid, created=False, confidence="high")
        minted = mint_local_rid(name)
        created = _create_stub(store, rid=minted, name=name, affiliation=affiliation)
        return ResolveResult(rid=minted, created=created, confidence="high")
    created = _create_stub(store, rid=mint_rid, name=name, affiliation=affiliation)
    return ResolveResult(rid=mint_rid, created=created, confidence="high")


def resolve_person(
    store: Any,
    *,
    rid: Optional[str] = None,
    name: Optional[str] = None,
    affiliation: Optional[str] = None,
    create_new: bool = False,
) -> ResolveResult:
    """Resolve a person descriptor to a ``rid``, minting a stub on a true miss.

    Resolution is rid-first, cautious on names, and deterministic on
    mints; see the module docstring for the full pipeline.
    ``affiliation`` is stamped on any minted stub, selects among
    same-named candidates, and vetoes a match it contradicts.
    ``create_new=True`` skips matching and mints a fresh identity for
    ``name``, the answer to a deferral whose candidates are all wrong. The resolver picks the id, and calling it twice creates two
    people.

    Raises :class:`ResolveError` when the request itself is unusable. It
    never raises for "no such person"; that is the mint path, not an
    error.
    """
    rid = (rid or "").strip()
    name = (name or "").strip()
    if not rid and not name:
        raise ResolveError("supply at least one of rid or name")

    # An explicit new-person mint: no matching, resolver-chosen random id.
    if create_new:
        if rid:
            raise ResolveError(
                "create_new and rid are mutually exclusive: a caller holding "
                "a rid resolves it (rid-first); create_new exists for a "
                "person who has no id yet. Accepting both would silently "
                "discard the rid and mint a duplicate identity."
            )
        return _resolve_create_new(store, name=name, affiliation=affiliation)

    # 1. rid-first: an ORCID is the identity; a minted local: id is too.
    if rid:
        return _resolve_by_rid(store, rid=rid, name=name, affiliation=affiliation)

    # 2. Name path: coarse fold to find comparable profiles, given names to
    #    decide, affiliation to select and to veto.
    if _is_junk_name(name):
        raise ResolveError(
            f"name {name!r} is a placeholder, not a person; refusing to "
            "match or mint an identity for it"
        )
    fold_key = normalize_name(name)
    if not fold_key:
        raise ResolveError(f"name {name!r} folds to nothing usable")
    parts = _name_parts(name)
    query_givens = parts[1] if parts else []
    compatible = _compatible_profiles(store, fold_key, query_givens)

    mint_key = _mint_key(name)
    mint_rid = _rid_for_key(mint_key) if mint_key else None

    bound = _bind_by_name(compatible, affiliation=affiliation, mint_key=mint_key, mint_rid=mint_rid)
    if bound is not None:
        return ResolveResult(rid=bound, created=False, confidence="high")

    # 3. Deferral: compatible profiles exist but the evidence cannot pick
    #    one. Mint nothing, guess nothing; the caller confirms and
    #    re-resolves with the chosen profile's rid, or with
    #    ``create_new=True`` when none of them is their person.
    if compatible:
        return ResolveResult(
            rid=None,
            created=False,
            confidence="low",
            candidates=tuple(
                Candidate(rid=c.rid, name=c.name, affiliation=c.affiliation) for c in compatible
            ),
        )

    # 4. True miss. mint_key and mint_rid are non-None here: the
    #    fold-usability check above refused every input _mint_key returns
    #    None for.
    return _mint_on_miss(
        store, name=name, affiliation=affiliation, mint_key=mint_key, mint_rid=mint_rid
    )
