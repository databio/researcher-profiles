"""Person-identity resolution for the profile graph.

The hard part of building the graph is turning per-paper author *name strings*
into person nodes. This is the one place that decision is made, so the builder
and the query layer (COI check) fold names exactly the same way. Divergence
here is the classic COI false-negative.

Resolution priority (highest confidence first):

1. Cross-corpus paper join: the same work in two corpora makes those two
   ``rid``s coauthors with zero name matching. Handled in :mod:`.build` (it is a
   property of paper identity, not of a name); recorded as ``high`` confidence.
2. Name match to a profile: an author name that folds to a profile's name
   resolves to that profile's ``rid`` (``medium`` confidence).
3. External node: a name matching no profile becomes an ``external`` node
   keyed by its folded name (``low`` confidence).

The public entry point is :func:`normalize_name`, the single name-fold both
sides use, plus :class:`NameIndex`, which maps folded names to ``rid``.
"""

import re
import unicodedata
from typing import Optional

from ..schema import is_rid, normalize_doi

#: Tokens that carry no identity and must not become the surname.
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "msc", "bsc", "dr"}


def _fold(text: str) -> str:
    """Lowercased, diacritic-stripped, alphanumeric-only form of a token/string."""
    norm = unicodedata.normalize("NFKD", str(text))
    ascii_text = norm.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "", ascii_text)


def normalize_name(name: Optional[str]) -> Optional[str]:
    """Fold a person name to a canonical ``"surname initial"`` key.

    Case- and diacritic-insensitive, and initial-folding: ``"Jane A. Doe"``,
    ``"J. Doe"`` and ``"Doe, Jane"`` all fold to ``"doe j"``. Single-token names
    fold to the token itself. Returns ``None`` for empty input.

    This loses the middle name and the rest of the given name: a
    corpus lists the same coauthor as "Jane Doe" on one paper and "J.A. Doe" on
    the next, and an identity key that distinguished them would split one person
    into two nodes. The cost is that two people who share a surname and initial
    collide, which is why a name match is only ``medium`` confidence and the
    cross-corpus paper join (which needs no name) is preferred.
    """
    if not name:
        return None
    raw = str(name).strip()
    if not raw:
        return None

    # "Surname, Given": the comma unambiguously marks the surname.
    if "," in raw:
        surname_part, _, given_part = raw.partition(",")
        surname = _fold(surname_part)
        given = given_part.strip()
    else:
        tokens = [t for t in re.split(r"\s+", raw) if t]
        # Drop trailing generational/degree suffixes so they never become the
        # surname ("Jane Doe Jr" -> surname "doe").
        while len(tokens) > 1 and _fold(tokens[-1]) in _SUFFIXES:
            tokens.pop()
        if not tokens:
            return None
        if len(tokens) == 1:
            folded = _fold(tokens[0])
            return folded or None
        surname = _fold(tokens[-1])
        given = " ".join(tokens[:-1])

    if not surname:
        # Nothing usable as a surname (e.g. a name that was all punctuation).
        folded = _fold(given)
        return folded or None

    initial = ""
    for ch in given:
        f = _fold(ch)
        if f:
            initial = f[0]
            break
    return f"{surname} {initial}" if initial else surname


def paper_identity_key(
    *, doi: Optional[str], openalex_id: Optional[str], paper_id: Optional[str]
) -> Optional[str]:
    """A cross-corpus join key for one work: DOI -> OpenAlex id -> paper_id.

    Returns ``None`` when a work carries none of the three, so the caller can
    fall back to a per-profile-unique key and not collapse two unrelated works.
    """
    doi_n = normalize_doi(doi)
    if doi_n:
        return f"doi:{doi_n}"
    if openalex_id:
        oa = str(openalex_id).strip().lower()
        # Reduce a full OpenAlex IRI to its bare work id so the two forms join.
        oa = oa.rsplit("/", 1)[-1]
        if oa:
            return f"openalex:{oa}"
    if paper_id:
        pid = str(paper_id).strip().lower()
        if pid:
            return f"paperid:{pid}"
    return None


def normalize_institution(
    *, affiliation_id: Optional[str] = None, name: Optional[str] = None
) -> Optional[tuple[str, Optional[str]]]:
    """Return ``(key, display_name)`` for an institution, or ``None``.

    A ROR IRI (``affiliation_id``) is the join key when present; otherwise the
    folded institution name is. The display name is the human-readable label
    (the raw ``name``), used in COI reasons.
    """
    if affiliation_id:
        rid = str(affiliation_id).strip()
        if rid:
            return (f"ror:{rid.lower()}", name or rid)
    if name:
        folded = _fold(name)
        if folded:
            return (f"inst:{folded}", str(name).strip())
    return None


class NameIndex:
    """Maps a folded author name to the ``rid`` of the profile that owns it.

    Built once from the profile set. A folded name that two profiles share is
    ambiguous and is dropped (mapped to ``None`` internally) rather than binding
    an author to an arbitrary one of them. A name match must not silently pick
    the wrong human.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, Optional[str]] = {}

    def add(self, name: Optional[str], rid: str) -> None:
        key = normalize_name(name)
        if not key:
            return
        if key in self._by_name:
            if self._by_name[key] != rid:
                # Collision across two distinct rids: poison the key.
                self._by_name[key] = None
        else:
            self._by_name[key] = rid

    def resolve(self, name: Optional[str]) -> Optional[str]:
        """The ``rid`` an author name folds to, or ``None`` if unknown/ambiguous."""
        key = normalize_name(name)
        if not key:
            return None
        return self._by_name.get(key)


def resolve_descriptor(
    index: NameIndex,
    *,
    rid: Optional[str] = None,
    orcid: Optional[str] = None,
    name: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve an inbound author descriptor to ``(node_key, kind)``.

    Priority: an explicit ``rid`` (or ``orcid``, which is an rid form) wins; then
    a name match against the profile set; otherwise an external node keyed by the
    folded name. Raises ``ValueError`` when nothing usable is supplied.

    ``kind`` is ``"profiled"`` when the key is an rid and ``"external"`` when it
    is a folded name. Note a supplied rid is trusted as profiled even if it has
    no node in the graph: the caller still wants edges looked up against it.
    """
    if rid and is_rid(rid):
        return (rid, "profiled")
    if orcid and is_rid(orcid):
        return (orcid, "profiled")
    matched = index.resolve(name)
    if matched:
        return (matched, "profiled")
    folded = normalize_name(name)
    if folded:
        return (folded, "external")
    raise ValueError("author descriptor has no rid, orcid, or usable name")


__all__ = [
    "normalize_name",
    "normalize_institution",
    "paper_identity_key",
    "NameIndex",
    "resolve_descriptor",
]
