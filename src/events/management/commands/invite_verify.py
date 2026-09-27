# src/events/management/commands/invite_verify.py
"""invite_verify -- re-verify a stored invitation's Ed25519 signature against the /state key.

Operator-side check that an invite row (event, ext_id, role, expiry) still matches the signature
minted for it: a tampered role/event/expiry, or a swapped signature, prints FAILED. The same
signature can be checked by any independent party holding the exported public key and the invite's
canonical fields (events/invite_signing.py) -- this command is the convenience wrapper that reads
those fields from the DB. It does NOT prove single-use (that is the DB redeemed_at flip) and, like
audit_verify, cannot bind an operator who holds the signing key (docs/THREAT-MODEL.md).
"""
from django.core.management.base import BaseCommand, CommandError

from audit import keys as audit_keys
from events import invite_signing
from events.models import Invite


class Command(BaseCommand):
    help = "Re-verify a stored invitation's Ed25519 signature against the deployment key."

    def add_arguments(self, parser):
        parser.add_argument("ext_id", help="the invite ext_id (e.g. inv_ab12...)")

    def handle(self, *args, **opts):
        try:
            invite = Invite.objects.select_related("event").get(ext_id=opts["ext_id"])
        except Invite.DoesNotExist:
            raise CommandError("no invite with ext_id %r" % opts["ext_id"])
        pub = audit_keys.ensure_private_key()[0].public_key()
        expires_iso = invite.expires_at.isoformat() if invite.expires_at else ""
        ok = invite_signing.verify_invite(
            pub, signature=invite.signature, event_ext_id=invite.event.ext_id,
            invite_ext_id=invite.ext_id, role=invite.role, expires_at=expires_iso)
        self.stdout.write("invite      = %s" % invite.ext_id)
        self.stdout.write("event       = %s" % invite.event.ext_id)
        self.stdout.write("role        = %s" % invite.role)
        self.stdout.write("expires_at  = %s" % (expires_iso or "(never)"))
        self.stdout.write("redeemed    = %s" % ("yes" if invite.is_redeemed else "no"))
        self.stdout.write("signature   = %s" % ("VERIFIED" if ok else "FAILED"))
        if not ok:
            raise CommandError("invite_verify FAILED: signature does not match the invite fields")
        self.stdout.write(
            "invite_verify = OK\n"
            "NOTE: operator-side check; it does not prove single-use (the DB redeemed_at flip) "
            "and cannot bind a key-holding operator (docs/THREAT-MODEL.md).")
