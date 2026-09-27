# tests/test_audit_receipts.py
"""Golden-vector and tamper tests for Ed25519 audit checkpoints (src/audit/receipts.py).

DB-free like the rest of tests/. Ed25519 is deterministic (RFC 8032), so a fixed private key over
a fixed checkpoint yields a fixed signature -- we pin both the public-key fingerprint and the
signature. If the signed material (fields, domain tag, or canonicalization) ever changes, a
public-key.pem + checkpoint.json already handed to judges would stop verifying; these goldens make
that a loud failure. Requires the `cryptography` wheel (added to requirements.txt for this reason).
"""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from audit import receipts as R

# Fixed 32-byte test seed -> a reproducible keypair (NEVER a real key; tests only).
_SEED = bytes(range(32))
_CP = dict(instance_id="inst_test_0001", seq=2,
           row_hash="d1e2b3e02e203e1a7427112933bcb6a97a17f183ceea9eae6336b45277222543",
           created_at="2026-09-27T04:05:00+00:00")

# Pinned golden outputs for _SEED over _CP (frozen for schema v1).
_GOLDEN_FINGERPRINT = "56475aa75463474c0285df5dbf2bcab73da651358839e9b77481b2eab107708c"
_GOLDEN_SIGNATURE = (
    "0f2997fce3e15d50c1326626150d375ba2426ed854c0919c83def90bcf252e0b"
    "ec503e675cc5dd7596b8172a13298b72a1f98eced7029cc4ec2630bf53cec603"
)


def _key():
    return Ed25519PrivateKey.from_private_bytes(_SEED)


def test_fingerprint_and_signature_are_frozen():
    k = _key()
    assert R.public_fingerprint(k.public_key()) == _GOLDEN_FINGERPRINT
    assert R.sign_checkpoint(k, **_CP) == _GOLDEN_SIGNATURE

def test_valid_signature_verifies():
    k = _key()
    assert R.verify_checkpoint(k.public_key(), signature=_GOLDEN_SIGNATURE, **_CP) is True

def test_each_tampered_field_fails_verification():
    pub = _key().public_key()
    for field, value in (("seq", 3), ("row_hash", "0" * 64),
                         ("instance_id", "inst_other"), ("created_at", "2026-01-01T00:00:00+00:00")):
        tampered = dict(_CP, **{field: value})
        assert R.verify_checkpoint(pub, signature=_GOLDEN_SIGNATURE, **tampered) is False

def test_wrong_key_fails_verification():
    other = Ed25519PrivateKey.generate().public_key()
    assert R.verify_checkpoint(other, signature=_GOLDEN_SIGNATURE, **_CP) is False

def test_malformed_signature_returns_false_not_raises():
    pub = _key().public_key()
    assert R.verify_checkpoint(pub, signature="not-hex", **_CP) is False
    assert R.verify_checkpoint(pub, signature="", **_CP) is False

def test_public_pem_roundtrip_preserves_fingerprint():
    pub = _key().public_key()
    pem = R.public_key_to_pem(pub)
    assert pem.splitlines()[0] == b"-----BEGIN PUBLIC KEY-----"
    assert R.public_fingerprint(R.public_key_from_pem(pem)) == _GOLDEN_FINGERPRINT

def test_private_pem_roundtrip_plain_and_encrypted():
    k = _key()
    plain = R.private_key_from_pem(R.private_key_to_pem(k))
    assert R.sign_checkpoint(plain, **_CP) == _GOLDEN_SIGNATURE
    enc_pem = R.private_key_to_pem(k, password=b"correct horse")
    enc = R.private_key_from_pem(enc_pem, password=b"correct horse")
    assert R.sign_checkpoint(enc, **_CP) == _GOLDEN_SIGNATURE

def test_rejects_non_ed25519_pem():
    # A malformed / non-Ed25519 public PEM must raise, not silently return something usable.
    for bad in (b"-----BEGIN PUBLIC KEY-----\nnope\n-----END PUBLIC KEY-----\n", b"garbage"):
        try:
            R.public_key_from_pem(bad)
            raised = False
        except Exception:
            raised = True
        assert raised, "expected a rejection for %r" % bad
