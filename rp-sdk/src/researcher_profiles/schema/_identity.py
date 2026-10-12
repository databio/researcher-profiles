"""Identity primitives (rid): wrappers over scholarcore with RP-specific hints.

`rid` (researcher id) is the identity of a profile and the single join key
for every cross-system mapping. It takes exactly one of two forms:

  ORCID  ``0000-0002-1825-0097``          regex + ISO 7064 MOD 11-2 checksum
  local  ``local:darwin-charles-a3f19c``  explicitly-prefixed, minted once

Neither form is the "real" one. An ORCID is convenient because it is globally
resolvable; a local id is the ordinary identity of a researcher who has no
ORCID, of a historical figure, and of a synthetic agent, all cases this
format exists to carry. An asserted ORCID is never a credential:
what makes a profile ORCID-verified is `provenance`, not the shape of `rid`.

The profile directory name is a separate thing: a human-readable display
handle that carries no authority and may be renamed freely.
"""

import re
import secrets
import unicodedata

#: Directory-name grammar, for display handles only. Never apply it to a
#: ``rid``: it forbids uppercase and would reject ``X``-suffixed ORCIDs.
from researcher_profiles.utils.slug import SLUG_RE
from scholarcore.identity import LOCAL_RID_RE, is_rid
from scholarcore.identity import normalize_doi as _sc_normalize_doi
from scholarcore.identity import validate_rid as _sc_validate_rid


def validate_rid(v: str) -> str:
    """Validate a researcher id, returning the canonical form.

    Accepts a canonical ORCID (regex **and** checksum) or a ``local:`` id.
    Raises :class:`ValueError` naming which form failed and why.
    """
    try:
        return _sc_validate_rid(v)
    except ValueError as e:
        if "checksum" in str(e).lower():
            raise ValueError(
                f"invalid ORCID checksum: {v!r} (ISO 7064 MOD 11-2 check digit "
                "does not match). For a researcher with no ORCID, mint a local id "
                "with `rp mint-local-id`."
            ) from None
        raise


def validate_ref(ref: str) -> str:
    """Validate an API/CLI profile reference: a directory slug **or** a rid.

    Both forms are path-safe (no slashes, dots, or traversal), so a validated
    ref may be joined onto a profiles root. Raises :class:`ValueError` otherwise.
    """
    if not isinstance(ref, str):
        raise ValueError(f"profile ref must be a string, got {type(ref).__name__}")
    ref = ref.strip()
    if SLUG_RE.match(ref) or is_rid(ref):
        return ref
    raise ValueError(
        f"invalid profile ref {ref!r}: expected a slug ({SLUG_RE.pattern}), "
        f"an ORCID, or a local id ({LOCAL_RID_RE.pattern})"
    )


def normalize_doi(value: str | None) -> str | None:
    """The bare DOI carried by ``value`` (``10.xxxx/yyy``), or ``None``.

    Strips surrounding whitespace and a pasted resolver prefix
    (``https://doi.org/``, ``http://dx.doi.org/``, ``doi:``). Preserves case,
    which some consumers depend on.
    """
    return _sc_normalize_doi(value, lowercase=False)


def _slugify(text: str) -> str:
    """Reduce arbitrary text to the ``SLUG_RE`` grammar ("researcher" when empty)."""
    norm = unicodedata.normalize("NFKD", str(text))
    ascii_text = norm.encode("ascii", "ignore").decode("ascii").lower()
    out = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    out = re.sub(r"-{2,}", "-", out)
    return out or "researcher"


def mint_local_rid(name: str) -> str:
    """Mint a new local researcher id.

    The 6 hex characters are generated once and recorded in ``profile.jsonld``,
    so the id survives directory renames and name changes. Minting is an
    explicit operator action: a local id asserts "this identity is not an
    ORCID", a claim about the world.
    """
    return f"local:{_slugify(name)}-{secrets.token_hex(3)}"
