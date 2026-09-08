"""Cryptographic signing and verification for researcher profiles.

This is the ``key_signature`` proof (see :class:`researcher_profiles.schema.Proof`):
the researcher publishes a public key at a well-known URL and signs
``profile.jsonld`` so any third party can verify authenticity and integrity
offline, independent of any central service. Neither ORCID nor Google
Scholar offers this, and federation depends on it.

Mechanism (see docs/rp-spec/index.md):

- Canonicalization: RFC 8785 JCS. We canonicalize the concrete JSON bytes
  (UTF-8, lexicographically sorted keys, no whitespace), not the RDF graph.
  This sidesteps JSON-LD / URDNA2015 canonicalization entirely, so a verifier
  needs no JSON-LD processor and never dereferences the ``@context`` (matching
  docs/rp-spec/index.md). Our documents contain only strings, integers, booleans,
  and ``null``, never floating-point, so the one genuinely hard part of JCS
  (ES6 number formatting) does not arise; :func:`jcs` rejects a float rather
  than emit a divergent encoding.
- Signature: detached JWS, EdDSA / Ed25519 (RFC 7515). ``jose`` libraries
  are ubiquitous, so third parties verify with a stock dependency. The
  signature is detached (the compact serialization is ``<header>..<sig>``
  with the payload segment empty), so ``profile.jsonld`` stays clean JSON-LD
  that non-verifying crawlers ignore.
- Pre-image: the entire profile object with the ``proof`` member removed,
  then JCS-canonicalized. Removing ``proof`` before signing is what lets the
  signature live inside the document it signs.

This module is import-heavy (it pulls ``cryptography``) and is therefore an
opt-in tier: install ``researcher-profiles[signing]``. Nothing on the profile
load path imports it, so a bare ``import researcher_profiles`` stays cheap.
"""

import base64
import binascii
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

#: Relative path (under a profile's ``<base>``) of the published JWK Set.
WELL_KNOWN_KEYS_PATH = ".well-known/researcher-profile-keys.json"

#: The one JWS algorithm this implementation emits and verifies.
ALG = "EdDSA"


# ---------------------------------------------------------------------------
# base64url (no padding), per JOSE
# ---------------------------------------------------------------------------


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ---------------------------------------------------------------------------
# RFC 8785 JSON Canonicalization Scheme (JCS)
# ---------------------------------------------------------------------------


def _jcs_str(value: Any) -> str:
    """Emit the canonical JCS text for ``value``.

    Keys are sorted by Unicode code point, which equals RFC 8785's UTF-16
    code-unit ordering for every Basic-Multilingual-Plane character (the only
    characters that appear in profile keys). Floats are rejected: our documents
    carry none, and permitting one would require ES6 number formatting that
    ``json.dumps`` does not guarantee.
    """
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise TypeError(
            "JCS canonicalization here refuses floats: profile documents carry "
            "no floating-point numbers, and permitting one would need ES6 "
            "number formatting this canonicalizer does not implement"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_jcs_str(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: kv[0])
        return (
            "{"
            + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{_jcs_str(v)}" for k, v in items)
            + "}"
        )
    raise TypeError(f"cannot canonicalize value of type {type(value).__name__}")


def jcs(obj: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 bytes of ``obj``."""
    return _jcs_str(obj).encode("utf-8")


def signing_preimage(profile: dict[str, Any]) -> bytes:
    """The exact bytes a signature covers: the profile minus ``proof``, JCS'd.

    Independent implementations must construct this identically: strip the
    ``proof`` member, then JCS-canonicalize the remaining object, or the
    signature will not verify byte-for-byte.
    """
    stripped = {k: v for k, v in profile.items() if k != "proof"}
    return jcs(stripped)


# ---------------------------------------------------------------------------
# Keys: JWK, thumbprint kid, JWK Set
# ---------------------------------------------------------------------------


def generate_private_key() -> Ed25519PrivateKey:
    """Generate a fresh Ed25519 signing key."""
    return Ed25519PrivateKey.generate()


def private_key_to_pem(key: Ed25519PrivateKey) -> bytes:
    """Serialize a private key as unencrypted PKCS#8 PEM."""
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_private_key(pem: bytes | str | Path) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from PEM bytes, text, or a file path."""
    if isinstance(pem, Path):
        pem = pem.read_bytes()
    elif isinstance(pem, str):
        pem = pem.encode("utf-8")
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(
            f"expected an Ed25519 private key, got {type(key).__name__}; this "
            "implementation signs with EdDSA / Ed25519 only"
        )
    return key


def _public_raw(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def jwk_thumbprint(x_b64u: str) -> str:
    """RFC 7638 thumbprint (used as ``kid``) for an OKP/Ed25519 key.

    Computed over the canonical JSON of the required members only, so the same
    key always yields the same ``kid`` and rotation is unambiguous.
    """
    members = {"crv": "Ed25519", "kty": "OKP", "x": x_b64u}
    digest = hashlib.sha256(jcs(members)).digest()
    return _b64u(digest)


def public_jwk(key: Ed25519PrivateKey | Ed25519PublicKey) -> dict[str, str]:
    """Return the public JWK (with a thumbprint ``kid``) for a key."""
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    x = _b64u(_public_raw(pub))
    return {
        "kty": "OKP",
        "crv": "Ed25519",
        "x": x,
        "use": "sig",
        "alg": ALG,
        "kid": jwk_thumbprint(x),
    }


def jwk_set(jwks: list[dict[str, str]]) -> dict[str, Any]:
    """Wrap one or more JWKs as a JWK Set (the well-known key document)."""
    return {"keys": list(jwks)}


def jwk_set_json(jwks: list[dict[str, str]]) -> str:
    """The JWK Set document as canonical, stable text."""
    return json.dumps(jwk_set(jwks), indent=2, sort_keys=True) + "\n"


def _public_key_from_jwk(jwk: dict[str, Any]) -> Ed25519PublicKey:
    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise ValueError(
            f"unsupported JWK: kty={jwk.get('kty')!r} crv={jwk.get('crv')!r}; "
            "only OKP/Ed25519 keys verify here"
        )
    return Ed25519PublicKey.from_public_bytes(_b64u_decode(jwk["x"]))


# ---------------------------------------------------------------------------
# Detached JWS sign / verify
# ---------------------------------------------------------------------------


def _detached_jws(private_key: Ed25519PrivateKey, payload: bytes, kid: str) -> str:
    header = {"alg": ALG, "kid": kid}
    header_b64 = _b64u(jcs(header))
    payload_b64 = _b64u(payload)
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = private_key.sign(signing_input)
    # Detached: the payload segment is empty (<header>..<signature>).
    return f"{header_b64}..{_b64u(signature)}"


def sign_profile(
    profile: dict[str, Any],
    private_key: Ed25519PrivateKey | bytes | str | Path,
    *,
    base_url: str,
) -> dict[str, Any]:
    """Build a ``key_signature`` proof for ``profile``.

    Signs the pre-image (profile minus ``proof``, JCS-canonicalized) with
    ``private_key`` and returns a proof dict whose ``verificationMethod`` points
    at the exact key (``<base>/.well-known/researcher-profile-keys.json#<kid>``).
    The caller appends the returned proof to ``profile["proof"]``.
    """
    key = (
        private_key if isinstance(private_key, Ed25519PrivateKey) else load_private_key(private_key)
    )
    jwk = public_jwk(key)
    kid = jwk["kid"]
    payload = signing_preimage(profile)
    jws = _detached_jws(key, payload, kid)
    method = f"{base_url.rstrip('/')}/{WELL_KNOWN_KEYS_PATH}#{kid}"
    return {
        "kind": "key_signature",
        "issuer": method.split("#", 1)[0],
        "verificationMethod": method,
        "alg": ALG,
        "signatureValue": jws,
    }


def verify_signature(profile: dict[str, Any], jwks: dict[str, Any]) -> bool:
    """Verify every ``key_signature`` proof on ``profile`` against ``jwks``.

    Returns ``True`` iff the profile carries at least one ``key_signature``
    proof and every such proof verifies: its ``kid`` names a key in the JWK Set,
    and the detached JWS validates against the reconstructed pre-image. A
    profile with no ``key_signature`` proof returns ``False`` (there is nothing
    to trust), never raising for a missing proof.
    """
    keys_by_kid = {k["kid"]: k for k in jwks.get("keys", []) if "kid" in k}
    payload = signing_preimage(profile)
    payload_b64 = _b64u(payload)

    sig_proofs = [
        p
        for p in profile.get("proof", [])
        if isinstance(p, dict) and p.get("kind") == "key_signature"
    ]
    if not sig_proofs:
        return False

    for proof in sig_proofs:
        jws = proof.get("signatureValue")
        method = proof.get("verificationMethod", "")
        if not jws or "." not in jws:
            return False
        header_b64, _, sig_b64 = jws.split(".", 2)
        try:
            header = json.loads(_b64u_decode(header_b64))
        except (ValueError, json.JSONDecodeError):
            return False
        if header.get("alg") != ALG:
            return False
        kid = header.get("kid") or (method.split("#", 1)[1] if "#" in method else None)
        jwk = keys_by_kid.get(kid)
        if jwk is None:
            return False
        try:
            pub = _public_key_from_jwk(jwk)
            signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
            pub.verify(_b64u_decode(sig_b64), signing_input)
        except (InvalidSignature, ValueError, TypeError, KeyError, binascii.Error):
            return False
    return True


__all__ = [
    "ALG",
    "WELL_KNOWN_KEYS_PATH",
    "generate_private_key",
    "jcs",
    "jwk_set",
    "jwk_set_json",
    "jwk_thumbprint",
    "load_private_key",
    "private_key_to_pem",
    "public_jwk",
    "sign_profile",
    "signing_preimage",
    "verify_signature",
]
