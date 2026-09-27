# src/normalize/verify.py
"""Offline verifier for a signed normalization-run bundle -- the piece an INDEPENDENT party runs.

A bundle directory contains:
  run.json         {engine_version, instance_id, event_ext_id, run_ext_id, inputs_hash,
                    result_hash, fingerprint, signature, lambda, n_boot, seed, created_at, audit_seq}
  inputs.json      the frozen inputs the run committed to (weights + ordered ballots + labels)
  result.json      the published leaderboard dict
  public-key.pem   the Ed25519 public key (SubjectPublicKeyInfo)

Given only these files it (1) pins the public key by fingerprint, (2) checks the run's Ed25519
signature, (3) confirms inputs.json / result.json canonicalize to the signed hashes, and -- the
point of the whole exercise -- (4) RE-RUNS the estimator from the pinned inputs and confirms the
ranking canonicalizes to the same result_hash. It needs only the stdlib, numpy and `cryptography`,
so a judge can run it on a machine that never touched the deployment: `python -m normalize.verify <dir>`.

Honest scope (docs/THREAT-MODEL.md): a PASS proves the published ranking is exactly what this engine
version produces from the pinned ballots, and that a run signed by the pinned key committed to it. It
is evidence against the operator only if the public key + fingerprint were pinned by an independent
party BEFORE judging -- the operator holds the private key and could re-sign a different run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import engine, signing


def _recompute(inputs) -> dict:
    """Reproduce the leaderboard dict from the pinned inputs, exactly as normalize.runs built it:
    same ballot order, same pinned lambda, same seed and n_boot -- the single shared code path is
    engine.compute_leaderboard, so a faithful bundle reproduces byte for byte."""
    criteria = inputs["criteria"]
    weights = {c: float(inputs["weights"][c]) for c in criteria}
    y, jk, sk = [], [], []
    for bal in inputs["ballots"]:
        raw = {c: bal[c] for c in criteria}
        y.append(engine.composite(raw, weights))
        jk.append(bal["judge"])
        sk.append(bal["submission"])
    display = {r["submission"]: (r["title"], r["track"]) for r in inputs["submissions"]}
    return engine.compute_leaderboard(y, jk, sk, display,
                                      n_boot=inputs["n_boot"], seed=inputs["seed"],
                                      lam=inputs["lambda"])


def verify_bundle(bundle_dir) -> tuple[bool, list[tuple[str, bool, str]]]:
    """Return (ok, checks) where checks is [(name, ok, detail)] in evaluation order.

    Stops reporting further checks after the first failure (later checks may be meaningless once an
    earlier invariant breaks), so `ok` is the AND of the checks actually run.
    """
    d = Path(bundle_dir)
    checks: list[tuple[str, bool, str]] = []

    try:
        run = json.loads((d / "run.json").read_text(encoding="utf-8"))
        inputs = json.loads((d / "inputs.json").read_text(encoding="utf-8"))
        result = json.loads((d / "result.json").read_text(encoding="utf-8"))
        pub_pem = (d / "public-key.pem").read_bytes()
    except (OSError, ValueError) as exc:
        return False, [("bundle files present and parseable", False, str(exc))]
    checks.append(("bundle files present and parseable", True,
                   "%d rows" % len(result.get("rows", []))))

    # (1) Pin the public key by the fingerprint recorded in run.json.
    try:
        pub = signing.public_key_from_pem(pub_pem)
        fp = signing.public_fingerprint(pub)
    except Exception as exc:                                   # noqa: BLE001 - report, don't crash
        checks.append(("public key loads", False, str(exc)))
        return False, checks
    fp_ok = (fp == run.get("fingerprint"))
    checks.append(("public key fingerprint matches run", fp_ok,
                   "bundle=%s run=%s" % (fp, run.get("fingerprint"))))
    if not fp_ok:
        return False, checks

    # (2) The engine that would reproduce this run must be the one that signed it.
    ev_ok = (run.get("engine_version") == inputs.get("engine_version") == engine.ENGINE_VERSION)
    checks.append(("engine version matches verifier", ev_ok,
                   "run=%s inputs=%s verifier=%s" % (
                       run.get("engine_version"), inputs.get("engine_version"),
                       engine.ENGINE_VERSION)))
    if not ev_ok:
        return False, checks

    # (3) inputs.json and result.json canonicalize to the signed hashes.
    ih = signing.content_hash("inputs", inputs)
    ih_ok = (ih == run.get("inputs_hash"))
    checks.append(("inputs.json matches inputs_hash", ih_ok, "recomputed %s" % ih))
    if not ih_ok:
        return False, checks
    rh_pub = signing.content_hash("result", engine.canonical_result(result))
    rh_ok = (rh_pub == run.get("result_hash"))
    checks.append(("result.json matches result_hash", rh_ok, "recomputed %s" % rh_pub))
    if not rh_ok:
        return False, checks

    # (4) The run signature verifies under the pinned key.
    sig_ok = signing.verify_run(
        pub, signature=run.get("signature", ""),
        engine_version=run.get("engine_version"), instance_id=run.get("instance_id"),
        event_ext_id=run.get("event_ext_id"), run_ext_id=run.get("run_ext_id"),
        inputs_hash=run.get("inputs_hash"), result_hash=run.get("result_hash"),
        created_at=run.get("created_at"))
    checks.append(("run signature valid", sig_ok, "run_ext_id=%s" % run.get("run_ext_id")))
    if not sig_ok:
        return False, checks

    # (5) THE REPRODUCTION: re-run the estimator from pinned inputs -> same result_hash.
    try:
        recomputed = _recompute(inputs)
        rh_re = signing.content_hash("result", engine.canonical_result(recomputed))
    except Exception as exc:                                   # noqa: BLE001 - report, don't crash
        checks.append(("ranking reproduces from pinned inputs", False, "recompute raised: %s" % exc))
        return False, checks
    re_ok = (rh_re == run.get("result_hash"))
    checks.append(("ranking reproduces from pinned inputs", re_ok, "recomputed %s" % rh_re))
    if not re_ok:
        return False, checks

    # (6) Sanity: the gauge held (~0) and the published order equals the recomputed order.
    gauge = float(recomputed.get("gauge_error", 0.0))
    pub_order = [r.get("submission") for r in result.get("rows", [])]
    re_order = [r.get("submission") for r in recomputed.get("rows", [])]
    sane = (gauge < 1e-6) and (pub_order == re_order)
    checks.append(("gauge ~0 and published order matches recompute", sane,
                   "gauge=%.2e order_match=%s" % (gauge, pub_order == re_order)))
    return sane, checks


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m normalize.verify <bundle_dir>", file=sys.stderr)
        return 2
    ok, checks = verify_bundle(argv[0])
    for name, passed, detail in checks:
        print("[%s] %s%s" % ("PASS" if passed else "FAIL", name,
                             (" -- " + detail) if detail else ""))
    print("\nRESULT:", "VERIFIED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
