"""Canonical identifiers for the four scholarcore entities.

Each entity has exactly one join key:

- Person: ``rid``, a canonical ORCID or an explicitly-prefixed
  ``local:`` id.
- Paper: normalized DOI (primary) or PMID (secondary). PMCID and arXiv id
  have normalizers too, for the downstream kernels that compare them.
- Award: ``application_id`` (see :mod:`scholarcore.funding`).
- Opportunity: ``opportunity_number`` (see :mod:`scholarcore.funding`).

Only Person and Paper need bespoke normalization logic, so that is what lives
here; ``application_id`` and ``opportunity_number`` are plain strings assigned
by whatever external system of record issues them, and are carried as-is.
"""

import re
import secrets
import unicodedata

_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")

#: Locally-minted researcher id, for people with no ORCID. The literal
#: ``local:`` prefix contains a ``:`` and lowercase letters, so it can never
#: match ``_ORCID_RE`` (anchored to digits and hyphens) and can never collide
#: with the ORCID namespace.
LOCAL_RID_RE = re.compile(r"^local:[a-z0-9][a-z0-9-]*-[0-9a-f]{6}$")

#: A resolver prefix a DOI is commonly pasted with, with or without a scheme.
#: Stripped case-insensitively.
_DOI_RESOLVER_RE = re.compile(r"^(https?://)?(dx\.)?doi\.org/", re.I)

#: A ``doi:`` label, stripped case-insensitively after any resolver prefix.
_DOI_LABEL_RE = re.compile(r"^doi:", re.I)

#: Prefixes a PMID is commonly pasted with. Stripped case-insensitively.
_PMID_PREFIX_RE = re.compile(r"^(https?://(www\.)?ncbi\.nlm\.nih\.gov/pubmed/|pmid:)", re.I)


def _orcid_checksum_ok(orcid: str) -> bool:
    """Check an ORCID's ISO 7064 MOD 11-2 check digit over its first 15 digits.

    Args:
        orcid: A string already matching ``_ORCID_RE``.

    Returns:
        True when the trailing check digit (which may be ``X`` for 10) is
        valid.
    """
    digits = orcid.replace("-", "")
    if len(digits) != 16:
        return False
    total = 0
    for ch in digits[:15]:
        if not ch.isdigit():
            return False
        total = (total + int(ch)) * 2
    remainder = total % 11
    result = (12 - remainder) % 11
    check = digits[15]
    expected = "X" if result == 10 else str(result)
    return check == expected


def is_local(rid: str) -> bool:
    """Return True when ``rid`` is a locally-minted identifier, not an ORCID."""
    return str(rid).startswith("local:")


def orcid_of(rid: str | None) -> str | None:
    """Return the ORCID carried by ``rid``, or None when it is a local id.

    This is the only way an ORCID is derived from a person: there is no
    separate ``orcid`` field to drift out of sync with ``rid``.
    """
    if not rid:
        return None
    return None if is_local(rid) else str(rid)


def validate_rid(v: str) -> str:
    """Validate a researcher id, returning its canonical form.

    Accepts a canonical ORCID (regex and checksum) or a ``local:`` id.

    Args:
        v: The candidate rid.

    Returns:
        The validated rid, unchanged.

    Raises:
        ValueError: If ``v`` is not a well-formed rid, naming which form
            failed and why.
    """
    if not isinstance(v, str):
        raise ValueError(f"rid must be a string, got {type(v).__name__}")
    v = v.strip()
    if not v:
        raise ValueError("rid must not be empty")
    if is_local(v):
        if not LOCAL_RID_RE.match(v):
            raise ValueError(
                f"malformed local rid: {v!r} "
                f"(expected {LOCAL_RID_RE.pattern}, e.g. "
                "'local:darwin-charles-a3f19c')"
            )
        return v
    if not _ORCID_RE.match(v):
        raise ValueError(
            f"malformed rid: {v!r}: must be either a canonical ORCID "
            f"({_ORCID_RE.pattern}) or a local id ({LOCAL_RID_RE.pattern})"
        )
    if not _orcid_checksum_ok(v):
        raise ValueError(
            f"invalid ORCID checksum: {v!r} (ISO 7064 MOD 11-2 check digit does not match)"
        )
    return v


def is_rid(v: str) -> bool:
    """Return True when ``v`` is a well-formed rid (either form). Never raises."""
    try:
        validate_rid(v)
    except (ValueError, TypeError):
        return False
    return True


def _slugify(text: str) -> str:
    """Reduce arbitrary text to a lowercase, hyphenated slug."""
    norm = unicodedata.normalize("NFKD", str(text))
    ascii_text = norm.encode("ascii", "ignore").decode("ascii").lower()
    out = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    out = re.sub(r"-{2,}", "-", out)
    return out or "person"


def mint_local_rid(name: str) -> str:
    """Mint a new local researcher id for a person with no ORCID.

    The 6 hex characters are generated once and meant to be stored and never
    regenerated, so the id survives name changes.

    Args:
        name: The person's display name, used to derive a readable slug.

    Returns:
        A new ``local:`` rid, e.g. ``local:darwin-charles-a3f19c``.
    """
    return f"local:{_slugify(name)}-{secrets.token_hex(3)}"


def normalize_doi(value: str | None, *, lowercase: bool = False) -> str | None:
    """Return the bare DOI carried by ``value``, or None.

    Strips surrounding whitespace, one pasted resolver prefix
    (``https://doi.org/``, ``http://dx.doi.org/``, or a bare ``doi.org/``),
    then a ``doi:`` label, then any leading ``/``. Only a LEADING prefix is
    stripped; a ``doi.org/`` inside the DOI is kept.

    Args:
        value: A raw DOI string, possibly with a resolver prefix.
        lowercase: If True, lowercase the DOI. Default is False to preserve
            case, since some systems (e.g. OpenAlex) preserve the original
            case and consumers may depend on that. ``lowercase=True`` is the
            join key: downstream kernels (paperkit, lumenoia's ``*_norm``
            columns) call it that way.

    Returns:
        The normalized DOI (``10.xxxx/yyy``), or None if ``value`` is empty.
    """
    if not value:
        return None
    doi = _DOI_RESOLVER_RE.sub("", str(value).strip())
    doi = _DOI_LABEL_RE.sub("", doi).lstrip("/").strip()
    if lowercase:
        doi = doi.lower()
    return doi or None


def normalize_pmid(value: str | int | None) -> str | None:
    """Return the bare numeric PMID carried by ``value``, or None.

    Strips surrounding whitespace and a pasted URL or ``pmid:`` prefix.

    Args:
        value: A raw PMID, as a string, int, or prefixed URL.

    Returns:
        The normalized PMID as a digit string, or None if ``value`` is empty
        or carries no digits.
    """
    if not value and value != 0:
        return None
    pmid = _PMID_PREFIX_RE.sub("", str(value).strip()).strip()
    return pmid if pmid.isdigit() else None


def normalize_pmcid(value: str | int | None) -> str | None:
    """Return the PubMed Central id carried by ``value`` as ``PMC<digits>``, or None.

    Uppercases, strips surrounding whitespace and a ``PMCID:`` label, and
    collapses repeated leading ``PMC`` prefixes (``PMCPMC9999``). The rest
    must be digits; leading zeros are kept, because a padded id is a data
    defect to flag, not an alias.

    Args:
        value: A raw PMCID, with or without the ``PMC`` prefix.

    Returns:
        The normalized PMCID, or None if ``value`` is empty or is not
        ``PMC`` followed by digits.
    """
    if value is None:
        return None
    pmcid = str(value).strip().upper()
    if pmcid.startswith("PMCID:"):
        pmcid = pmcid[len("PMCID:") :].strip()
    while pmcid.startswith("PMC"):
        pmcid = pmcid[3:].strip()
    return f"PMC{pmcid}" if pmcid.isdigit() else None


def normalize_arxiv_id(value: str | None) -> str | None:
    """Return the versionless arXiv id carried by ``value``, or None.

    Lowercases, strips surrounding whitespace, an ``arxiv:`` label, trailing
    slashes, and a trailing version (``v1``, ``v2``), so every version of a
    preprint is one id.

    Args:
        value: A raw arXiv id, new style (``2401.12345v2``) or old style
            (``hep-th/9901001``).

    Returns:
        The normalized id, or None if ``value`` is empty.
    """
    if value is None:
        return None
    arxiv = str(value).strip().lower()
    if arxiv.startswith("arxiv:"):
        arxiv = arxiv[len("arxiv:") :]
    arxiv = re.sub(r"v\d+$", "", arxiv.strip().rstrip("/")).strip()
    return arxiv or None
