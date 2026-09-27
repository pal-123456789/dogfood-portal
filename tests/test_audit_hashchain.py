# tests/test_audit_hashchain.py
"""Golden-vector and tamper-detection tests for the audit hash chain (src/audit/hashchain.py).

DB-free on purpose, exactly like test_smoke.py and test_normalize_engine.py: nothing here is
marked django_db, so CI never pays for a test database. The chain is the crown-jewel integrity
primitive, so beyond the structural properties we PIN the exact canonical bytes and four digests:
if anyone ever changes the canonicalization, the domain tags, or the frozen header field set, a
v1 checkpoint published to judges would silently stop verifying -- these golden vectors turn that
into a loud test failure instead.
"""
from audit import hashchain as H

# --- Frozen golden inputs (must be reproduced byte-for-byte to match the pinned digests) ---
_P0 = {"before": None,
       "after": {"functionality": 5, "quality": 3, "innovation": 1},
       "note": "cafe ☕"}
_H1 = {"schema_version": 1, "instance_id": "inst_test_0001", "seq": 1,
       "event_type": "ballot.revision.create", "object_type": "ballot", "object_id": "blt_0001",
       "actor_user_id": "usr_judge_1", "actor_membership_id": "jdg_01",
       "occurred_at": "2026-09-27T04:00:00+00:00"}
_P1 = {"judge": "jdg_01", "submission": "prj_07"}
_H2 = {"schema_version": 1, "instance_id": "inst_test_0001", "seq": 2,
       "event_type": "assignment.create", "object_type": "assignment", "object_id": "asg_0002",
       "actor_user_id": "usr_org_1", "actor_membership_id": "org_01",
       "occurred_at": "2026-09-27T04:00:01+00:00"}

# --- Pinned golden outputs (computed once, frozen for schema v1) ---
_CANON_P0 = '{"after":{"functionality":5,"innovation":1,"quality":3},"before":null,"note":"cafe ☕"}'
_PH_P0 = "ea6f9730f0443a27e38331b7d13e42d771025e40bec1d564549ca3e32a29b30e"
_RH1 = "50cd486d1e1b0c0b41e67e3876ddf73e8e2e00c9e8f096e267022aa014ad894f"
_PH_P1 = "4cbf5df9955017c5be081449e1cd5732c06381c0a7f61d44545c13c60300c24b"
_RH2 = "d1e2b3e02e203e1a7427112933bcb6a97a17f183ceea9eae6336b45277222543"


def _chain():
    """The canonical two-link chain assembled as it is stored (header + hashes + payload)."""
    d1, rh1 = H.chain_link(header=_H1, payload=_P0, prev_hash=H.GENESIS_HASH)
    d2, rh2 = H.chain_link(header=_H2, payload=_P1, prev_hash=rh1)
    r1 = dict(_H1, prev_hash=H.GENESIS_HASH, row_hash=rh1, payload_hash=d1, payload=_P0)
    r2 = dict(_H2, prev_hash=rh1, row_hash=rh2, payload_hash=d2, payload=_P1)
    return r1, r2


# --- Golden vectors: the exact bytes/digests a v1 checkpoint's verifier must reproduce ---

def test_canonical_bytes_are_frozen():
    assert H.canonical_bytes(_P0).decode("utf-8") == _CANON_P0

def test_payload_and_row_digests_are_frozen():
    assert H.payload_hash(_P0) == _PH_P0
    assert H.row_hash(header=_H1, payload_digest=_PH_P0, prev_hash=H.GENESIS_HASH) == _RH1
    assert H.payload_hash(_P1) == _PH_P1
    assert H.row_hash(header=_H2, payload_digest=_PH_P1, prev_hash=_RH1) == _RH2

def test_domain_separation_tag_is_applied():
    import hashlib
    # The payload tag must actually be mixed in: a bare sha256 of the canonical bytes differs.
    assert H.payload_hash(_P0) != hashlib.sha256(H.canonical_bytes(_P0)).hexdigest()

def test_extra_row_keys_do_not_change_row_hash():
    # verify_chain passes the whole stored row as `header`; extra keys must be ignored.
    noisy = dict(_H1, prev_hash="x", row_hash="y", payload=_P0, junk=123)
    assert H.row_hash(header=noisy, payload_digest=_PH_P0, prev_hash=H.GENESIS_HASH) == _RH1


# --- Whole-chain verification and tamper detection ---

def test_valid_chain_and_empty_chain_verify():
    assert H.verify_chain(list(_chain())) == (True, None, "ok")
    assert H.verify_chain([]) == (True, None, "ok")

def test_payload_edit_is_detected():
    # Editing the payload while leaving the stored payload_hash/row_hash is caught at seq 1
    # (here by the payload_hash cross-check, which fires just before the row_hash recompute).
    r1, r2 = _chain()
    r1 = dict(r1, payload=dict(_P0, note="tampered"))
    assert H.verify_chain([r1, r2])[:2] == (False, 1)

def test_payload_edit_with_recomputed_hash_still_breaks_row_hash():
    # A cleverer tamper: edit the payload AND fix payload_hash, but the stored row_hash is now
    # stale -- the row_hash recompute must still catch it. (Fixing row_hash too only pushes the
    # break to seq 2's prev_hash link, which test_prev_hash_break_is_detected covers.)
    r1, r2 = _chain()
    tampered = dict(_P0, note="tampered")
    r1 = dict(r1, payload=tampered, payload_hash=H.payload_hash(tampered))
    ok, seq, reason = H.verify_chain([r1, r2])
    assert (ok, seq) == (False, 1) and "row_hash does not match recomputation" in reason

def test_header_field_edit_is_detected():
    r1, r2 = _chain()
    r1 = dict(r1, actor_membership_id="jdg_99")  # stored row_hash no longer matches
    assert H.verify_chain([r1, r2])[:2] == (False, 1)

def test_reordering_is_detected():
    r1, r2 = _chain()
    assert H.verify_chain([r2, r1])[:2] == (False, 2)

def test_prev_hash_break_is_detected():
    r1, r2 = _chain()
    r2 = dict(r2, prev_hash=H.GENESIS_HASH)  # correct seq, wrong link
    ok, seq, reason = H.verify_chain([r1, r2])
    assert (ok, seq) == (False, 2) and "prev_hash" in reason

def test_declared_payload_hash_must_match_payload():
    r1, r2 = _chain()
    r1 = dict(r1, payload_hash="0" * 64)
    ok, seq, reason = H.verify_chain([r1, r2])
    assert (ok, seq) == (False, 1) and "payload_hash does not match" in reason

def test_row_without_payload_or_hash_is_rejected():
    r1, r2 = _chain()
    r1 = {k: v for k, v in r1.items() if k not in ("payload", "payload_hash")}
    assert H.verify_chain([r1, r2])[:2] == (False, 1)

def test_unsupported_schema_version_is_rejected():
    r1, r2 = _chain()
    r1 = dict(r1, schema_version=2)
    assert H.verify_chain([r1, r2])[:2] == (False, 1)

def test_missing_header_field_is_reported_not_raised():
    r1, r2 = _chain()
    r1 = {k: v for k, v in r1.items() if k != "occurred_at"}
    ok, seq, reason = H.verify_chain([r1, r2])
    assert (ok, seq) == (False, 1) and "header field" in reason
