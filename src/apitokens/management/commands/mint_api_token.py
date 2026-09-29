# src/apitokens/management/commands/mint_api_token.py
"""Operator command to mint / list / revoke a user's personal API tokens.

The raw token is shown EXACTLY ONCE, here, at mint time -- only its sha256 hash is stored, so it
can never be recovered later (by design). Self-service minting from a signed-in web page is a
follow-up; this command is the operator path and keeps the credential off every persisted surface.

    python manage.py mint_api_token --email judge@x.com --name "CI read-only"
    python manage.py mint_api_token --email judge@x.com --list
    python manage.py mint_api_token --revoke tok_ab12cd34
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apitokens import services
from apitokens.models import ApiToken

User = get_user_model()


class Command(BaseCommand):
    help = "Mint, list, or revoke a user's personal API (Bearer) tokens."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="user the token belongs to (mint/list)")
        parser.add_argument("--name", help="human label for a newly minted token")
        parser.add_argument("--list", action="store_true", help="list the user's tokens")
        parser.add_argument("--revoke", metavar="EXT_ID", help="revoke a token by its ext_id")

    def handle(self, *args, **opts):
        if opts["revoke"]:
            tok = ApiToken.objects.filter(ext_id=opts["revoke"]).select_related("user").first()
            if tok is None:
                raise CommandError("no token with ext_id %r" % opts["revoke"])
            if services.revoke_token(tok.user, tok.ext_id):
                self.stdout.write(self.style.SUCCESS("revoked %s (%s)" % (tok.ext_id, tok.name)))
            else:
                self.stdout.write("already revoked: %s" % tok.ext_id)
            return

        if not opts["email"]:
            raise CommandError("--email is required (or use --revoke EXT_ID)")
        user = User.objects.filter(email__iexact=opts["email"]).first()
        if user is None:
            raise CommandError("no user with email %r" % opts["email"])

        if opts["list"]:
            rows = ApiToken.objects.filter(user=user)
            if not rows:
                self.stdout.write("no tokens for %s" % user.email)
            for t in rows:
                state = "revoked" if t.revoked_at else "active"
                self.stdout.write("  %s  %-24s  %-8s  last_used=%s" % (
                    t.ext_id, t.name, state, t.last_used_at))
            return

        if not opts["name"]:
            raise CommandError("--name is required when minting")
        token, raw = services.create_token(user, opts["name"])
        self.stdout.write(self.style.SUCCESS("minted %s for %s" % (token.ext_id, user.email)))
        self.stdout.write("")
        self.stdout.write("  RAW TOKEN (shown once -- store it now, it is NOT recoverable):")
        self.stdout.write(self.style.WARNING("    " + raw))
        self.stdout.write("")
        self.stdout.write("  Use it as:  Authorization: Bearer " + raw)
