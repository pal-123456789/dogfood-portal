# src/normalize/management/commands/certificate.py
"""certificate --event <id> [--version N] [--format html|json|txt] [--out <path>] --
emit a verifiable results certificate for an event's official, PUBLISHED results.

The certificate is a rendering + attestation layer over an already signed, already published
normalization run (normalize.certificate): it copies the run's result_hash, signature, signer
fingerprint, public key and ranking verbatim -- no new key, no new signature, no recompute. BEFORE
it emits anything it self-verifies with the SAME offline verifier a judge runs
(normalize.verify.verify_bundle over a freshly exported bundle -- which re-runs the estimator from
the pinned ballots and checks the signature + hashes) AND re-checks the certificate's own Ed25519
signature, failing loudly (CommandError) if either does not verify. So a certificate is only ever
issued for a run that actually reproduces and is validly signed.

Operator/ad-hoc only: NOT wired into boot or the build gate, and OFF the acceptance checker's five
routes. Requires published results (run publish_results first) and the operator key that signed the
run (default $DOGFOOD_AUDIT_KEY or /state), exactly like the release_bundle command.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from audit import keys, receipts
from events.models import Event
from normalize import certificate, results, runs, verify
from normalize.models import NormalizationRun, ResultPublication


def _resolve_event(value):
    ev = Event.objects.filter(ext_id=value).first()
    if ev is None and value.isdigit():
        ev = Event.objects.filter(pk=int(value)).first()
    return ev


class Command(BaseCommand):
    help = "Emit a verifiable results certificate for an event's official published results."

    def add_arguments(self, parser):
        parser.add_argument("--event", required=True,
                            help="event ext_id (e.g. evt_01) or numeric pk")
        parser.add_argument("--version", type=int, default=None,
                            help="publication version to certify (default: current/highest)")
        parser.add_argument("--format", default="json", choices=["json", "html", "txt"],
                            help="output format (default json)")
        parser.add_argument("--out", default=None, help="write to this path (default: stdout)")
        parser.add_argument("--key", default=None,
                            help="private key path (default: $DOGFOOD_AUDIT_KEY or /state)")

    def handle(self, *args, **opts):
        event = _resolve_event(opts["event"])
        if event is None:
            raise CommandError("no event %r (use its ext_id, e.g. evt_01, or numeric pk)"
                               % opts["event"])
        if opts["version"] is None:
            pub = results.current_publication(event)
        else:
            pub = (ResultPublication.objects
                   .filter(event_ext_id=event.ext_id, version=opts["version"]).first())
        if pub is None:
            raise CommandError(
                "no published results to certify for %s%s -- run publish_results first"
                % (event.ext_id, "" if opts["version"] is None else " v%d" % opts["version"]))
        run = NormalizationRun.objects.filter(run_ext_id=pub.run_ext_id).first()
        if run is None:
            raise CommandError("published run %s is missing for %s"
                               % (pub.run_ext_id, event.ext_id))

        # The operator key that signed the run -- same load + fingerprint gate as release_bundle.
        key, created = keys.ensure_private_key(opts["key"])
        pubkey = key.public_key()
        fp = receipts.public_fingerprint(pubkey)
        if fp != run.fingerprint:
            raise CommandError(
                "operator key %s does not match the run's signer %s -- certify from the host that "
                "signed the run." % (fp, run.fingerprint))

        # Self-verify with the SAME offline verifier a judge runs, over a freshly exported bundle:
        # this re-runs the estimator from the pinned ballots and checks the signature + hashes.
        with tempfile.TemporaryDirectory() as d:
            runs.export_bundle(run, pubkey, d)
            ok, checks = verify.verify_bundle(d)
        if not ok:
            failed = "; ".join(n + ((" -- " + det) if det else "")
                               for n, passed, det in checks if not passed)
            raise CommandError("refusing to certify: signed run did not self-verify -- " + failed)

        pem = receipts.public_key_to_pem(pubkey).decode("ascii")
        cert = certificate.build_certificate(pub, run, event_name=event.name, public_key_pem=pem)
        if not certificate.verify_certificate_signature(cert):
            raise CommandError("refusing to certify: certificate signature did not verify")

        rendered = certificate.render(cert, opts["format"])
        if opts["out"]:
            Path(opts["out"]).write_text(rendered, encoding="utf-8")
            self.stdout.write("certificate (%s) -> %s  [run %s, %s v%d, self-verified]%s"
                              % (opts["format"], opts["out"], run.run_ext_id, event.ext_id,
                                 pub.version, " (key generated)" if created else ""))
        else:
            self.stdout.write(rendered)
