"""The verification proof envelope (``rp:proof``) and the proof kinds this
reference implementation understands.
"""

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, model_validator

from scholarcore.identity import is_rid, orcid_of

from .jsonld import JsonLdModel

logger = logging.getLogger(__name__)

#: Proof ``kind`` values this reference implementation understands. An open
#: set: a consumer must ignore a proof whose ``kind`` it does not recognize
#: (the same posture as an unknown manifest ``role``), so an unrecognized kind
#: is tolerated, not rejected.
KNOWN_PROOF_KINDS: frozenset[str] = frozenset(
    {
        "orcid_roundtrip",  # the ORCID record's website list points back here
        "domain_wellknown",  # a .well-known challenge on the claimed domain
        "key_signature",  # a detached JWS over the canonicalized document
        "institution",  # (reserved) an institution vouched via SSO/email
        "orcid_login",  # (registry-issued) the serving registry saw an owner sign in with this ORCID iD
    }
)

#: Proof kinds only the registry serving a document can issue. They are
#: computed when the document is served and are never persisted: every store
#: strips them on write, and a reader trusts one only when ``issuer`` is the
#: origin it fetched the document from.
REGISTRY_ISSUED_PROOF_KINDS: frozenset[str] = frozenset({"orcid_login"})

#: The members each known kind requires, by their serialized (JSON) names.
#: One table feeds both the Python validator (:meth:`Proof._check_kind_requirements`)
#: and the exported JSON Schema (an ``allOf`` of ``if kind == ... then
#: required``), so the two cannot drift. Format checks (URL, ORCID, date) stay
#: Python-only.
_KIND_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "key_signature": ("verificationMethod", "alg", "signatureValue"),
    "domain_wellknown": ("issuer", "challenge"),
    "orcid_roundtrip": ("issuer",),
    "orcid_login": ("issuer", "orcid", "verifiedAt"),
}


def _proof_kind(p: Any) -> Any:
    return p.get("kind") if isinstance(p, dict) else getattr(p, "kind", None)


def strip_registry_issued_proofs(proofs: list) -> list:
    """Return ``proofs`` without the registry-issued kinds.

    Accepts :class:`Proof` models or plain dicts (a serialized document's
    ``proof`` list) and returns a new list; the input is not mutated.
    """
    return [p for p in proofs or [] if _proof_kind(p) not in REGISTRY_ISSUED_PROOF_KINDS]


def strip_registry_issued_from_document(document: dict, *, where: str = "") -> dict:
    """Return ``document`` (a serialized profile dict) fit to persist.

    Drops every registry-issued proof from its ``proof`` list. When nothing is
    dropped the very same dict comes back, so a verbatim payload stays
    verbatim; otherwise a shallow copy with the filtered list. Logs once at
    INFO when something was dropped. The one helper every store backend calls
    before it persists a document.
    """
    if not isinstance(document, dict):
        return document
    proofs = document.get("proof")
    if not isinstance(proofs, list):
        return document
    kept = strip_registry_issued_proofs(proofs)
    if len(kept) == len(proofs):
        return document
    dropped = sorted({str(_proof_kind(p)) for p in proofs if p not in kept})
    logger.info(
        "dropped registry-issued proof(s) %s from %s: they are computed when served",
        dropped,
        where or document.get("rid") or "a document",
    )
    return {**document, "proof": kept}


def _proof_schema_extra(schema: dict[str, Any]) -> None:
    """Emit :data:`_KIND_REQUIREMENTS` as JSON Schema ``if/then`` rules."""
    schema["allOf"] = [
        {
            "if": {"properties": {"kind": {"const": kind}}, "required": ["kind"]},
            "then": {"required": list(members)},
        }
        for kind, members in _KIND_REQUIREMENTS.items()
    ]


class Proof(JsonLdModel):
    """One verification proof attached to a profile (``rp:proof``).

    The format standardizes the *envelope*: what is asserted (``kind``), who
    vouches (``issuer``), when it was last confirmed (``verifiedAt``), and,
    for checkable proofs, how to check it. It stays neutral on the identity
    *source*. A profile may carry zero, one, or many proofs at once; a
    consumer picks its own trust threshold over the proofs whose ``kind`` it
    understands.

    ``extra="allow"`` (inherited from :class:`JsonLdModel`) matters here:
    different kinds carry different members, and future kinds add members
    this version does not model. Unknown members are preserved, never
    rejected.
    """

    # Pydantic merges this with the inherited JsonLdModel config (extra="allow" stays).
    model_config = ConfigDict(json_schema_extra=_proof_schema_extra)

    #: What is asserted / how to check it. See :data:`KNOWN_PROOF_KINDS`.
    kind: str
    #: Who vouches: an ORCID IRI, a domain, a key id/URL, an institution IRI.
    issuer: str | None = None
    #: When this proof was last confirmed (ISO-8601). Per-proof; the top-level
    #: ``verifiedAt`` is retained as the ``orcid_roundtrip`` proof's timestamp.
    #: For ``orcid_login`` it is when the registry last confirmed the ORCID
    #: sign-in (required there).
    verified_at: str | None = Field(default=None, alias="verifiedAt")

    # --- key_signature members -----------------------------------------
    #: URL of the JWK Set holding the signing key, with a ``#<kid>`` fragment
    #: naming the exact key used (so rotation is unambiguous).
    verification_method: str | None = Field(default=None, alias="verificationMethod")
    #: JWS ``alg`` (this implementation emits ``EdDSA`` / Ed25519).
    alg: str | None = None
    #: The detached JWS compact serialization (``<header>..<signature>``).
    signature_value: str | None = Field(default=None, alias="signatureValue")

    # --- domain_wellknown members ----------------------------------------
    #: The challenge URL under the claimed domain a consumer fetches.
    challenge: str | None = None
    #: The token the challenge URL is expected to return.
    token: str | None = None

    # --- orcid_roundtrip members -----------------------------------------
    #: The matched researcher-URL found in the ORCID record's website list.
    matched_url: str | None = Field(default=None, alias="matchedUrl")

    # --- orcid_login members --------------------------------------------
    #: The bare canonical ORCID iD the registry saw an owner sign in with.
    #: Same form as ``rid``, so a reader compares the two as strings.
    orcid: str | None = None

    @model_validator(mode="after")
    def _check_kind_requirements(self) -> "Proof":
        """Enforce the members each *known* kind needs; tolerate unknown kinds.

        An unrecognized ``kind`` is not rejected: a future proof
        type must round-trip through an older validator untouched.
        """
        members = _KIND_REQUIREMENTS.get(self.kind)
        if members is None:
            return self
        values = {
            "issuer": self.issuer,
            "verifiedAt": self.verified_at,
            "verificationMethod": self.verification_method,
            "alg": self.alg,
            "signatureValue": self.signature_value,
            "challenge": self.challenge,
            "orcid": self.orcid,
        }
        missing = [n for n in members if not values.get(n)]
        if self.kind == "key_signature":
            if missing:
                raise ValueError(
                    f"proof kind 'key_signature' requires {missing}; a signature "
                    "proof is meaningless without the key pointer and the value"
                )
        elif self.kind == "domain_wellknown":
            if "issuer" in missing:
                raise ValueError(
                    "proof kind 'domain_wellknown' requires an issuer (the domain "
                    "whose control is claimed)"
                )
            if "challenge" in missing:
                raise ValueError(
                    "proof kind 'domain_wellknown' requires a challenge URL to "
                    "fetch under the claimed domain"
                )
        elif self.kind == "orcid_roundtrip":
            if missing:
                raise ValueError(
                    "proof kind 'orcid_roundtrip' requires an issuer (the ORCID "
                    "whose record points back to this profile)"
                )
        elif self.kind == "orcid_login":
            self._check_orcid_login(missing)
        return self

    def _check_orcid_login(self, missing: list[str]) -> None:
        """The ``orcid_login`` members, present and well formed."""
        parts = urlsplit(self.issuer or "")
        if "issuer" in missing or parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(
                "proof kind 'orcid_login' requires an issuer: the base URL of "
                "the registry that serves the profile"
            )
        orcid = self.orcid or ""
        if "orcid" in missing or not is_rid(orcid) or orcid_of(orcid) != orcid:
            raise ValueError(
                "proof kind 'orcid_login' requires orcid: a canonical ORCID iD "
                "(0000-0000-0000-0000 form)"
            )
        try:
            if "verifiedAt" in missing:
                raise ValueError
            datetime.fromisoformat(str(self.verified_at))
        except ValueError:
            raise ValueError(
                "proof kind 'orcid_login' requires verifiedAt: when the registry "
                "last confirmed the ORCID sign-in"
            ) from None
