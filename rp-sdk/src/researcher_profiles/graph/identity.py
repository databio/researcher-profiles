"""Person-identity resolution for the profile graph.

The builder and the COI check must fold names identically; any divergence is
a COI false negative.

Resolution priority (highest confidence first):

1. Cross-corpus paper join: the same work in two corpora makes those two
   ``rid``s coauthors with zero name matching. Handled in :mod:`.build` (it is a
   property of paper identity, not of a name); recorded as ``high`` confidence.
2. Name match to a profile: an author name that folds to a profile's name
   resolves to that profile's ``rid`` (``medium`` confidence).
3. External node: a name matching no profile becomes an ``external`` node
   keyed by its folded name (``low`` confidence).
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

    Dropping the rest of the given name keeps "Jane Doe" and "J.A. Doe" as one
    person. The cost is that people sharing a surname and initial collide,
    which is why a name match is only ``medium`` confidence.
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
        # "Jane Doe Jr" -> surname "doe".
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

    ``None`` when a work carries none of the three.
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
    folded name is.
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

    A folded name two profiles share is ambiguous and resolves to ``None``,
    so a name match never silently picks the wrong person.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, Optional[str]] = {}

    def add(self, name: Optional[str], rid: str) -> None:
        key = normalize_name(name)
        if not key:
            return
        if key in self._by_name:
            if self._by_name[key] != rid:
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

    A supplied rid counts as ``"profiled"`` even with no node in the graph.
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
