# src/normalize/signing.py
"""Ed25519 signatures over a reproducible normalization run -- P2 of the integrity spine.

Same trust model as audit/receipts.py (the operator holds the private key; anyone with the pinned
public key can verify; only the key-holder can sign; see docs/THREAT-MODEL.md for the honest,
retrospective scope). This module signs a RUN: the tuple that names an engine version, the event,
a run id, and the two hashes that pin a published leaderboard to (a) the exact ballots + weights
that fed it and (b) the exact ranking that came out.

Key handling (PEM, fingerprint, load) is REUSED from audit.receipts so there is a single Ed25519
code path in the repo -- only the signed material and its domain tag differ. Both are pure and
Django-free, so this unit-tests without a DB and normalize.verify imports it to check a bundle
offline (stdlib + numpy + cryptography only).
"""
from __future__ import annotations

import hashlib

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
    "content_hash", "run_material", "sign_run", "verify_run", "RUN_FIELDS",
]

_SEP = b"\x1e"
# Distinct from every audit tag (payload / row / checkpoint) so a run signature can never be
# replayed as an audit checkpoint, nor a checkpoint as a run. Frozen for run schema v1.
_RUN_TAG = b"dogfood.normalize.run.v1"
_HASH = hashlib.sha256

# The run fields that are signed, in a FROZEN order (mirrors receipts.CHECKPOINT_FIELDS style).
RUN_FIELDS = ("engine_version", "instance_id", "event_ext_id", "run_ext_id",
              "inputs_hash", "result_hash", "created_at")


def content_hash(kind: str, obj) -> str:
    """sha256 hex of a domain-tagged canonical object -- used for inputs_hash and result_hash.

    `kind` is a short discriminator ('inputs' / 'result') folded into the pre-image, so an inputs
    blob can never collide with a result blob that happens to canonicalize the same bytes. The run
    signature binds both digests, so signing the two 64-hex strings is equivalent to signing the
    (possibly large) blobs themselves.
    """
    material = _RUN_TAG + _SEP + kind.encode("ascii") + _SEP + canonical_bytes(obj)
    return _HASH(material).hexdigest()


def run_material(*, engine_version: str, instance_id: str, event_ext_id: str, run_ext_id: str,
                 inputs_hash: str, result_hash: str, created_at: str) -> bytes:
    """Domain-tagged canonical bytes signed by / verified against a run."""
    body = {"engine_version": engine_version, "instance_id": instance_id,
            "event_ext_id": event_ext_id, "run_ext_id": run_ext_id,
            "inputs_hash": inputs_hash, "result_hash": result_hash, "created_at": created_at}
    return _RUN_TAG + _SEP + canonical_bytes(body)


def sign_run(key, *, engine_version: str, instance_id: str, event_ext_id: str, run_ext_id: str,
             inputs_hash: str, result_hash: str, created_at: str) -> str:
    """Hex-encoded Ed25519 signature over the run fields (deterministic per RFC 8032)."""
    return key.sign(run_material(
        engine_version=engine_version, instance_id=instance_id, event_ext_id=event_ext_id,
        run_ext_id=run_ext_id, inputs_hash=inputs_hash, result_hash=result_hash,
        created_at=created_at)).hex()


def verify_run(pub, *, signature: str, engine_version: str, instance_id: str, event_ext_id: str,
               run_ext_id: str, inputs_hash: str, result_hash: str, created_at: str) -> bool:
    """Verify a run signature; any tampered field, wrong key, or malformed sig returns False."""
    try:
        pub.verify(bytes.fromhex(signature), run_material(
            engine_version=engine_version, instance_id=instance_id, event_ext_id=event_ext_id,
            run_ext_id=run_ext_id, inputs_hash=inputs_hash, result_hash=result_hash,
            created_at=created_at))
        return True
    except (InvalidSignature, ValueError):
        return False
