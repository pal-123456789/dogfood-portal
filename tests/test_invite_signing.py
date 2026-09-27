# tests/test_invite_signing.py
"""Golden / wire-format tests for the invitation signing layer (§20).

DB-free like the rest of tests/. Ed25519 is deterministic (RFC 8032) and its public-key
fingerprint depends only on the key, so the fixed seed reproduces the SAME fingerprint the audit
and normalize layers pin -- one shared key path. The invite *material* is pinned by independent
reconstruction, so any change to the domain tag, field set, or canonicalization is a loud failure.
Domain separation is proven directly: an invite signature must not verify as an audit checkpoint
or a normalization run, nor either of those as an invite.
"""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from audit import receipts as CP
from audit.hashchain import canonical_bytes
from events import invite_signing as signing
from normalize import engine, signing as run_signing

# Same fixed 32-byte seed as tests/test_audit_receipts.py -> same public key -> same fingerprint.
_SEED = bytes(range(32))
_GOLDEN_FINGERPRINT = "56475aa75463474c0285df5dbf2bcab73da651358839e9b77481b2eab107708c"
_TAG = b"dogfood.invite.v1"
_SEP = b"\x1e"

_INVITE = dict(event_ext_id="evt_demo", invite_ext_id="inv_0011223344556677",
               role="judge", expires_at="2099-12-31T23:59:00+00:00")


def _key():
    return Ed25519PrivateKey.from_private_bytes(_SEED)


def test_fingerprint_matches_shared_key_path():
    # The invite signer reuses audit's Ed25519 code path, so the fingerprint is byte-identical.
    assert signing.public_fingerprint(_key().public_key()) == _GOLDEN_FINGERPRINT


def test_sign_then_verify_roundtrips():
    k = _key()
    sig = signing.sign_invite(k, **_INVITE)
    assert signing.verify_invite(k.public_key(), signature=sig, **_INVITE) is True


def test_never_expiring_invite_roundtrips():
    # expires_at="" (a link with no expiry) is a total, reproducible pre-image.
    k = _key()
    body = dict(_INVITE, expires_at="")
    sig = signing.sign_invite(k, **body)
    assert signing.verify_invite(k.public_key(), signature=sig, **body) is True


def test_each_tampered_field_fails_verification():
    k = _key()
    pub = k.public_key()
    sig = signing.sign_invite(k, **_INVITE)
    for field, value in (("event_ext_id", "evt_other"), ("invite_ext_id", "inv_dead"),
                         ("role", "participant"), ("expires_at", "2000-01-01T00:00:00+00:00")):
        assert signing.verify_invite(pub, signature=sig, **dict(_INVITE, **{field: value})) is False


def test_wrong_key_and_malformed_signature_return_false():
    other = Ed25519PrivateKey.generate().public_key()
    sig = signing.sign_invite(_key(), **_INVITE)
    assert signing.verify_invite(other, signature=sig, **_INVITE) is False
    assert signing.verify_invite(_key().public_key(), signature="not-hex", **_INVITE) is False
    assert signing.verify_invite(_key().public_key(), signature="", **_INVITE) is False


def test_invite_signature_is_not_a_valid_checkpoint():
    # Domain separation: an invite receipt must never verify as an audit checkpoint (distinct tag).
    k = _key()
    inv_sig = signing.sign_invite(k, **_INVITE)
    assert CP.verify_checkpoint(k.public_key(), signature=inv_sig,
                                instance_id="inst_x", seq=1, row_hash="a" * 64,
                                created_at=_INVITE["expires_at"]) is False


def test_checkpoint_signature_is_not_a_valid_invite():
    k = _key()
    cp_sig = CP.sign_checkpoint(k, instance_id="inst_x", seq=1,
                                row_hash="a" * 64, created_at=_INVITE["expires_at"])
    assert signing.verify_invite(k.public_key(), signature=cp_sig, **_INVITE) is False


def test_invite_signature_is_not_a_valid_run():
    # Domain separation against the normalization-run tag too (three distinct signed spaces).
    k = _key()
    inv_sig = signing.sign_invite(k, **_INVITE)
    assert run_signing.verify_run(
        k.public_key(), signature=inv_sig, engine_version=engine.ENGINE_VERSION,
        instance_id="inst_x", event_ext_id=_INVITE["event_ext_id"], run_ext_id="nrun_0",
        inputs_hash="a" * 64, result_hash="b" * 64, created_at=_INVITE["expires_at"]) is False


def test_invite_material_wire_format_is_frozen():
    # Independently reconstruct the signed pre-image: guards tag + field set + canonicalization.
    body = {k: _INVITE[k] for k in ("event_ext_id", "invite_ext_id", "role", "expires_at")}
    assert signing.invite_material(**_INVITE) == _TAG + _SEP + canonical_bytes(body)
