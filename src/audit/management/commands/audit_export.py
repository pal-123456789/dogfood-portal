# src/audit/management/commands/audit_export.py
"""audit_export <dir> -- write a signed, independently-verifiable audit bundle.

Emits the three files audit/verify.py expects:
  checkpoint.json     {schema_version, instance_id, seq, row_hash, created_at, fingerprint, signature}
  public-key.pem      the Ed25519 public key (SubjectPublicKeyInfo)
  audit-prefix.jsonl  one JSON row per audit event, in seq order

The private key is loaded from /state (generated once if absent). A judge pins public-key.pem +
its fingerprint BEFORE judging, keeps the checkpoint, and later runs
`python -m audit.verify <dir>` on a machine that never touched this deployment. The command
self-verifies the bundle it just wrote so a broken export fails loudly here, not in the judge's hands.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from audit import keys, receipts, service, verify
from audit.hashchain import SCHEMA_VERSION


class Command(BaseCommand):
    help = "Write a signed, offline-verifiable audit bundle to <dir>."

    def add_arguments(self, parser):
        parser.add_argument("dir", help="output directory for the bundle (created if absent)")
        parser.add_argument("--key", default=None,
                            help="private key path (default: $DOGFOOD_AUDIT_KEY or /state)")

    def handle(self, *args, **opts):
        head = service.current_head()
        if head is None:
            raise CommandError("no AuditHead -- run migrate first")
        rows = service.chain_rows()

        key, created = keys.ensure_private_key(opts["key"])
        pub = key.public_key()
        d = Path(opts["dir"])
        d.mkdir(parents=True, exist_ok=True)

        (d / "audit-prefix.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
            encoding="utf-8")
        (d / "public-key.pem").write_bytes(receipts.public_key_to_pem(pub))

        created_at = timezone.now().isoformat()
        checkpoint = {
            "schema_version": SCHEMA_VERSION, "instance_id": head.instance_id,
            "seq": head.seq, "row_hash": head.row_hash, "created_at": created_at,
            "fingerprint": receipts.public_fingerprint(pub),
            "signature": receipts.sign_checkpoint(
                key, instance_id=head.instance_id, seq=head.seq,
                row_hash=head.row_hash, created_at=created_at),
        }
        (d / "checkpoint.json").write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")

        ok, checks = verify.verify_bundle(d)
        for name, passed, detail in checks:
            self.stdout.write("  [%s] %s%s" % ("PASS" if passed else "FAIL", name,
                                               (" -- " + detail) if detail else ""))
        if not ok:
            raise CommandError("audit_export wrote a bundle that does not self-verify")
        self.stdout.write(
            "audit_export = OK -> %s (%d rows, seq=%d%s)\n"
            "Pin public-key.pem + fingerprint %s BEFORE judging; verify later with "
            "`python -m audit.verify %s`." % (
                d, len(rows), head.seq, ", key generated" if created else "",
                checkpoint["fingerprint"], d))
