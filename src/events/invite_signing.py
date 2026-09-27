# src/events/invite_signing.py
"""Ed25519 signatures over a single-use invitation -- the "signed" half of §20.

Same trust model and single code path as audit/receipts.py and normalize/signing.py: the operator
holds the private key on /state; anyone with the pinned public key can verify an invite; only the
key-holder can mint one. Key handling (PEM, fingerprint, load) is REUSED from audit.receipts so the
whole repo has ONE Ed25519 implementation -- only the signed material and its domain tag differ.

This signs the tuple that NAMES an invitation: the event it joins, the invite's own id, the role it
grants, and its expiry. Binding all four means a tampered role ("participant" -> "organizer" -- not
that organizer is ever offered) or a swapped event fails verification. Single-use is NOT part of the
signature (a signature is replayable by design); it is enforced in the DB at redeem time
(Invite.redeemed_at under select_for_update). Pure and Django-free, so it unit-tests without a DB
and an operator can re-verify an invite offline with the exported public key.
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
    "invite_material", "sign_invite", "verify_invite", "INVITE_FIELDS",
]

_SEP = b"\x1e"
# Distinct from every audit tag (payload / row / checkpoint) and the normalize run tag, so an
# invite signature can never be replayed as any of those, nor vice versa. Frozen for invite v1.
_INVITE_TAG = b"dogfood.invite.v1"

# The invite fields that are signed, in a FROZEN order (mirrors receipts.CHECKPOINT_FIELDS style).
INVITE_FIELDS = ("event_ext_id", "invite_ext_id", "role", "expires_at")


def invite_material(*, event_ext_id: str, invite_ext_id: str, role: str, expires_at: str) -> bytes:
    """Domain-tagged canonical bytes signed by / verified against an invite.

    `expires_at` is the ISO 8601 string, or "" for a never-expiring invite; passing the empty
    string (not None) keeps the pre-image total and reproducible from the stored row.
    """
    body = {"event_ext_id": event_ext_id, "invite_ext_id": invite_ext_id,
            "role": role, "expires_at": expires_at}
    return _INVITE_TAG + _SEP + canonical_bytes(body)


def sign_invite(key, *, event_ext_id: str, invite_ext_id: str, role: str, expires_at: str) -> str:
    """Hex-encoded Ed25519 signature over the invite fields (deterministic per RFC 8032)."""
    return key.sign(invite_material(
        event_ext_id=event_ext_id, invite_ext_id=invite_ext_id,
        role=role, expires_at=expires_at)).hex()


def verify_invite(pub, *, signature: str, event_ext_id: str, invite_ext_id: str,
                  role: str, expires_at: str) -> bool:
    """Verify an invite signature; any tampered field, wrong key, or malformed sig returns False."""
    try:
        pub.verify(bytes.fromhex(signature), invite_material(
            event_ext_id=event_ext_id, invite_ext_id=invite_ext_id,
            role=role, expires_at=expires_at))
        return True
    except (InvalidSignature, ValueError):
        return False
