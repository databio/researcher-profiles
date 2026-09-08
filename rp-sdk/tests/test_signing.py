"""The `key_signature` proof: offline, central-service-free authenticity.

A published profile can be verified by any third party with a stock JOSE/crypto
dependency and no network round-trip. That is the property ORCID and Scholar
cannot offer, and the enabler for "profiles are URLs." These tests pin the two
things independent implementations must agree on byte-for-byte: the JCS
canonicalization and the detached-JWS pre-image (profile minus `proof`).
"""

import json

import pytest

from researcher_profiles.cli.auth import signing
from researcher_profiles.schema import ProfileDocument, Proof

ORCID = "0000-0002-1825-0097"
BASE = "https://jane.example.org/profile"


def _profile() -> dict:
    return {
        "@context": "https://profiles.databio.org/context/v1.jsonld",
        "@id": BASE,
        "@type": "Person",
        "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
        "name": "Jane Doe",
        "rid": ORCID,
        "provenance": "self_published",
        "url": BASE,
    }


# ---------------------------------------------------------------------------
# JCS canonicalization
# ---------------------------------------------------------------------------


def test_jcs_sorts_keys_and_strips_whitespace():
    assert signing.jcs({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert signing.jcs([1, "x", True, None]) == b'[1,"x",true,null]'


def test_jcs_is_stable_regardless_of_input_key_order():
    a = signing.jcs({"name": "Jane", "rid": ORCID, "nested": {"z": 1, "a": 2}})
    b = signing.jcs({"nested": {"a": 2, "z": 1}, "rid": ORCID, "name": "Jane"})
    assert a == b


def test_jcs_refuses_floats():
    with pytest.raises(TypeError, match="floats"):
        signing.jcs({"x": 1.5})


def test_preimage_excludes_the_proof_member():
    prof = _profile()
    with_proof = {**prof, "proof": [{"kind": "orcid_roundtrip", "issuer": "x"}]}
    assert signing.signing_preimage(prof) == signing.signing_preimage(with_proof)


# ---------------------------------------------------------------------------
# Sign / verify round-trip
# ---------------------------------------------------------------------------


def test_sign_then_verify_round_trips():
    key = signing.generate_private_key()
    proof = signing.sign_profile(_profile(), key, base_url=BASE)
    signed = {**_profile(), "proof": [proof]}
    jwks = signing.jwk_set([signing.public_jwk(key)])
    assert signing.verify_signature(signed, jwks) is True


def test_verification_method_names_the_exact_key():
    key = signing.generate_private_key()
    proof = signing.sign_profile(_profile(), key, base_url=BASE)
    kid = signing.public_jwk(key)["kid"]
    assert proof["verificationMethod"] == (f"{BASE}/.well-known/researcher-profile-keys.json#{kid}")
    assert proof["alg"] == "EdDSA"


def test_tampering_breaks_the_signature():
    key = signing.generate_private_key()
    proof = signing.sign_profile(_profile(), key, base_url=BASE)
    jwks = signing.jwk_set([signing.public_jwk(key)])
    tampered = {**_profile(), "name": "Mallory", "proof": [proof]}
    assert signing.verify_signature(tampered, jwks) is False


def test_a_different_key_does_not_verify():
    key = signing.generate_private_key()
    proof = signing.sign_profile(_profile(), key, base_url=BASE)
    signed = {**_profile(), "proof": [proof]}
    other = signing.jwk_set([signing.public_jwk(signing.generate_private_key())])
    assert signing.verify_signature(signed, other) is False


def test_no_signature_proof_is_not_trusted():
    """A profile with no key_signature proof returns False, never raising."""
    key = signing.generate_private_key()
    jwks = signing.jwk_set([signing.public_jwk(key)])
    assert signing.verify_signature(_profile(), jwks) is False


def test_extra_non_signature_proofs_do_not_disturb_the_signature():
    key = signing.generate_private_key()
    proof = signing.sign_profile(_profile(), key, base_url=BASE)
    jwks = signing.jwk_set([signing.public_jwk(key)])
    signed = {
        **_profile(),
        "proof": [
            proof,
            {"kind": "orcid_roundtrip", "issuer": f"https://orcid.org/{ORCID}"},
        ],
    }
    assert signing.verify_signature(signed, jwks) is True


def test_thumbprint_kid_is_deterministic():
    key = signing.generate_private_key()
    assert signing.public_jwk(key)["kid"] == signing.public_jwk(key)["kid"]


def test_pem_round_trip():
    key = signing.generate_private_key()
    pem = signing.private_key_to_pem(key)
    reloaded = signing.load_private_key(pem)
    assert signing.public_jwk(reloaded) == signing.public_jwk(key)


# ---------------------------------------------------------------------------
# A signed profile still validates as a ProfileDocument
# ---------------------------------------------------------------------------


def test_signed_document_is_a_valid_profile_document():
    key = signing.generate_private_key()
    # Sign the CANONICAL PUBLISHED serialization, not a pre-validation dict:
    # model validation fills defaults (e.g. `level`), so a signature must cover
    # the exact bytes a verifier will reconstruct. Loading + re-serializing is
    # then a no-op, so the signature survives the round-trip.
    doc = ProfileDocument.model_validate(_profile()).model_dump(mode="json")
    proof = signing.sign_profile(doc, key, base_url=BASE)
    doc["proof"] = [proof]
    model = ProfileDocument.model_validate(doc)
    assert len(model.proof) == 1
    assert model.proof[0].kind == "key_signature"
    jwks = signing.jwk_set([signing.public_jwk(key)])
    assert signing.verify_signature(model.model_dump(mode="json"), jwks) is True


# ---------------------------------------------------------------------------
# Proof envelope validation
# ---------------------------------------------------------------------------


def test_unknown_proof_kind_is_tolerated():
    """A consumer must ignore a proof kind it does not understand."""
    p = Proof.model_validate({"kind": "future_scheme_2030", "issuer": "whoever"})
    assert p.kind == "future_scheme_2030"


def test_key_signature_requires_its_members():
    with pytest.raises(Exception, match="key_signature"):
        Proof.model_validate({"kind": "key_signature", "alg": "EdDSA"})


def test_domain_wellknown_requires_issuer_and_challenge():
    with pytest.raises(Exception, match="domain_wellknown"):
        Proof.model_validate({"kind": "domain_wellknown", "issuer": "example.org"})
    ok = Proof.model_validate(
        {
            "kind": "domain_wellknown",
            "issuer": "example.org",
            "challenge": "https://example.org/.well-known/rp-challenge.txt",
            "token": "abc123",
        }
    )
    assert ok.token == "abc123"


def test_orcid_roundtrip_requires_an_issuer():
    with pytest.raises(Exception, match="orcid_roundtrip"):
        Proof.model_validate({"kind": "orcid_roundtrip"})


# ---------------------------------------------------------------------------
# CLI: sign / sign-verify
# ---------------------------------------------------------------------------


def test_cli_sign_and_verify(tmp_path):
    from researcher_profiles.cli import main

    (tmp_path / "profile.jsonld").write_text(json.dumps(_profile()), encoding="utf-8")

    assert main(["sign", str(tmp_path)]) == 0
    assert (tmp_path / ".well-known" / "researcher-profile-keys.json").is_file()
    assert (tmp_path / ".keys" / "signing.pem").is_file()
    assert main(["sign-verify", str(tmp_path)]) == 0

    # Tampering makes verification exit non-zero.
    doc = json.loads((tmp_path / "profile.jsonld").read_text())
    doc["name"] = "Mallory"
    (tmp_path / "profile.jsonld").write_text(json.dumps(doc), encoding="utf-8")
    assert main(["sign-verify", str(tmp_path)]) == 1


def test_cli_sign_preserves_other_proofs_and_re_signs_cleanly(tmp_path):
    from researcher_profiles.cli import main

    doc = {
        **_profile(),
        "proof": [{"kind": "orcid_roundtrip", "issuer": f"https://orcid.org/{ORCID}"}],
    }
    (tmp_path / "profile.jsonld").write_text(json.dumps(doc), encoding="utf-8")

    assert main(["sign", str(tmp_path)]) == 0
    assert main(["sign", str(tmp_path)]) == 0  # re-sign
    written = json.loads((tmp_path / "profile.jsonld").read_text())
    kinds = [p["kind"] for p in written["proof"]]
    assert kinds.count("key_signature") == 1  # no stale duplicate
    assert "orcid_roundtrip" in kinds
    assert main(["sign-verify", str(tmp_path)]) == 0
