# src/audit/hashchain.py
"""Pure, Django-free primitives for the append-only audit chain (schema v1).

Deterministic and stdlib-only, so it unit-tests without a database (mirroring the tests/
convention) and is reused by the ORM model, the `audit_verify` command, and the signed
checkpoint bundle without importing any of them.

The chain is a singly-linked hash chain. Each event's row_hash binds a canonical HEADER
(schema_version, instance_id, seq, event_type, object_type, object_id, actor ids, occurred_at)
to the hash of its canonical payload and to the previous row's row_hash. Recomputing the chain
and comparing row_hashes detects any insertion, deletion, reordering, or field edit *within the
stored rows*.

What an in-database chain cannot do by itself is bind an operator who holds the DB password, the
filesystem, and the signing key: they can rewrite every row and recompute every hash. The signed
checkpoint (see audit/receipts.py -- Ed25519, publicly verifiable) exists for exactly that gap:
a checkpoint that leaves the operator's control before a disputed change, then retained and
compared by an independent party, pins the chain as it was at that time. See docs/THREAT-MODEL.md
for the honest scope. Nothing stored only on the operator's machine is tamper-evident against
that operator.

Two domain-separation tags keep the payload-hash and row-hash pre-image spaces disjoint, and the
v1 suffix lets a future schema change the field set without colliding with v1 digests. The header
field set and both tags are FROZEN for v1; golden vectors in tests/test_audit_hashchain.py lock
the exact byte output.
"""
from __future__ import annotations

import hashlib
import json

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64            # prev_hash of the first event
_SEP = b"\x1e"                     # ASCII record separator: cannot occur in hex/JSON scalars
_HASH = hashlib.sha256

# Domain-separation tags. Distinct pre-images mean a payload digest can never equal a row digest,
# and a future v2 chain (different tag) can never collide with a v1 chain. Frozen for schema v1.
_PAYLOAD_TAG = b"dogfood.audit.payload.v1"
_ROW_TAG = b"dogfood.audit.row.v1"

# The canonical header fields, in a FROZEN set. row_hash hashes exactly these keys: extra keys on
# a row are ignored, a missing key raises. Changing this tuple is a v2 schema change.
HEADER_FIELDS = (
    "schema_version", "instance_id", "seq", "event_type", "object_type",
    "object_id", "actor_user_id", "actor_membership_id", "occurred_at",
)


def canonical_bytes(payload) -> bytes:
    """Deterministic UTF-8 serialization of a payload (sorted keys, no whitespace).

    Two structurally-equal payloads always serialize identically, so digests are stable across
    processes and runs. NaN/Inf and non-JSON values raise, keeping the digest total.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def payload_hash(payload) -> str:
    """sha256 hex of the domain-tagged canonical payload bytes."""
    return _HASH(_PAYLOAD_TAG + _SEP + canonical_bytes(payload)).hexdigest()


def _canonical_header(header) -> bytes:
    """Canonical bytes of exactly the FROZEN header fields; a missing key raises KeyError."""
    return canonical_bytes({k: header[k] for k in HEADER_FIELDS})


def row_hash(*, header, payload_digest: str, prev_hash: str) -> str:
    """sha256 hex binding the canonical header, the payload digest, and the predecessor.

    The parts are joined with a separator absent from the hex/JSON inputs, so distinct
    (header, payload, prev) tuples cannot collide through concatenation ambiguity.
    """
    material = _SEP.join((
        _ROW_TAG, _canonical_header(header),
        payload_digest.encode("utf-8"), prev_hash.encode("utf-8"),
    ))
    return _HASH(material).hexdigest()


def chain_link(*, header, payload, prev_hash: str) -> tuple[str, str]:
    """Compute (payload_digest, row_hash) for a new event appended after prev_hash."""
    digest = payload_hash(payload)
    return digest, row_hash(header=header, payload_digest=digest, prev_hash=prev_hash)


def verify_chain(rows) -> tuple[bool, "int | None", str]:
    """Recompute the chain over `rows` and report the first break.

    Each row is a mapping carrying the HEADER_FIELDS plus prev_hash, row_hash, and either payload
    or payload_hash. Returns (ok, broken_seq_or_None, reason). An empty chain is valid. Checks in
    order: schema_version supported; seq starts at 1 and increments by 1; prev_hash links to the
    prior row_hash (GENESIS for the first); payload_hash matches payload when a payload is present;
    row_hash matches the recomputation.
    """
    expected_prev = GENESIS_HASH
    expected_seq = 1
    for row in rows:
        seq = row.get("seq")
        if row.get("schema_version") != SCHEMA_VERSION:
            return False, seq, "unsupported schema_version: %r" % (row.get("schema_version"),)
        if seq != expected_seq:
            return False, seq, "seq gap: expected %d, got %r" % (expected_seq, seq)
        if row.get("prev_hash") != expected_prev:
            return False, seq, "prev_hash does not link to the previous row"
        digest = row.get("payload_hash")
        if "payload" in row:
            recomputed = payload_hash(row["payload"])
            if digest is not None and digest != recomputed:
                return False, seq, "payload_hash does not match payload"
            digest = recomputed
        if digest is None:
            return False, seq, "row missing both payload and payload_hash"
        try:
            rh = row_hash(header=row, payload_digest=digest, prev_hash=row["prev_hash"])
        except KeyError as exc:
            return False, seq, "row missing header field %s" % exc
        if rh != row.get("row_hash"):
            return False, seq, "row_hash does not match recomputation"
        expected_prev = row["row_hash"]
        expected_seq += 1
    return True, None, "ok"
