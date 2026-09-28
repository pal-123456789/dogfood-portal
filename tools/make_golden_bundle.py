#!/usr/bin/env python3
# tools/make_golden_bundle.py
"""Generate the committed CI golden for the offline normalization-run verifier.

WHAT IT PRODUCES
    tests/goldens/golden_bundle.json -- ONE JSON file that packs the four files a signed
    normalization-run bundle contains (the exact shape normalize.runs.export_bundle writes and
    normalize.verify.verify_bundle reads):

        run.json        the signed run header (the two content hashes + Ed25519 signature + key fp)
        inputs.json     the frozen inputs the run committed to (weights + ordered ballots + labels)
        result.json     the published leaderboard dict
        public-key.pem  the Ed25519 public key, SubjectPublicKeyInfo PEM

    They are packed under the keys "run.json"/"inputs.json"/"result.json"/"public-key.pem" so the
    golden is a single reviewable artifact; the CI test tests/test_verifier_golden.py unpacks them
    into a temp dir and runs the REAL verify_bundle. (File formatting is irrelevant to the result:
    the verifier re-parses and canonicalizes, so the committed hashes still bind.)

DETERMINISTIC BY CONSTRUCTION
    A fixed tiny fixture (3 well-separated submissions x 3 judges of differing severity), a fixed
    32-byte key seed (so the public key, its fingerprint and -- Ed25519 being deterministic per
    RFC 8032 -- the signature are byte-stable), seed=0 and a PINNED lambda (so reproduction never
    re-runs cross-validation). Re-running writes byte-identical output. This mirrors the pure core
    of normalize.runs.build_run WITHOUT touching the database: the inputs dict is hand-built
    instead of read from the ORM, so the golden needs only numpy + cryptography -- no DB, no
    Django settings. Before writing, it self-verifies with the very verifier a judge runs, so a
    broken golden fails here rather than in CI.

RUN IT (on your machine -- the golden bytes are COMMITTED, never generated in CI):
    python tools/make_golden_bundle.py
    git add tests/goldens/golden_bundle.json && git commit -m "test: commit verifier CI golden"

Until the golden is committed, tests/test_verifier_golden.py skips, so CI stays green.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))     # runnable outside the container too (PYTHONPATH=src there)

from normalize import engine, signing, verify   # noqa: E402 - after the sys.path bootstrap above

# ---- fixed, documented constants (change ONLY with a regenerate + recommit) -------------------
_KEY_SEED = bytes(range(32))                # same seed as tests/test_normalize_signing.py -> same fp
_EVENT = "evt_golden"
_INSTANCE = "inst_golden_0001"
_RUN_EXT_ID = "nrun_golden_0000000000000001"
_CREATED_AT = "2026-01-01T00:00:00+00:00"
_AUDIT_SEQ = 1              # part of the run.json schema (export_bundle writes it); normalize.verify
                            # does not read it -- only the release verifier cross-links it to a chain.
_N_BOOT = 200
_SEED = 0
_LAMBDA = 1.0              # pinned in-grid value; reproduction never re-runs CV (mirrors a pinned run)

_OUT = _ROOT / "tests" / "goldens" / "golden_bundle.json"

# 3 submissions x 3 judges. Scores separate quality by ~1.0 under every judge, plus a lenient /
# medium / harsh severity offset, so the ranking is unambiguous and the bootstrap ranks never flip:
# the golden reproduces across numpy/BLAS builds because canonical_result rounds q to 4 dp.
_SUBMISSIONS = [
    {"submission": "prj_a", "title": "Alpha",  "track": "trk_01"},
    {"submission": "prj_b", "title": "Bravo",  "track": "trk_01"},
    {"submission": "prj_c", "title": "Cesium", "track": "trk_01"},
]
# (judge, submission, functionality, quality, innovation) -- integers 1..5, exactly as a ballot.
_RAW = [
    ("jdg_h", "prj_a", 4, 4, 4), ("jdg_h", "prj_b", 3, 3, 3), ("jdg_h", "prj_c", 2, 2, 2),
    ("jdg_m", "prj_a", 5, 4, 5), ("jdg_m", "prj_b", 4, 3, 4), ("jdg_m", "prj_c", 3, 2, 3),
    ("jdg_l", "prj_a", 5, 5, 5), ("jdg_l", "prj_b", 4, 4, 4), ("jdg_l", "prj_c", 3, 3, 3),
]


def _build_inputs() -> dict:
    """The frozen inputs dict, ballots ordered by (submission, judge) as normalize.runs orders them.

    Equal rubric weights (the seeded default). Ballot ORDER only has to be internally consistent --
    both this generator and normalize.verify._recompute iterate inputs["ballots"] in stored order --
    but we mirror runs._ordered_inputs so the golden looks like a real exported run.
    """
    weights = {c: 1.0 for c in engine.CRITERIA}
    ballots = []
    for judge, sub, f, q, i in sorted(_RAW, key=lambda r: (r[1], r[0])):
        ballots.append({"submission": sub, "judge": judge, "version": 1,
                        "functionality": f, "quality": q, "innovation": i})
    return {
        "engine_version": engine.ENGINE_VERSION,
        "event_ext_id": _EVENT,
        "criteria": list(engine.CRITERIA),
        "weights": {c: float(weights[c]) for c in engine.CRITERIA},
        "n_boot": _N_BOOT,
        "seed": _SEED,
        "lambda": _LAMBDA,
        "ballots": ballots,
        "submissions": list(_SUBMISSIONS),
    }


def _result_from_inputs(inputs: dict) -> dict:
    """Leaderboard reconstructed EXACTLY as normalize.verify._recompute does, so result.json hashes
    to run.result_hash and reproduces on the verifier's side by construction."""
    criteria = inputs["criteria"]
    weights = {c: float(inputs["weights"][c]) for c in criteria}
    y, jk, sk = [], [], []
    for bal in inputs["ballots"]:
        y.append(engine.composite({c: bal[c] for c in criteria}, weights))
        jk.append(bal["judge"])
        sk.append(bal["submission"])
    display = {r["submission"]: (r["title"], r["track"]) for r in inputs["submissions"]}
    return engine.compute_leaderboard(y, jk, sk, display,
                                      n_boot=inputs["n_boot"], seed=inputs["seed"],
                                      lam=inputs["lambda"])


def build_bundle() -> dict:
    """Return the packed golden dict (mirrors runs.build_run + runs.export_bundle, DB-free)."""
    key = Ed25519PrivateKey.from_private_bytes(_KEY_SEED)
    pub = key.public_key()
    inputs = _build_inputs()
    result = _result_from_inputs(inputs)
    inputs_hash = signing.content_hash("inputs", inputs)
    result_hash = signing.content_hash("result", engine.canonical_result(result))
    signature = signing.sign_run(
        key, engine_version=engine.ENGINE_VERSION, instance_id=_INSTANCE,
        event_ext_id=_EVENT, run_ext_id=_RUN_EXT_ID,
        inputs_hash=inputs_hash, result_hash=result_hash, created_at=_CREATED_AT)
    run_json = {
        "engine_version": engine.ENGINE_VERSION, "instance_id": _INSTANCE,
        "event_ext_id": _EVENT, "run_ext_id": _RUN_EXT_ID,
        "inputs_hash": inputs_hash, "result_hash": result_hash,
        "fingerprint": signing.public_fingerprint(pub), "signature": signature,
        "lambda": _LAMBDA, "n_boot": _N_BOOT, "seed": _SEED,
        "created_at": _CREATED_AT, "audit_seq": _AUDIT_SEQ,
    }
    return {
        "_generator": "tools/make_golden_bundle.py",
        "_format": ("dogfood normalization-run bundle (run.json / inputs.json / result.json / "
                    "public-key.pem) packed into one JSON; see tests/goldens/README.md"),
        "run.json": run_json,
        "inputs.json": inputs,
        "result.json": result,
        "public-key.pem": signing.public_key_to_pem(pub).decode("ascii"),
    }


def materialize(dst, bundle: dict) -> Path:
    """Write the packed bundle back out as the four files verify_bundle reads (same helper shape the
    CI test uses)."""
    d = Path(dst)
    d.mkdir(parents=True, exist_ok=True)
    for name in ("run.json", "inputs.json", "result.json"):
        (d / name).write_text(json.dumps(bundle[name]), encoding="utf-8")
    (d / "public-key.pem").write_text(bundle["public-key.pem"], encoding="utf-8")
    return d


def main() -> int:
    bundle = build_bundle()

    # Self-verify with the very verifier a judge (and CI) runs: refuse to write a broken golden.
    with tempfile.TemporaryDirectory() as tmp:
        ok, checks = verify.verify_bundle(materialize(tmp, bundle))
    if not ok:
        failed = "; ".join(n + ((" -- " + d) if d else "")
                           for n, passed, d in checks if not passed)
        print("refusing to write: golden does not self-verify -- failing: " + failed,
              file=sys.stderr)
        return 1

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    run = bundle["run.json"]
    print("wrote %s" % _OUT)
    print("  fingerprint = %s" % run["fingerprint"])
    print("  result_hash = %s" % run["result_hash"])
    print("  rows=%d  top=%s  (self-verify: VERIFIED)"
          % (len(bundle["result.json"]["rows"]), bundle["result.json"]["rows"][0]["submission"]))
    print("Commit it:  git add %s" % _OUT.relative_to(_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
