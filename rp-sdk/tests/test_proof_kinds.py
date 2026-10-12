"""The ``orcid_login`` proof kind: its members, the document rule tying it to ``rid``,
the strip helper every store uses, and the exported JSON Schema agreeing with the validator.
"""

import json

import jsonschema
import pytest

from researcher_profiles.schema import (
    KNOWN_PROOF_KINDS,
    REGISTRY_ISSUED_PROOF_KINDS,
    ProfileDocument,
    Proof,
    strip_registry_issued_proofs,
)
from researcher_profiles.schema_export import export_schemas

ORCID = "0000-0002-1825-0097"
OTHER_ORCID = "0000-0001-5109-3700"
ISSUER = "https://profiles.example.org"
WHEN = "2026-09-01T12:00:00+00:00"


def _login(**overrides) -> dict:
    return {
        "kind": "orcid_login",
        "issuer": ISSUER,
        "orcid": ORCID,
        "verifiedAt": WHEN,
        **overrides,
    }


def _doc(rid: str = ORCID, proof: list | None = None) -> dict:
    return {
        "@context": "https://profiles.databio.org/context/v1.jsonld",
        "@type": "Person",
        "conformsTo": "https://profiles.databio.org/context/v1.jsonld",
        "name": "Jane Doe",
        "rid": rid,
        "provenance": "self_published",
        "proof": proof or [],
    }


class TestOrcidLoginProof:
    def test_complete_proof_validates(self):
        p = Proof.model_validate(_login())
        assert (p.kind, p.issuer, p.orcid, p.verified_at) == ("orcid_login", ISSUER, ORCID, WHEN)
        assert "orcid_login" in KNOWN_PROOF_KINDS
        assert "orcid_login" in REGISTRY_ISSUED_PROOF_KINDS

    @pytest.mark.parametrize("member", ["issuer", "orcid", "verifiedAt"])
    def test_each_member_is_required(self, member):
        data = _login()
        del data[member]
        with pytest.raises(Exception, match="orcid_login"):
            Proof.model_validate(data)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"issuer": "profiles.example.org"},
            {"issuer": "ftp://profiles.example.org"},
            {"orcid": "0000-0002-1825-0098"},
            {"orcid": f"https://orcid.org/{ORCID}"},
            {"orcid": "local:jane-doe-a3f19c"},
            {"verifiedAt": "yesterday"},
        ],
    )
    def test_malformed_members_are_rejected(self, overrides):
        with pytest.raises(Exception, match="orcid_login"):
            Proof.model_validate(_login(**overrides))

    def test_unknown_kind_still_loads_untouched(self):
        p = Proof.model_validate({"kind": "future_scheme_2030", "issuer": "whoever", "x": 1})
        assert p.kind == "future_scheme_2030"
        assert p.model_dump(mode="json")["x"] == 1


class TestDocumentRule:
    def test_matching_orcid_loads(self):
        doc = ProfileDocument.model_validate(_doc(proof=[_login()]))
        assert doc.proof[0].orcid == ORCID

    @pytest.mark.parametrize(
        "doc_kwargs,match",
        [
            ({"proof": [_login(orcid=OTHER_ORCID)]}, "orcid_login proof names ORCID iD"),
            (
                {"rid": "local:jane-doe-a3f19c", "proof": [_login()]},
                "orcid_login proof names ORCID iD",
            ),
            ({"proof": [_login(), _login()]}, "at most one orcid_login"),
        ],
        ids=["other-orcid", "local-rid", "two-proofs"],
    )
    def test_document_rule_fails(self, doc_kwargs, match):
        with pytest.raises(Exception, match=match):
            ProfileDocument.model_validate(_doc(**doc_kwargs))


class TestStrip:
    ROUNDTRIP = {"kind": "orcid_roundtrip", "issuer": f"https://orcid.org/{ORCID}"}
    SIGNATURE = {
        "kind": "key_signature",
        "verificationMethod": "https://jane.example.org/jwks.json#k1",
        "alg": "EdDSA",
        "signatureValue": "abc..def",
    }

    @pytest.mark.parametrize(
        "wrap,kind_of",
        [(lambda p: p, lambda p: p["kind"]), (Proof.model_validate, lambda p: p.kind)],
        ids=["dicts", "models"],
    )
    def test_strips_registry_issued(self, wrap, kind_of):
        kept = strip_registry_issued_proofs(
            [wrap(p) for p in (self.SIGNATURE, _login(), self.ROUNDTRIP)]
        )
        assert [kind_of(p) for p in kept] == ["key_signature", "orcid_roundtrip"]

    def test_input_not_mutated(self):
        proofs = [_login()]
        assert strip_registry_issued_proofs(proofs) == []
        assert proofs == [_login()]


@pytest.fixture(scope="module")
def proof_schema(tmp_path_factory):
    out = tmp_path_factory.mktemp("schemas")
    export_schemas(out)
    full = json.loads((out / "profile_jsonld.schema.json").read_text())
    defs = full.get("$defs") or full.get("definitions")
    return {**defs["Proof"], "$defs": defs}


class TestExportedSchema:
    def test_complete_orcid_login_accepted(self, proof_schema):
        jsonschema.validate(_login(), proof_schema)

    @pytest.mark.parametrize(
        "base,drop_key",
        [(_login(), "orcid"), (TestStrip.SIGNATURE, "signatureValue")],
        ids=["orcid_login-without-orcid", "key_signature-without-value"],
    )
    def test_missing_member_rejected(self, proof_schema, base, drop_key):
        data = {k: v for k, v in base.items() if k != drop_key}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(data, proof_schema)
