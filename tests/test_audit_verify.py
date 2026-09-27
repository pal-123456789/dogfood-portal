# tests/test_audit_verify.py
"""End-to-end tests for the offline bundle verifier (src/audit/verify.py).

DB-free: each test builds a synthetic SIGNED bundle on disk from the pure primitives (hashchain +
receipts), then asserts the verifier accepts a good bundle and rejects every class of tamper --
edited payload, dropped head row, forged checkpoint, and a swapped public key. This exercises the
whole integrity story (canonical chain + Ed25519 checkpoint) without a database or Django. Requires
the `cryptography` wheel.
"""
import json
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from audit import hashchain as H
from audit import receipts as R
from audit import verify as V

_SEED = bytes(range(1, 33))
_INSTANCE = "inst_bundle_0001"
_PAYLOADS = [
    {"event": "genesis"},
    {"judge": "jdg_01", "submission": "prj_07"},
    {"functionality": 5, "quality": 4, "innovation": 3},
]


def _build_bundle(key=None, *, payloads=None):
    """Materialise a valid bundle in a fresh temp dir; return its Path."""
    key = key or Ed25519PrivateKey.from_private_bytes(_SEED)
    payloads = payloads or _PAYLOADS
    d = Path(tempfile.mkdtemp(prefix="bundle_"))

    prev = H.GENESIS_HASH
    rows = []
    for i, payload in enumerate(payloads, 1):
        header = {"schema_version": H.SCHEMA_VERSION, "instance_id": _INSTANCE, "seq": i,
                  "event_type": "test.event", "object_type": "test", "object_id": "obj_%d" % i,
                  "actor_user_id": "usr_1", "actor_membership_id": "mem_1",
                  "occurred_at": "2026-09-27T04:%02d:00+00:00" % i}
        digest, rh = H.chain_link(header=header, payload=payload, prev_hash=prev)
        rows.append(dict(header, prev_hash=prev, row_hash=rh, payload_hash=digest, payload=payload))
        prev = rh

    (d / "audit-prefix.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (d / "public-key.pem").write_bytes(R.public_key_to_pem(key.public_key()))

    head = rows[-1]
    created_at = "2026-09-27T05:00:00+00:00"
    cp = {"schema_version": H.SCHEMA_VERSION, "instance_id": _INSTANCE, "seq": head["seq"],
          "row_hash": head["row_hash"], "created_at": created_at,
          "fingerprint": R.public_fingerprint(key.public_key()),
          "signature": R.sign_checkpoint(key, instance_id=_INSTANCE, seq=head["seq"],
                                          row_hash=head["row_hash"], created_at=created_at)}
    (d / "checkpoint.json").write_text(json.dumps(cp, indent=2), encoding="utf-8")
    return d


def _named_check(checks, name):
    return next((c for c in checks if c[0] == name), None)


def test_valid_bundle_verifies():
    ok, checks = V.verify_bundle(_build_bundle())
    assert ok is True
    assert all(passed for _, passed, _ in checks)

def test_edited_payload_fails_the_chain():
    d = _build_bundle()
    lines = (d / "audit-prefix.jsonl").read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1]); row["payload"] = {"judge": "jdg_01", "submission": "prj_99"}
    lines[1] = json.dumps(row, ensure_ascii=False)
    (d / "audit-prefix.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, checks = V.verify_bundle(d)
    assert ok is False
    assert _named_check(checks, "audit chain recomputes")[1] is False

def test_dropped_head_row_fails_head_match():
    d = _build_bundle()
    lines = (d / "audit-prefix.jsonl").read_text(encoding="utf-8").splitlines()
    (d / "audit-prefix.jsonl").write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    ok, checks = V.verify_bundle(d)
    assert ok is False
    # chain over the shorter prefix still recomputes, but the head no longer matches the signature.
    assert _named_check(checks, "audit chain recomputes")[1] is True
    assert _named_check(checks, "prefix head matches signed checkpoint")[1] is False

def test_forged_checkpoint_seq_fails_signature():
    d = _build_bundle()
    cp = json.loads((d / "checkpoint.json").read_text(encoding="utf-8"))
    cp["seq"] = 99
    (d / "checkpoint.json").write_text(json.dumps(cp), encoding="utf-8")
    ok, checks = V.verify_bundle(d)
    assert ok is False
    assert _named_check(checks, "checkpoint signature valid")[1] is False

def test_swapped_public_key_fails_fingerprint():
    d = _build_bundle()
    other = Ed25519PrivateKey.generate()
    (d / "public-key.pem").write_bytes(R.public_key_to_pem(other.public_key()))
    ok, checks = V.verify_bundle(d)
    assert ok is False
    assert _named_check(checks, "public key fingerprint matches checkpoint")[1] is False

def test_missing_files_fail_cleanly():
    d = Path(tempfile.mkdtemp(prefix="empty_"))
    ok, checks = V.verify_bundle(d)
    assert ok is False and checks[0][1] is False
