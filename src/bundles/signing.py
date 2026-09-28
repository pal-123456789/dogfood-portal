# src/bundles/signing.py
"""Ed25519 signature over a portable event bundle (T4) -- SAME key, SAME code path.

NO NEW KEY, NO NEW SCHEME. This reuses the one operator Ed25519 key on /state
(audit.keys.ensure_private_key) and the single Ed25519 implementation in audit.receipts -- exactly
as records.signing, normalize.signing and events.invite_signing do. The ONLY things that differ
here are the signed material (a whole event-bundle dict) and its frozen domain tag
(dogfood.bundle.v1), so a bundle signature can never be replayed as any other domain, nor vice
versa. Key handling (PEM, fingerprint, load) is REUSED verbatim from audit.receipts, so the whole
repo keeps ONE Ed25519 implementation.

Honest scope: a bundle signature attests only that the operator's key signed THIS exported
structure at export time. It is NOT proof of results integrity and NOT a measure of merit; it binds
the exported bytes to the operator's key, nothing more. Because the operator holds the private key,
it is decisive to a third party only if they pinned the public key + fingerprint beforehand
(docs/THREAT-MODEL.md).

Pure and Django-free (only audit.receipts + audit.hashchain + stdlib), so it unit-tests without a
database and a holder of the exported public key can re-verify a bundle offline.
"""
from __future__ import annotations

from cryptography.exceptions import InvalidSignature

from audit.hashchain import canonical_bytes
from audit.receipts import (  # one Ed25519 code path for the whole repo
    generate_private_key,
    private_key_from_pem,
    private_key_to_pem,
    public_fingerprint,
    public_key_from_pem,
    public_key_to_pem,
)

__all__ = [
    "generate_private_key", "private_key_from_pem", "private_key_to_pem",
    "public_fingerprint", "public_key_from_pem", "public_key_to_pem",
    "bundle_material", "sign_bundle", "verify_bundle_sig",
]

_SEP = b"\x1e"
# Distinct from every audit tag (payload / row / checkpoint), the normalize run tag, the record tag
# and the invite tag, so a bundle signature can never be replayed as any of those, nor vice versa.
# Frozen for bundle schema v1.
_BUNDLE_TAG = b"dogfood.bundle.v1"


def bundle_material(bundle_dict) -> bytes:
    """Domain-tagged canonical bytes signed by / verified against a bundle.

    The WHOLE bundle dict is folded into one canonical pre-image (sorted keys, no whitespace), so
    any change to any exported field -- an event name, a submission title, a track id -- flips
    verification. NaN/Inf or a non-JSON value raises (canonical_bytes is total), never signs.
    """
    return _BUNDLE_TAG + _SEP + canonical_bytes(bundle_dict)


def sign_bundle(key, bundle_dict) -> str:
    """Hex-encoded Ed25519 signature over the bundle (deterministic per RFC 8032)."""
    return key.sign(bundle_material(bundle_dict)).hex()


def verify_bundle_sig(pub, *, signature: str, bundle_dict) -> bool:
    """Verify a bundle signature; any tampered field, wrong key, or malformed sig returns False."""
    try:
        pub.verify(bytes.fromhex(signature), bundle_material(bundle_dict))
        return True
    except (InvalidSignature, ValueError):
        return False
