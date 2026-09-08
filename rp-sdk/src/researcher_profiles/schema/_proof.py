"""The verification proof envelope (``rp:proof``) and the proof kinds this
reference implementation understands.
"""

from pydantic import Field, model_validator

from .jsonld import JsonLdModel

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
    }
)


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

    #: What is asserted / how to check it. See :data:`KNOWN_PROOF_KINDS`.
    kind: str
    #: Who vouches: an ORCID IRI, a domain, a key id/URL, an institution IRI.
    issuer: str | None = None
    #: When this proof was last confirmed (ISO-8601). Per-proof; the top-level
    #: ``verifiedAt`` is retained as the ``orcid_roundtrip`` proof's timestamp.
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

    @model_validator(mode="after")
    def _check_kind_requirements(self) -> "Proof":
        """Enforce the members each *known* kind needs; tolerate unknown kinds.

        An unrecognized ``kind`` is not rejected: a future proof
        type must round-trip through an older validator untouched.
        """
        if self.kind == "key_signature":
            missing = [
                n
                for n, v in (
                    ("verificationMethod", self.verification_method),
                    ("alg", self.alg),
                    ("signatureValue", self.signature_value),
                )
                if not v
            ]
            if missing:
                raise ValueError(
                    f"proof kind 'key_signature' requires {missing}; a signature "
                    "proof is meaningless without the key pointer and the value"
                )
        elif self.kind == "domain_wellknown":
            if not self.issuer:
                raise ValueError(
                    "proof kind 'domain_wellknown' requires an issuer (the domain "
                    "whose control is claimed)"
                )
            if not self.challenge:
                raise ValueError(
                    "proof kind 'domain_wellknown' requires a challenge URL to "
                    "fetch under the claimed domain"
                )
        elif self.kind == "orcid_roundtrip":
            if not self.issuer:
                raise ValueError(
                    "proof kind 'orcid_roundtrip' requires an issuer (the ORCID "
                    "whose record points back to this profile)"
                )
        return self
