# src/normalize/management/commands/normalize_publish.py
"""normalize_publish -- sign & record a reproducible normalization run for the current event.

Signs the run with the SAME operator Ed25519 key as the audit checkpoints (one /state key; the
distinct domain tag dogfood.normalize.run.v1 keeps a run signature from ever being replayed as an
audit checkpoint) and co-commits a `normalization.published` event on the audit chain. With
--export DIR it also writes the four-file offline bundle and self-verifies it, so a broken export
fails loudly here rather than in a judge's hands.

Operator/ad-hoc only -- NOT wired into the boot entrypoint or the build gate, so the shipped image
still boots with an empty (0-row) audit chain. Run:
  manage.py normalize_publish --export /state/normalize-bundle
Then verify anywhere with:  python -m normalize.verify /state/normalize-bundle
"""
from django.core.management.base import BaseCommand, CommandError

from audit import keys

from normalize import runs, services, verify


class Command(BaseCommand):
    help = "Sign & record a reproducible normalization run; optionally export an offline bundle."

    def add_arguments(self, parser):
        parser.add_argument("--boot", type=int, default=1000, help="bootstrap draws")
        parser.add_argument("--seed", type=int, default=0, help="RNG seed (pinned into the run)")
        parser.add_argument("--export", default=None, metavar="DIR",
                            help="also write an offline-verifiable bundle to DIR")
        parser.add_argument("--key", default=None,
                            help="private key path (default: $DOGFOOD_AUDIT_KEY or /state)")

    def handle(self, *args, **opts):
        event = services.current_event()
        if event is None:
            raise CommandError("no event configured -- run the seed first")
        key, created = keys.ensure_private_key(opts["key"])
        try:
            run = runs.publish_run(event, key=key, n_boot=opts["boot"], seed=opts["seed"])
        except ValueError as exc:
            raise CommandError(str(exc))

        w = self.stdout.write
        w("normalize_publish = OK%s" % (" (key generated)" if created else ""))
        w("  run_ext_id  = %s" % run.run_ext_id)
        w("  engine      = %s" % run.engine_version)
        w("  event       = %s   lambda = %g   n_boot = %d   seed = %d"
          % (run.event_ext_id, run.lambda_value, run.n_boot, run.seed))
        w("  inputs_hash = %s" % run.inputs_hash)
        w("  result_hash = %s" % run.result_hash)
        w("  fingerprint = %s" % run.fingerprint)
        w("  audit event = seq %d (normalization.published)" % run.audit_seq)

        if opts["export"]:
            d = runs.export_bundle(run, key.public_key(), opts["export"])
            ok, checks = verify.verify_bundle(d)
            for name, passed, detail in checks:
                w("  [%s] %s%s" % ("PASS" if passed else "FAIL", name,
                                   (" -- " + detail) if detail else ""))
            if not ok:
                raise CommandError("normalize_publish wrote a bundle that does not self-verify")
            w("  bundle -> %s" % d)
            w("  Pin public-key.pem + fingerprint %s BEFORE judging; verify later with "
              "`python -m normalize.verify %s`." % (run.fingerprint, d))
