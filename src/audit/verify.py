# src/audit/verify.py
"""Offline verifier for a signed audit checkpoint bundle -- the piece an INDEPENDENT party runs.

A bundle directory contains:
  checkpoint.json      {schema_version, instance_id, seq, row_hash, created_at, fingerprint, signature}
  public-key.pem       the Ed25519 public key (SubjectPublicKeyInfo)
  audit-prefix.jsonl   one audit row per line: the frozen HEADER_FIELDS + prev_hash + row_hash + payload

Verification is deliberately independent of Django and of the running instance: given only these
files it (1) pins the public key by fingerprint, (2) checks the checkpoint's Ed25519 signature,
(3) recomputes the whole hash chain over the prefix, and (4) confirms the prefix head equals the
signed (seq, row_hash). It needs only the stdlib plus `cryptography`, so a judge can run it on a
machine that never touched the deployment.

Honest scope (docs/THREAT-MODEL.md): a PASS proves the prefix is internally consistent AND matches
a checkpoint signed by the pinned key. It becomes evidence against the operator only if the
checkpoint was retained by an independent party BEFORE a disputed change -- the operator holds the
private key and could re-sign a rewritten history, or sign two conflicting histories (equivocation).
Comparing independently-retained checkpoints is what closes that gap; this tool cannot.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .hashchain import verify_chain
from . import receipts


def _load_jsonl(path: Path):
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def verify_bundle(bundle_dir) -> tuple[bool, list[tuple[str, bool, str]]]:
    """Return (ok, checks) where checks is [(name, ok, detail)] in evaluation order.

    Stops reporting further checks after the first failure (later checks may be meaningless once an
    earlier invariant breaks), so `ok` is the AND of the checks actually run.
    """
    d = Path(bundle_dir)
    checks: list[tuple[str, bool, str]] = []

    try:
        cp = json.loads((d / "checkpoint.json").read_text(encoding="utf-8"))
        pub_pem = (d / "public-key.pem").read_bytes()
        rows = _load_jsonl(d / "audit-prefix.jsonl")
    except (OSError, ValueError) as exc:
        return False, [("bundle files present and parseable", False, str(exc))]
    checks.append(("bundle files present and parseable", True,
                   "%d audit rows" % len(rows)))

    # (1) Pin the public key by fingerprint recorded in the checkpoint.
    try:
        pub = receipts.public_key_from_pem(pub_pem)
        fp = receipts.public_fingerprint(pub)
    except Exception as exc:                                   # noqa: BLE001 - report, don't crash
        checks.append(("public key loads", False, str(exc)))
        return False, checks
    fp_ok = (fp == cp.get("fingerprint"))
    checks.append(("public key fingerprint matches checkpoint", fp_ok,
                   "bundle=%s checkpoint=%s" % (fp, cp.get("fingerprint"))))
    if not fp_ok:
        return False, checks

    # (2) Checkpoint signature verifies under the pinned key.
    sig_ok = receipts.verify_checkpoint(
        pub, instance_id=cp.get("instance_id"), seq=cp.get("seq"),
        row_hash=cp.get("row_hash"), created_at=cp.get("created_at"),
        signature=cp.get("signature", ""))
    checks.append(("checkpoint signature valid", sig_ok, "seq=%s" % cp.get("seq")))
    if not sig_ok:
        return False, checks

    # (3) The hash chain over the prefix recomputes cleanly.
    chain_ok, broken_seq, reason = verify_chain(rows)
    checks.append(("audit chain recomputes", chain_ok,
                   reason if chain_ok else "break at seq %s: %s" % (broken_seq, reason)))
    if not chain_ok:
        return False, checks

    # (4) The prefix head equals the signed (instance_id, seq, row_hash).
    head = rows[-1] if rows else None
    head_ok = bool(head) and head.get("seq") == cp.get("seq") \
        and head.get("row_hash") == cp.get("row_hash") \
        and head.get("instance_id") == cp.get("instance_id")
    checks.append(("prefix head matches signed checkpoint", head_ok,
                   "head seq=%s" % (head.get("seq") if head else None)))
    return head_ok, checks


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m audit.verify <bundle_dir>", file=sys.stderr)
        return 2
    ok, checks = verify_bundle(argv[0])
    for name, passed, detail in checks:
        print("[%s] %s%s" % ("PASS" if passed else "FAIL", name,
                             (" -- " + detail) if detail else ""))
    print("\nRESULT:", "VERIFIED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
