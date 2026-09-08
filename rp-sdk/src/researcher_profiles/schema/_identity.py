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

#: Directory-name grammar. Applies only to display handles (profile directory
#: names, the ``slug``). Never apply it to a ``rid``: it forbids uppercase and
#: would reject every ``X``-suffixed ORCID (three exist in the tree today).
#: The single canonical copy lives in ``utils.slug`` (a stdlib-only leaf);
#: ``api/upload.py`` and ``client/`` share this same object. Re-exported here
#: because this module and ``schema`` are historical import sites for it.
from researcher_profiles.utils.slug import SLUG_RE

# scholarcore is the one implementation of the rid grammar; the wrappers below
# add RP-specific hints and defaults on top of it.
from scholarcore.identity import LOCAL_RID_RE, is_rid
from scholarcore.identity import normalize_doi as _sc_normalize_doi
from scholarcore.identity import validate_rid as _sc_validate_rid


def validate_rid(v: str) -> str:
    """Validate a researcher id, returning the canonical form.

    Accepts a canonical ORCID (regex **and** checksum) or a ``local:`` id.
    Raises :class:`ValueError` naming which form failed and why. Wraps
    scholarcore's validate_rid with an RP-specific hint for checksum errors.
    """
    try:
        return _sc_validate_rid(v)
    except ValueError as e:
        # Add RP-specific hint for checksum errors
        if "checksum" in str(e).lower():
            raise ValueError(
                f"invalid ORCID checksum: {v!r} (ISO 7064 MOD 11-2 check digit "
                "does not match). For a researcher with no ORCID, mint a local id "
                "with `rp mint-local-id`."
            ) from None
        raise


def validate_ref(ref: str) -> str:
    """Validate an API/CLI profile reference: a directory slug **or** a rid.

    This is the boundary contract. Both forms are inherently path-safe (no
    slashes, no dots, no traversal), so a validated ref may be joined onto a
    profiles root. Raises :class:`ValueError` otherwise.
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
    (``https://doi.org/``, ``http://dx.doi.org/``, ``doi:``). Preserves case
    since some systems (e.g. OpenAlex) preserve original case and consumers
    depend on that. Delegates to scholarcore's normalize_doi.
    """
    return _sc_normalize_doi(value, lowercase=False)


def _slugify(text: str) -> str:
    """Reduce arbitrary text to the ``SLUG_RE`` grammar.

    RP-specific: uses "researcher" as empty fallback (scholarcore uses "person").
    """
    norm = unicodedata.normalize("NFKD", str(text))
    ascii_text = norm.encode("ascii", "ignore").decode("ascii").lower()
    out = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    out = re.sub(r"-{2,}", "-", out)
    return out or "researcher"


def mint_local_rid(name: str) -> str:
    """Mint a new local researcher id.

    The 6 hex characters are generated once, here, and are meant to be recorded
    in ``profile.jsonld`` and never regenerated, so the id survives both
    directory renames and name changes.

    Minting is an explicit operator action, because a local id asserts "this
    profile's identity is not an ORCID", which is a claim about the world. It
    is an ordinary path, not an escape hatch: synthetic, historical, and
    no-ORCID researchers all live here.

    RP-specific: uses "researcher" as empty fallback (scholarcore uses "person").
    """
    return f"local:{_slugify(name)}-{secrets.token_hex(3)}"
