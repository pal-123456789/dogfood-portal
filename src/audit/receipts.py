# src/audit/receipts.py
"""Ed25519 signed checkpoints for the audit chain -- the operator-evidence layer.

Why Ed25519 and not the Django SECRET_KEY / HMAC: a checkpoint is meant to be verified by an
INDEPENDENT party (a judge, an auditor) who must NOT be able to forge one. HMAC verification needs
the same secret that signs, so anyone who can check a receipt can also mint one. An Ed25519 public
key lets anyone verify while only the private-key holder can sign. The public key + its fingerprint
are exported and pinned BEFORE judging; the private key lives on the operator's protected /state
volume.

Honest scope (see docs/THREAT-MODEL.md): the operator holds the private key, so signatures do not
bind the operator against themselves -- a key-holder can even sign two conflicting histories
(equivocation). What a checkpoint buys is that a value which left the operator's control at time T,
retained by an independent party, pins the chain as it was at T: any later rewrite fails to match
the retained checkpoint. That is retrospective, comparison-based evidence, not prevention.

Pure and Django-free: keys and fields are passed in, so this unit-tests without a database or
settings. The thin wiring (where the PEM lives, env gating) belongs to the model/command that
calls this. cryptography ships a manylinux wheel, so it adds no gcc to the image.
"""
from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.exceptions import InvalidSignature

from .hashchain import SCHEMA_VERSION, canonical_bytes

_SEP = b"\x1e"
# Distinct from the payload/row tags so a checkpoint signature can never be replayed as a row or
# payload pre-image. Frozen for schema v1.
_CHECKPOINT_TAG = b"dogfood.audit.checkpoint.v1"

# The checkpoint fields that are signed, in a FROZEN set (mirrors hashchain.HEADER_FIELDS style).
CHECKPOINT_FIELDS = ("schema_version", "instance_id", "seq", "row_hash", "created_at")


def generate_private_key() -> Ed25519PrivateKey:
    """A fresh Ed25519 private key (bootstrap when the /state key file is absent)."""
    return Ed25519PrivateKey.generate()


def private_key_to_pem(key: Ed25519PrivateKey, *, password: bytes | None = None) -> bytes:
    """PKCS8 PEM for on-disk storage; pass a password to encrypt at rest (else unencrypted)."""
    enc = (serialization.BestAvailableEncryption(password) if password
           else serialization.NoEncryption())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=enc,
    )


def private_key_from_pem(pem: bytes, *, password: bytes | None = None) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem, password=password)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("not an Ed25519 private key")
    return key


def public_key_to_pem(pub: Ed25519PublicKey) -> bytes:
    """SubjectPublicKeyInfo PEM -- this is what ships in the bundle as public-key.pem."""
    return pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def public_key_from_pem(pem: bytes) -> Ed25519PublicKey:
    pub = serialization.load_pem_public_key(pem)
    if not isinstance(pub, Ed25519PublicKey):
        raise ValueError("not an Ed25519 public key")
    return pub


def public_fingerprint(pub: Ed25519PublicKey) -> str:
    """sha256 hex over the 32 raw public-key bytes -- the value pinned before judging."""
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()


def checkpoint_material(*, instance_id: str, seq: int, row_hash: str, created_at: str) -> bytes:
    """Domain-tagged canonical bytes signed by / verified against a checkpoint."""
    body = {"schema_version": SCHEMA_VERSION, "instance_id": instance_id,
            "seq": seq, "row_hash": row_hash, "created_at": created_at}
    return _CHECKPOINT_TAG + _SEP + canonical_bytes(body)


def sign_checkpoint(key: Ed25519PrivateKey, *, instance_id: str, seq: int,
                    row_hash: str, created_at: str) -> str:
    """Hex-encoded Ed25519 signature over the checkpoint fields (deterministic per RFC 8032)."""
    sig = key.sign(checkpoint_material(
        instance_id=instance_id, seq=seq, row_hash=row_hash, created_at=created_at))
    return sig.hex()


def verify_checkpoint(pub: Ed25519PublicKey, *, instance_id: str, seq: int,
                      row_hash: str, created_at: str, signature: str) -> bool:
    """Constant-time-ish verify; any tampered field or wrong key returns False (never raises)."""
    try:
        pub.verify(bytes.fromhex(signature), checkpoint_material(
            instance_id=instance_id, seq=seq, row_hash=row_hash, created_at=created_at))
        return True
    except (InvalidSignature, ValueError):
        return False
