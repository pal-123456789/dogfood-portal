# tests/test_normalize_signing.py
"""Golden / wire-format and reproducibility tests for the normalization-run signing layer.

DB-free like the rest of tests/. Ed25519 is deterministic (RFC 8032) and its public-key
fingerprint depends only on the key, so a fixed seed reproduces the SAME fingerprint the audit
layer pins -- we assert that here (one shared key path). The run *material* and the content-hash
pre-image are pinned by independent reconstruction, so any change to the domain tag, field set, or
canonicalization is a loud failure. Domain separation is proven directly: a run signature must not
verify as an audit checkpoint, nor a checkpoint signature as a run.
"""
import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from audit import receipts as CP
from audit.hashchain import canonical_bytes
from normalize import engine, signing

# Same fixed 32-byte seed as tests/test_audit_receipts.py -> same public key -> same fingerprint.
_SEED = bytes(range(32))
_GOLDEN_FINGERPRINT = "56475aa75463474c0285df5dbf2bcab73da651358839e9b77481b2eab107708c"
_TAG = b"dogfood.normalize.run.v1"
_SEP = b"\x1e"

_RUN = dict(engine_version=engine.ENGINE_VERSION, instance_id="inst_test_0001",
            event_ext_id="evt_demo", run_ext_id="nrun_0011223344556677",
            inputs_hash="a" * 64, result_hash="b" * 64,
            created_at="2026-09-27T04:05:00+00:00")


def _key():
    return Ed25519PrivateKey.from_private_bytes(_SEED)


def test_fingerprint_matches_shared_key_path():
    # The run signer reuses audit's Ed25519 code path, so the fingerprint is byte-identical.
    assert signing.public_fingerprint(_key().public_key()) == _GOLDEN_FINGERPRINT


def test_sign_then_verify_roundtrips():
    k = _key()
    sig = signing.sign_run(k, **_RUN)
    assert signing.verify_run(k.public_key(), signature=sig, **_RUN) is True


def test_each_tampered_field_fails_verification():
    k = _key()
    pub = k.public_key()
    sig = signing.sign_run(k, **_RUN)
    for field, value in (("engine_version", "ridge-additive-v2"), ("instance_id", "inst_x"),
                         ("event_ext_id", "evt_other"), ("run_ext_id", "nrun_dead"),
                         ("inputs_hash", "c" * 64), ("result_hash", "d" * 64),
                         ("created_at", "2026-01-01T00:00:00+00:00")):
        assert signing.verify_run(pub, signature=sig, **dict(_RUN, **{field: value})) is False


def test_wrong_key_and_malformed_signature_return_false():
    other = Ed25519PrivateKey.generate().public_key()
    sig = signing.sign_run(_key(), **_RUN)
    assert signing.verify_run(other, signature=sig, **_RUN) is False
    assert signing.verify_run(_key().public_key(), signature="not-hex", **_RUN) is False
    assert signing.verify_run(_key().public_key(), signature="", **_RUN) is False


def test_run_signature_is_not_a_valid_checkpoint():
    # Domain separation: a run receipt must never verify as an audit checkpoint (distinct tag).
    k = _key()
    run_sig = signing.sign_run(k, **_RUN)
    assert CP.verify_checkpoint(k.public_key(), signature=run_sig,
                                instance_id=_RUN["instance_id"], seq=1,
                                row_hash=_RUN["result_hash"],
                                created_at=_RUN["created_at"]) is False


def test_checkpoint_signature_is_not_a_valid_run():
    k = _key()
    cp_sig = CP.sign_checkpoint(k, instance_id="inst_test_0001", seq=1,
                                row_hash="e" * 64, created_at=_RUN["created_at"])
    assert signing.verify_run(k.public_key(), signature=cp_sig, **_RUN) is False


def test_run_material_wire_format_is_frozen():
    # Independently reconstruct the signed pre-image: guards tag + field set + canonicalization.
    body = {k: _RUN[k] for k in ("engine_version", "instance_id", "event_ext_id", "run_ext_id",
                                 "inputs_hash", "result_hash", "created_at")}
    assert signing.run_material(**_RUN) == _TAG + _SEP + canonical_bytes(body)


def test_content_hash_preimage_is_tagged_and_kind_scoped():
    obj = {"b": 2, "a": [1, 2, 3], "nested": {"x": 1.5}}
    expected = hashlib.sha256(_TAG + _SEP + b"inputs" + _SEP + canonical_bytes(obj)).hexdigest()
    assert signing.content_hash("inputs", obj) == expected
    # Deterministic + kind-scoped: same bytes under a different kind must not collide.
    assert signing.content_hash("inputs", dict(obj)) == expected
    assert signing.content_hash("inputs", obj) != signing.content_hash("result", obj)


def test_canonical_result_excludes_only_gauge_error():
    result = {"rows": [{"rank": 1, "submission": "prj_a"}], "n_ballots": 3, "lambda": 1.0,
              "sigma": 0.5, "gauge_error": 3.1e-15, "n_components": 1, "n_boot": 200,
              "n_submissions": 1, "n_judges": 3, "unresolved_count": 0}
    canon = engine.canonical_result(result)
    assert "gauge_error" not in canon
    assert set(canon) == set(result) - {"gauge_error"}
    # Two runs identical except for the machine-precision gauge hash the same result.
    other = dict(result, gauge_error=9.9e-13)
    assert (signing.content_hash("result", engine.canonical_result(result))
            == signing.content_hash("result", engine.canonical_result(other)))

