# src/normalize/release.py
"""ONE offline verifier for a signed RELEASE bundle -- the whole chain of custody in one command.

A release bundle (written by `manage.py release_bundle`) is the UNION of the two focused bundles
this repo already ships, plus a human-readable ranking:

  run.json / inputs.json / result.json   the signed, reproducible normalization run  (normalize.verify)
  checkpoint.json / audit-prefix.jsonl   the append-only audit hash-chain + signed head (audit.verify)
  public-key.pem                         the ONE operator Ed25519 key that signed BOTH
  ranking.csv                            the published ranking, rendered from the signed result
  release.json / verification-instructions.txt   manifest + how-to

`verify_release` runs BOTH existing offline verifiers unchanged, then adds the cross-links that make
the bundle worth more than its parts: the checkpoint and the run share one signer; the run's
`normalization.published` finalization is an event that actually sits INSIDE the signed chain, for
this exact run_ext_id and result_hash; that event is at/below the signed checkpoint head; and
ranking.csv is byte-for-byte the signed result rendered canonically. Pure: stdlib + numpy +
cryptography, no Django and nothing from the deployment, so a judge runs it on a clean machine:
    python -m normalize.release <dir>

Honest scope (docs/THREAT-MODEL.md): the operator holds the private key, so a PASS is decisive only
if an independent party pinned public-key.pem + its fingerprint BEFORE judging and retained the
checkpoint. This tool recomputes and cross-checks; it cannot bind the operator by itself.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

from audit import verify as audit_verify

from . import verify as norm_verify

# result["rows"] keys, in a FROZEN column order -- engine_version pins the schema, so this is stable.
# Display-only per-row fields (engine._UNSIGNED_ROW_FIELDS, e.g. win_next) are deliberately absent:
# ranking_csv reads only these columns, so the signed CSV mirrors canonical_result and never carries
# a value the run does not commit to.
RANKING_COLUMNS = ("rank", "submission", "title", "track", "q", "raw_mean", "delta",
                   "rank_lo", "rank_median", "rank_hi", "n_ballots", "tied_with_next", "component")


def _fmt(v) -> str:
    """Deterministic cell text. Same function on both ends => byte-identical CSV by construction."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return format(v, ".10g")
    if v is None:
        return ""
    return str(v)


def ranking_csv(result) -> str:
    """Render result['rows'] to a canonical CSV string (LF terminators, csv-quoted titles)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(RANKING_COLUMNS)
    for r in result.get("rows", []):
        w.writerow([_fmt(r.get(c)) for c in RANKING_COLUMNS])
    return buf.getvalue()


def _load_jsonl(path) -> list:
    rows = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def verify_release(bundle_dir) -> tuple[bool, list[tuple[str, bool, str]]]:
    """Return (ok, checks). Runs both sub-verifiers, then binds them with four cross-links."""
    d = Path(bundle_dir)
    checks: list[tuple[str, bool, str]] = []

    a_ok, a_checks = audit_verify.verify_bundle(d)
    checks += [("audit/" + n, ok, det) for n, ok, det in a_checks]
    n_ok, n_checks = norm_verify.verify_bundle(d)
    checks += [("run/" + n, ok, det) for n, ok, det in n_checks]
    if not (a_ok and n_ok):
        checks.append(("cross-link: both sub-bundles verify first", False,
                       "audit_ok=%s run_ok=%s -- cross-link checks skipped" % (a_ok, n_ok)))
        return False, checks

    try:
        cp = json.loads((d / "checkpoint.json").read_text(encoding="utf-8"))
        run = json.loads((d / "run.json").read_text(encoding="utf-8"))
        result = json.loads((d / "result.json").read_text(encoding="utf-8"))
        rows = _load_jsonl(d / "audit-prefix.jsonl")
        csv_text = (d / "ranking.csv").read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        checks.append(("cross-link: bundle cross-files parseable", False, str(exc)))
        return False, checks

    ok = True
    # (x1) One signer: the audit checkpoint and the normalization run were signed under one key.
    same_fp = (cp.get("fingerprint") == run.get("fingerprint"))
    ok = ok and same_fp
    checks.append(("cross-link: checkpoint and run share one signer", same_fp,
                   "checkpoint=%s run=%s" % (cp.get("fingerprint"), run.get("fingerprint"))))

    # (x2) The run's finalization is an event INSIDE the signed chain, for THIS run + result.
    #      audit_seq is NOT part of the run signature, so it must be validated here, never trusted.
    link = [r for r in rows
            if r.get("event_type") == "normalization.published"
            and r.get("object_id") == run.get("run_ext_id")]
    payload = (link[0].get("payload") or {}) if link else {}
    link_ok = (len(link) == 1 and link[0].get("seq") == run.get("audit_seq")
               and payload.get("result_hash") == run.get("result_hash")
               and payload.get("inputs_hash") == run.get("inputs_hash"))
    ok = ok and link_ok
    checks.append(("cross-link: signed run is committed in the audit chain", link_ok,
                   "matches=%d audit_seq=%s" % (len(link), run.get("audit_seq"))))

    # (x3) That finalization event is at/below the signed checkpoint head.
    within = (isinstance(run.get("audit_seq"), int) and isinstance(cp.get("seq"), int)
              and 1 <= run["audit_seq"] <= cp["seq"])
    ok = ok and within
    checks.append(("cross-link: run's audit event is within the signed checkpoint", within,
                   "audit_seq=%s checkpoint_seq=%s" % (run.get("audit_seq"), cp.get("seq"))))

    # (x4) ranking.csv is exactly the signed result, rendered canonically.
    csv_ok = (csv_text == ranking_csv(result))
    ok = ok and csv_ok
    checks.append(("cross-link: ranking.csv matches the signed result", csv_ok,
                   "%d data rows" % len(result.get("rows", []))))

    return bool(ok), checks


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m normalize.release <bundle_dir>", file=sys.stderr)
        return 2
    ok, checks = verify_release(argv[0])
    for name, passed, detail in checks:
        print("[%s] %s%s" % ("PASS" if passed else "FAIL", name,
                             (" -- " + detail) if detail else ""))
    print("\nRESULT:", "VERIFIED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
