# src/records/signing.py
"""Ed25519 signatures over a participation record (T4) -- the "signed, publicly verifiable" half.

NO NEW KEY, NO NEW SCHEME. This is the SAME operator Ed25519 key and the SAME single code path as
audit/receipts.py, normalize/signing.py and events/invite_signing.py -- the operator holds the
private key on /state (audit.keys.ensure_private_key); anyone with the pinned public key can verify
a record; only the key-holder can sign one. Exactly as normalize.run relates to audit.checkpoint,
the ONLY things that differ here are the signed material and its frozen domain tag. Key handling
(PEM, fingerprint, load) is REUSED verbatim from audit.receipts, so the whole repo keeps ONE Ed25519
implementation.

This signs a participation record: the tuple that NAMES what the deployment attests -- the record
kind, the subject's role, the event, the subject descriptor, the listed items, an issued-at stamp,
and the honest attestation string. Binding all of them means a tampered event, role, subject, item
title, timestamp or attestation fails verification. It attests only that the operator's key signed
those participation facts; it is NOT a measure of merit and NOT fraud detection.

Pure and Django-free (only audit.receipts + audit.hashchain + stdlib), so it unit-tests without a
database and a holder of the exported public key can re-verify a record offline.
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
    "record_material", "sign_record", "verify_record", "RECORD_FIELDS",
]

_SEP = b"\x1e"
# Distinct from every audit tag (payload / row / checkpoint), the normalize run tag, and the invite
# tag, so a record signature can never be replayed as any of those, nor vice versa. Frozen for
# record schema v1.
_RECORD_TAG = b"dogfood.record.v1"

# The record fields that are signed, in a FROZEN order (mirrors receipts.CHECKPOINT_FIELDS /
# normalize.RUN_FIELDS style). Scalars AND structured values (`subject` dict, `items` list) are all
# folded into one canonical pre-image, so any change to any displayed field flips verification.
RECORD_FIELDS = ("kind", "role", "event_ext_id", "event_name",
                 "subject", "items", "issued_at", "attestation")


def record_material(**fields) -> bytes:
    """Domain-tagged canonical bytes signed by / verified against a participation record.

    Exactly the RECORD_FIELDS keys are folded into the pre-image (extra kwargs are ignored, a
    missing key is treated as None) so the material is reproducible from the record's own body and
    total for any caller-supplied JSON.
    """
    body = {k: fields.get(k) for k in RECORD_FIELDS}
    return _RECORD_TAG + _SEP + canonical_bytes(body)


def sign_record(key, **fields) -> str:
    """Hex-encoded Ed25519 signature over the record fields (deterministic per RFC 8032)."""
    return key.sign(record_material(**fields)).hex()


def verify_record(pub, *, signature: str, **fields) -> bool:
    """Verify a record signature; any tampered field, wrong key, or malformed sig returns False."""
    try:
        pub.verify(bytes.fromhex(signature), record_material(**fields))
        return True
    except (InvalidSignature, ValueError):
        return False
