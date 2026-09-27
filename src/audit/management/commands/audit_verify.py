# src/audit/management/commands/audit_verify.py
"""audit_verify -- recompute the in-DB hash chain and confirm the head matches its tip.

This is the ONLINE, operator-side check (the offline, independent-party check is
`python -m audit.verify <bundle>` over an exported bundle). It proves the stored rows are
internally consistent: schema supported, seq contiguous from 1, prev_hash links, payload
digests match, row_hash recomputes -- and that AuditHead points at the last row. It does NOT
prove the operator didn't rewrite BOTH the rows and the head (that needs a retained external
checkpoint); see docs/THREAT-MODEL.md and this command prints that caveat.
"""
from django.core.management.base import BaseCommand, CommandError

from audit import service
from audit.hashchain import verify_chain


class Command(BaseCommand):
    help = "Recompute the in-DB audit hash chain and verify the head matches its tip."

    def handle(self, *args, **opts):
        rows = service.chain_rows()
        head = service.current_head()
        ok, broken_seq, reason = verify_chain(rows)
        self.stdout.write("audit rows      = %d" % len(rows))
        self.stdout.write("chain recomputes = %s%s" % (
            "PASS" if ok else "FAIL",
            "" if ok else "  <- break at seq %s: %s" % (broken_seq, reason)))

        head_ok = True
        if head is not None:
            tip_seq = rows[-1]["seq"] if rows else 0
            tip_hash = rows[-1]["row_hash"] if rows else "0" * 64
            head_ok = (head.seq == tip_seq and head.row_hash == tip_hash)
            self.stdout.write("head matches tip = %s  (head seq=%d)" % (
                "PASS" if head_ok else "FAIL", head.seq))

        if not (ok and head_ok):
            raise CommandError("audit_verify FAILED")
        self.stdout.write(
            "audit_verify = OK (%d rows, internally consistent)\n"
            "NOTE: this is operator-side evidence; independent proof needs an externally "
            "retained checkpoint verified with `python -m audit.verify` (docs/THREAT-MODEL.md)."
            % len(rows))
