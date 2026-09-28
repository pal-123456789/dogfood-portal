# tests/test_verifier_golden.py
"""CI golden for the offline normalization-run verifier (src/normalize/verify.py).

DB-free and network-free, like the rest of tests/: it imports only normalize.verify (pure
numpy + cryptography + stdlib -- no Django DB, no settings loaded here, no sockets) and runs it
against a COMMITTED golden bundle. The golden is generated once on a developer machine by
tools/make_golden_bundle.py and committed as tests/goldens/golden_bundle.json; CI never
regenerates it. That makes this a true regression check: the exact signed bytes a judge would be
handed must still verify, and a one-byte tamper must still be rejected.

If the golden is absent (not yet generated) the whole module skips, so CI stays green until the
real signed bytes are committed. The golden packs the verifier's four on-disk files (run.json /
inputs.json / result.json / public-key.pem) into one JSON object; each test unpacks a FRESH copy
into tmp_path and calls the real verify_bundle, so a tamper never leaks between tests. Formatting
of the unpacked files is irrelevant -- the verifier re-parses and canonicalizes, so the committed
inputs_hash / result_hash / signature are what actually bind.
"""
import json
from pathlib import Path

import pytest

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


def test_golden_bundle_verifies(tmp_path):
    # The committed, signed bytes must PASS every check (valid signature + matching hashes +
    # the ranking reproducing from the pinned inputs).
    ok, checks = verify.verify_bundle(_materialize(tmp_path, _load()))
    assert ok is True, checks
    assert all(passed for _n, passed, _d in checks)


def test_one_byte_tampered_score_is_rejected(tmp_path):
    # Flip a single digit in a signature-covered input score: inputs.json no longer canonicalizes
    # to the signed inputs_hash. This is the "change one digit in a score" tamper.
    bundle = _load()
    b0 = bundle["inputs.json"]["ballots"][0]
    b0["functionality"] = 1 if b0["functionality"] != 1 else 5
    ok, checks = verify.verify_bundle(_materialize(tmp_path, bundle))
    assert ok is False
    assert _named(checks, "inputs.json matches inputs_hash")[1] is False


def test_one_byte_tampered_result_is_rejected(tmp_path):
    # Corrupt one published q value: result.json no longer canonicalizes to the signed result_hash.
    bundle = _load()
    row = bundle["result.json"]["rows"][0]
    row["q"] = round(float(row["q"]) + 0.1, 4)
    ok, checks = verify.verify_bundle(_materialize(tmp_path, bundle))
    assert ok is False
    assert _named(checks, "result.json matches result_hash")[1] is False


def test_one_byte_tampered_signature_is_rejected(tmp_path):
    # Corrupt a single hex char of the Ed25519 signature; the hashes still match, so the failure is
    # isolated to the signature check -- this run is not the one the pinned key signed.
    bundle = _load()
    sig = bundle["run.json"]["signature"]
    bundle["run.json"]["signature"] = ("1" if sig[0] != "1" else "2") + sig[1:]
    ok, checks = verify.verify_bundle(_materialize(tmp_path, bundle))
    assert ok is False
    assert _named(checks, "run signature valid")[1] is False
