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


def test_jcs_sorts_keys_and_strips_whitespace():
    assert signing.jcs({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert signing.jcs([1, "x", True, None]) == b'[1,"x",true,null]'


def test_jcs_is_stable_regardless_of_input_key_order():
    a = signing.jcs({"name": "Jane", "rid": ORCID, "nested": {"z": 1, "a": 2}})
    b = signing.jcs({"nested": {"a": 2, "z": 1}, "rid": ORCID, "name": "Jane"})
    assert a == b


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # RFC 8785 appendix B vectors, then the shapes interest weights take.
        (1e30, b"1e+30"),
        (4.5, b"4.5"),
        (0.002, b"0.002"),
        (1e-27, b"1e-27"),
        (333333333.33333329, b"333333333.3333333"),
        (-0.0, b"0"),
        (1e21, b"1e+21"),
        (1e20, b"100000000000000000000"),
        (1e-7, b"1e-7"),
        (0.000001, b"0.000001"),
        (-1.0, b"-1"),
        (0.45, b"0.45"),
    ],
    ids=[
        "1e30",
        "4.50",
        "2e-3",
        "1e-27",
        "shortest-digits",
        "negative-zero",
        "1e21-switches-to-exponent",
        "1e20-stays-fixed",
        "1e-7-switches-to-exponent",
        "1e-6-stays-fixed",
        "integral-weight",
        "share",
    ],
)
def test_jcs_writes_floats_as_es6_does(value, expected):
    assert signing.jcs(value) == expected


def test_jcs_refuses_nan():
    with pytest.raises(TypeError, match="NaN"):
        signing.jcs({"x": float("nan")})


def test_preimage_excludes_the_proof_member():
    prof = _profile()
    with_proof = {**prof, "proof": [{"kind": "orcid_roundtrip", "issuer": "x"}]}
    assert signing.signing_preimage(prof) == signing.signing_preimage(with_proof)


@pytest.fixture
def key():
    return signing.generate_private_key()


def _jwks(key):
    return signing.jwk_set([signing.public_jwk(key)])


@pytest.mark.parametrize(
    ("over", "other_key", "signed", "expected"),
    [
        ({}, False, True, True),
        (
            {"proof_extra": [{"kind": "orcid_roundtrip", "issuer": f"https://orcid.org/{ORCID}"}]},
            False,
            True,
            True,
        ),
        ({"name": "Mallory"}, False, True, False),
        ({}, True, True, False),
        ({}, False, False, False),
    ],
    ids=[
        "round-trip",
        "extra-non-signature-proof",
        "tampered-name",
        "other-key",
        "no-signature-proof",
    ],
)
def test_verify_signature(key, over, other_key, signed, expected):
    """No key_signature proof returns False, never raising."""
    over = dict(over)
    extra = over.pop("proof_extra", [])
    doc = _profile()
    if signed:
        proof = signing.sign_profile(doc, key, base_url=BASE)
        doc = {**doc, **over, "proof": [proof, *extra]}
    jwks = _jwks(signing.generate_private_key() if other_key else key)
    assert signing.verify_signature(doc, jwks) is expected


def test_verification_method_names_the_exact_key(key):
    proof = signing.sign_profile(_profile(), key, base_url=BASE)
    kid = signing.public_jwk(key)["kid"]
    assert proof["verificationMethod"] == (f"{BASE}/.well-known/researcher-profile-keys.json#{kid}")
    assert proof["alg"] == "EdDSA"


def test_thumbprint_kid_is_deterministic(key):
    assert signing.public_jwk(key)["kid"] == signing.public_jwk(key)["kid"]


def test_pem_round_trip(key):
    pem = signing.private_key_to_pem(key)
    reloaded = signing.load_private_key(pem)
    assert signing.public_jwk(reloaded) == signing.public_jwk(key)


def test_signed_document_is_a_valid_profile_document(key):
    # sign the canonical serialization; validation fills defaults
    doc = ProfileDocument.model_validate(_profile()).model_dump(mode="json")
    proof = signing.sign_profile(doc, key, base_url=BASE)
    doc["proof"] = [proof]
    model = ProfileDocument.model_validate(doc)
    assert len(model.proof) == 1
    assert model.proof[0].kind == "key_signature"
    jwks = _jwks(key)
    assert signing.verify_signature(model.model_dump(mode="json"), jwks) is True


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({"kind": "key_signature", "alg": "EdDSA"}, "key_signature"),
        ({"kind": "domain_wellknown", "issuer": "example.org"}, "domain_wellknown"),
        ({"kind": "orcid_roundtrip"}, "orcid_roundtrip"),
    ],
)
def test_proof_missing_members_is_refused(payload, match):
    with pytest.raises(Exception, match=match):
        Proof.model_validate(payload)


@pytest.mark.parametrize(
    ("payload", "attr", "value"),
    [
        # a consumer must ignore a proof kind it does not understand
        ({"kind": "future_scheme_2030", "issuer": "whoever"}, "kind", "future_scheme_2030"),
        (
            {
                "kind": "domain_wellknown",
                "issuer": "example.org",
                "challenge": "https://example.org/.well-known/rp-challenge.txt",
                "token": "abc123",
            },
            "token",
            "abc123",
        ),
    ],
    ids=["unknown-kind", "domain-wellknown"],
)
def test_proof_accepted(payload, attr, value):
    assert getattr(Proof.model_validate(payload), attr) == value


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
