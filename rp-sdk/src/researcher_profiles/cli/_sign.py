"""``rp sign`` and ``rp sign-verify``: the key_signature proof on profile.jsonld.

The two verbs share one handler because they share a document, a key path and a
JWK Set; only the direction differs.
"""

import argparse
import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from ._shared import (
    _EG_SLUG,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    _add_root,
    _resolve_profile_jsonld,
    add_subcommand,
)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    """Register this group's verbs on the root subparser action."""
    p_sign = add_subcommand(
        sub,
        "sign",
        "Sign a profile's profile.jsonld with a key_signature proof "
        "(detached JWS / Ed25519) and emit the well-known JWK Set",
        f"rp sign {_EG_SLUG}",
        "rp sign ./profiles/voss-elena --base-url https://profiles.example.org/voss-elena",
        extra=(
            "The private key defaults to <profile>/.keys/signing.pem and is\n"
            "generated at 0600 if absent; keep it out of the published tree.\n"
        ),
    )
    p_sign.add_argument("profile", help="A profile directory or profile.jsonld, a rid, or a slug")
    _add_root(p_sign, "Profiles root (for a rid/slug)")
    p_sign.add_argument(
        "--key",
        help="Ed25519 private-key PEM. Default: <dir>/.keys/signing.pem, "
        "generated (and kept out of the published tree) if absent",
    )
    p_sign.add_argument(
        "--base-url",
        help="Published base URL of this profile. Default: the document's url. "
        "Used to build verificationMethod (…/.well-known/…#kid)",
    )
    p_sign.add_argument(
        "--gen-key",
        action="store_true",
        help="Generate a fresh signing key even if one already exists",
    )

    p_sverify = add_subcommand(
        sub,
        "sign-verify",
        "Verify the key_signature proof(s) on a profile.jsonld against a JWK Set",
        f"rp sign-verify {_EG_SLUG}",
        "rp sign-verify ./profiles/voss-elena --keys ./keys/researcher-profile-keys.json",
        extra="Exit: 0 when a proof verifies, 1 when none does, 2 when there is\nno JWK Set to check against.\n",
    )
    p_sverify.add_argument(
        "profile", help="A profile directory or profile.jsonld, a rid, or a slug"
    )
    _add_root(p_sverify, "Profiles root (for a rid/slug)")
    p_sverify.add_argument(
        "--keys",
        help="JWK Set path. Default: <dir>/.well-known/researcher-profile-keys.json",
    )


def _cmd_sign(args: argparse.Namespace) -> int:
    """Handle ``sign`` and ``sign-verify`` (the key_signature proof)."""
    import pydantic

    from ..schema import ProfileDocument
    from ..schema.jsonld import canonical_dumps, read_jsonld
    from .auth import signing

    profile_jsonld, prof_dir = _resolve_profile_jsonld(args.profile, args.root)
    if not profile_jsonld.is_file():
        print(f"no profile.jsonld at {profile_jsonld}", file=sys.stderr)
        return EXIT_USAGE
    doc = read_jsonld(profile_jsonld)
    keys_path = prof_dir / signing.WELL_KNOWN_KEYS_PATH

    if args.cmd == "sign-verify":
        jwks_src = Path(args.keys) if args.keys else keys_path
        if not jwks_src.is_file():
            print(f"no JWK Set at {jwks_src}", file=sys.stderr)
            return EXIT_USAGE
        jwks = json.loads(jwks_src.read_text(encoding="utf-8"))
        if signing.verify_signature(doc, jwks):
            print(f"OK: key_signature verified against {jwks_src}")
            return EXIT_OK
        print(f"FAIL: no valid key_signature proof for {profile_jsonld}", file=sys.stderr)
        return EXIT_ERROR

    # args.cmd == "sign"
    base_url = args.base_url or doc.get("url")
    if not base_url:
        print(
            "signing needs a base URL for verificationMethod: pass --base-url or "
            "set the document's url",
            file=sys.stderr,
        )
        return EXIT_USAGE

    key_path = Path(args.key) if args.key else prof_dir / ".keys" / "signing.pem"
    if args.gen_key or not key_path.is_file():
        key = signing.generate_private_key()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(signing.private_key_to_pem(key))
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600: private material
        print(f"generated signing key at {key_path} (keep this out of the published tree)")
    else:
        key = signing.load_private_key(key_path)

    # Normalize to the canonical published serialization before signing, so the
    # signed bytes are exactly what a verifier reconstructs after loading and
    # re-serializing (validation fills defaults like `level`; signing a raw dict
    # would break the moment the document is re-emitted).
    try:
        doc = ProfileDocument.model_validate(doc).model_dump(mode="json")
    except pydantic.ValidationError as e:
        print(f"refusing to sign an invalid profile.jsonld: {e}", file=sys.stderr)
        return EXIT_USAGE
    # One current signature per document: drop any stale key_signature proof.
    existing = [p for p in doc.get("proof", []) if p.get("kind") != "key_signature"]
    doc.pop("proof", None)
    proof = signing.sign_profile(doc, key, base_url=base_url)
    doc["proof"] = existing + [proof]
    profile_jsonld.write_text(canonical_dumps(doc), encoding="utf-8")

    jwks_path = prof_dir / signing.WELL_KNOWN_KEYS_PATH
    jwks_path.parent.mkdir(parents=True, exist_ok=True)
    jwks_path.write_text(signing.jwk_set_json([signing.public_jwk(key)]), encoding="utf-8")

    print(f"signed {profile_jsonld}")
    print(f"  verificationMethod: {proof['verificationMethod']}")
    print(f"  JWK Set: {jwks_path}")
    return EXIT_OK


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "sign": _cmd_sign,
    "sign-verify": _cmd_sign,
}
