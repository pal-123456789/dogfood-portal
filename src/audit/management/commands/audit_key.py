# src/audit/management/commands/audit_key.py
"""audit_key -- generate (if absent) the Ed25519 signing key and print its PUBLIC half.

Run this once after `docker compose up`, BEFORE judging: it materialises the private key on
/state (0600, never printed) and prints the public key + fingerprint for an independent party
to pin. Anyone holding the public key can verify a checkpoint; no one but the /state key-holder
can sign one. `--out <file>` also writes public-key.pem for handing off.
"""
from pathlib import Path

from django.core.management.base import BaseCommand

from audit import keys, receipts


class Command(BaseCommand):
    help = "Generate-if-absent the audit signing key; print its public key + fingerprint."

    def add_arguments(self, parser):
        parser.add_argument("--key", default=None, help="private key path (default: /state)")
        parser.add_argument("--out", default=None, help="also write public-key.pem here")

    def handle(self, *args, **opts):
        key, created = keys.ensure_private_key(opts["key"])
        pub = key.public_key()
        pem = receipts.public_key_to_pem(pub)
        self.stdout.write("private key = %s (%s)" % (
            keys.key_path() if opts["key"] is None else opts["key"],
            "generated now" if created else "already present"))
        self.stdout.write("fingerprint = %s" % receipts.public_fingerprint(pub))
        if opts["out"]:
            Path(opts["out"]).write_bytes(pem)
            self.stdout.write("public key  -> %s" % opts["out"])
        else:
            self.stdout.write(pem.decode("ascii").rstrip())
