# tests/test_verifier_vectors.py
"""Extra tamper vectors for the offline normalization-run verifier (src/normalize/verify.py).

Companion to tests/test_verifier_golden.py -- same DB-free, network-free discipline: it imports
only normalize.verify (pure numpy + cryptography + stdlib), plus cryptography itself for the one
vector that needs a second Ed25519 key, and runs the REAL verify_bundle against the COMMITTED
golden bundle. If the golden is absent (not yet generated) the whole module skips so CI stays
green. Each test unpacks a FRESH copy of the four on-disk files (run.json / inputs.json /
result.json / public-key.pem) into its own tmp_path via _materialize, so a tamper never leaks
between tests.

The golden test already pins three vectors -- a tampered input score ("inputs.json matches
inputs_hash"), a tampered published q ("result.json matches result_hash") and a corrupted
signature hex ("run signature valid"). The vectors here are DISTINCT: they exercise the checks
and the ordered short-circuit those three do not -- the public-key fingerprint pin, the
engine-version gate, ballot-order sensitivity, a different signed input field, and a missing
bundle file. Every named check string below is copied verbatim from verify_bundle's checks.
"""
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from normalize import verify

_GOLDEN = Path(__file__).parent / "goldens" / "golden_bundle.json"

if not _GOLDEN.exists():
    pytest.skip("golden bundle not yet generated; run tools/make_golden_bundle.py",
                allow_module_level=True)


def _load() -> dict:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def _materialize(dst, bundle: dict) -> Path:
    """Write the packed bundle out as the four files verify_bundle reads, into a fresh dir."""
    d = Path(dst)
    for name in ("run.json", "inputs.json", "result.json"):
        (d / name).write_text(json.dumps(bundle[name]), encoding="utf-8")
    (d / "public-key.pem").write_text(bundle["public-key.pem"], encoding="utf-8")
    return d


def _named(checks, name):
    return next((c for c in checks if c[0] == name), None)


def _fresh_public_pem() -> str:
    """A DIFFERENT valid Ed25519 public key as SubjectPublicKeyInfo PEM -- the same encoding the
    bundle's public-key.pem uses, so verify loads it fine, but its fingerprint (sha256 over the 32
    raw public-key bytes) cannot match the one signed into run.json."""
    key = Ed25519PrivateKey.generate()
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def test_wrong_public_key_fails_fingerprint_pin_before_signature(tmp_path):
    # Swap public-key.pem for a freshly generated, valid Ed25519 key. verify pins the key by the
    # fingerprint recorded in run.json BEFORE it checks the signature, so a swapped key is caught
    # at the fingerprint pin and the signature check is never reached (NOT the signature check).
    bundle = _load()
    bundle["public-key.pem"] = _fresh_public_pem()
    ok, checks = verify.verify_bundle(_materialize(tmp_path, bundle))
    assert ok is False
    assert _named(checks, "public key fingerprint matches run")[1] is False
    assert _named(checks, "run signature valid") is None


def test_tampered_rubric_weight_fails_inputs_hash(tmp_path):
    # Rubric weights are part of the signed inputs. Bumping one weight changes inputs.json's
    # canonical bytes, so it no longer hashes to the signed inputs_hash. (Distinct signed field
    # from the golden test's ballot-score tamper; same binding check.)
    bundle = _load()
    weights = bundle["inputs.json"]["weights"]
    c0 = sorted(weights)[0]
    weights[c0] = float(weights[c0]) + 1.0
    ok, checks = verify.verify_bundle(_materialize(tmp_path, bundle))
    assert ok is False
    assert _named(checks, "inputs.json matches inputs_hash")[1] is False


def test_altered_engine_version_fails_version_gate(tmp_path):
    # Change the engine version stamped in run.json. verify requires
    # run.engine_version == inputs.engine_version == the verifier's engine.ENGINE_VERSION and gates
    # on it BEFORE the hash and signature checks, so the failure is isolated to the version gate.
    bundle = _load()
    bundle["run.json"]["engine_version"] = bundle["run.json"]["engine_version"] + "-tampered"
    ok, checks = verify.verify_bundle(_materialize(tmp_path, bundle))
    assert ok is False
    assert _named(checks, "engine version matches verifier")[1] is False
    assert _named(checks, "inputs.json matches inputs_hash") is None


def test_reordered_ballot_rows_fail_inputs_hash(tmp_path):
    # The signed inputs_hash canonicalizes with sorted dict keys but PRESERVES list order, so the
    # order of the ballot rows is bound by the signature. Reversing the ballots (a pure reorder,
    # no value changed) is a detected tamper against inputs_hash -- verify is order-sensitive here,
    # it does NOT re-sort ballots into a canonical order.
    bundle = _load()
    ballots = bundle["inputs.json"]["ballots"]
    bundle["inputs.json"]["ballots"] = list(reversed(ballots))
    assert bundle["inputs.json"]["ballots"] != ballots  # golden fixture has >1 distinct rows
    ok, checks = verify.verify_bundle(_materialize(tmp_path, bundle))
    assert ok is False
    assert _named(checks, "inputs.json matches inputs_hash")[1] is False


def test_missing_bundle_file_is_not_verified(tmp_path):
    # Delete one of the four required files. verify catches the read error and returns ok False
    # with a failed "bundle files present and parseable" check -- it must never fall through to a
    # truthy result when a file is missing.
    bundle = _load()
    d = _materialize(tmp_path, bundle)
    (d / "run.json").unlink()
    ok, checks = verify.verify_bundle(d)
    assert ok is False
    assert _named(checks, "bundle files present and parseable")[1] is False
