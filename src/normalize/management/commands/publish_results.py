# src/normalize/management/commands/publish_results.py
"""publish_results -- publish the current event's official, publicly visible ranking (W1).

The governance step on top of `normalize_publish`: it builds+signs a fresh reproducible run AND
appends the next-version ResultPublication, co-committing a `results.published` audit event and
flipping Event.results_published -- all in ONE transaction (normalize.results.publish_results). After
this, the ranking is live at /normalize/results and verifies offline like any signed run.

Operator/ad-hoc only -- NOT wired into the boot entrypoint or the build gate, so the shipped image
still boots with nothing published (results private by default). Run:
  manage.py publish_results --status final --note "Final results after judge review."
"""
from django.core.management.base import BaseCommand, CommandError

from audit import keys

from normalize import results, services
from normalize.models import ResultPublication


class Command(BaseCommand):
    help = "Publish the current event's official results (signs a run + appends a versioned publication)."

    def add_arguments(self, parser):
        parser.add_argument("--status", default=ResultPublication.FINAL,
                            choices=[ResultPublication.PROVISIONAL, ResultPublication.FINAL],
                            help="final (default) or provisional")
        parser.add_argument("--note", default="", help="human note stored with the publication")
        parser.add_argument("--boot", type=int, default=1000, help="bootstrap draws")
        parser.add_argument("--seed", type=int, default=0, help="RNG seed (pinned into the run)")
        parser.add_argument("--key", default=None,
                            help="private key path (default: $DOGFOOD_AUDIT_KEY or /state)")
        parser.add_argument("--actor", default="", help="actor user id recorded on the audit event")

    def handle(self, *args, **opts):
        event = services.current_event()
        if event is None:
            raise CommandError("no event configured -- run the seed first")
        key, created = keys.ensure_private_key(opts["key"])
        try:
            pub, run = results.publish_results(
                event, key=key, note=opts["note"], status=opts["status"],
                actor_user_id=opts["actor"], n_boot=opts["boot"], seed=opts["seed"])
        except ValueError as exc:
            raise CommandError(str(exc))

        w = self.stdout.write
        w("publish_results = OK%s" % (" (key generated)" if created else ""))
        w("  event       = %s" % event.ext_id)
        w("  version     = %d   status = %s" % (pub.version, pub.status))
        w("  run_ext_id  = %s" % run.run_ext_id)
        w("  result_hash = %s" % run.result_hash)
        w("  fingerprint = %s" % run.fingerprint)
        w("  audit event = seq %d (results.published)" % pub.audit_seq)
        w("  public at   = /normalize/results   (verify: python -m normalize.verify)")
