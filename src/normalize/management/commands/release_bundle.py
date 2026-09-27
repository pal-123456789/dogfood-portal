# src/normalize/management/commands/release_bundle.py
"""release_bundle <dir> -- write ONE signed, offline-verifiable RELEASE bundle: the whole chain of
custody in a single directory.

It is the UNION of what `normalize_publish --export` writes (run.json / inputs.json / result.json /
public-key.pem -- the signed, reproducible run) and what `audit_export` writes (checkpoint.json /
audit-prefix.jsonl -- the append-only chain + a signed head), plus ranking.csv (the published
ranking rendered from the signed result), release.json (a manifest), and verification-instructions.txt.
The audit half is produced inline here (mirroring audit_export) rather than shelling out, so the two
halves are guaranteed to share ONE operator key and ONE consistent head.

Before returning it self-verifies with the SAME offline verifier a judge runs -- `normalize.release`
-- so a broken export fails here, not in a judge's hands:
    python -m normalize.release <dir>

Operator/ad-hoc only: NOT wired into boot or the build gate, and OFF the acceptance checker's five
routes, so replay stays 7/7. Needs a normalization run to exist -- run `publish_results` (or
`normalize_publish`) first.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from audit import keys, receipts, service
from audit.hashchain import SCHEMA_VERSION

from normalize import release, results, runs, services
from normalize.models import NormalizationRun, ResultPublication


def _current_run(event):
    """The run behind the current FINAL publication if there is one, else the latest run.

    `pub` is returned only when it actually references the bundled run, so the manifest never claims
    a publication for a run that was exported ad-hoc (via normalize_publish, before any publish).
    """
    pub = results.current_publication(event)
    if pub is not None and pub.status == ResultPublication.FINAL:
        run = NormalizationRun.objects.filter(run_ext_id=pub.run_ext_id).first()
        if run is not None:
            return run, pub
    run = NormalizationRun.objects.filter(event_ext_id=event.ext_id).order_by("-id").first()
    if pub is None or run is None or pub.run_ext_id != run.run_ext_id:
        pub = None
    return run, pub


class Command(BaseCommand):
    help = "Write a signed, offline-verifiable release bundle (ranking + run + audit checkpoint)."

    def add_arguments(self, parser):
        parser.add_argument("dir", help="output directory for the bundle (created if absent)")
        parser.add_argument("--key", default=None,
                            help="private key path (default: $DOGFOOD_AUDIT_KEY or /state)")

    def handle(self, *args, **opts):
        event = services.current_event()
        if event is None:
            raise CommandError("no event configured -- run the seed first")
        run, pub = _current_run(event)
        if run is None:
            raise CommandError(
                "no normalization run for %s -- run publish_results or normalize_publish first"
                % event.ext_id)

        key, created = keys.ensure_private_key(opts["key"])
        pubkey = key.public_key()
        fp = receipts.public_fingerprint(pubkey)
        if fp != run.fingerprint:
            raise CommandError(
                "operator key %s does not match the run's signer %s -- export from the host that "
                "signed the run, or the bundle will not verify." % (fp, run.fingerprint))

        head = service.current_head()
        if head is None:
            raise CommandError("no AuditHead -- run migrate first")
        rows = service.chain_rows()
        d = Path(opts["dir"])
        d.mkdir(parents=True, exist_ok=True)

        # (a) normalize sub-bundle: run.json / inputs.json / result.json / public-key.pem
        runs.export_bundle(run, pubkey, d)

        # (b) audit sub-bundle: audit-prefix.jsonl + a freshly signed checkpoint at the head
        #     (byte-for-byte the audit_export shape; public-key.pem already written above, identical)
        (d / "audit-prefix.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
            encoding="utf-8")
        created_at = timezone.now().isoformat()
        checkpoint = {
            "schema_version": SCHEMA_VERSION, "instance_id": head.instance_id,
            "seq": head.seq, "row_hash": head.row_hash, "created_at": created_at,
            "fingerprint": fp,
            "signature": receipts.sign_checkpoint(
                key, instance_id=head.instance_id, seq=head.seq,
                row_hash=head.row_hash, created_at=created_at),
        }
        (d / "checkpoint.json").write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")

        # (c) published ranking + manifest + human instructions
        (d / "ranking.csv").write_text(release.ranking_csv(run.result), encoding="utf-8")
        manifest = {
            "kind": "dogfood.release-bundle.v1", "event_ext_id": event.ext_id,
            "run_ext_id": run.run_ext_id, "result_hash": run.result_hash,
            "inputs_hash": run.inputs_hash, "fingerprint": run.fingerprint,
            "audit_seq": run.audit_seq, "checkpoint_seq": head.seq,
            "publication_version": (pub.version if pub is not None else None),
            "publication_status": (pub.status if pub is not None else None),
            "files": ["run.json", "inputs.json", "result.json", "public-key.pem",
                      "audit-prefix.jsonl", "checkpoint.json", "ranking.csv",
                      "verification-instructions.txt", "release.json"],
        }
        (d / "release.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        (d / "verification-instructions.txt").write_text(
            _instructions(fingerprint=run.fingerprint, run=run.run_ext_id, seq=head.seq, dir=d),
            encoding="utf-8")

        # (d) self-verify with the very verifier a judge will run
        ok, checks = release.verify_release(d)
        w = self.stdout.write
        for name, passed, detail in checks:
            w("  [%s] %s%s" % ("PASS" if passed else "FAIL", name,
                               (" -- " + detail) if detail else ""))
        if not ok:
            failed = "; ".join(
                name + ((" -- " + detail) if detail else "")
                for name, passed, detail in checks if not passed)
            raise CommandError(
                "release_bundle wrote a bundle that does not self-verify -- failing: " + failed)
        w("release_bundle = OK -> %s%s" % (d, " (key generated)" if created else ""))
        w("  ranking rows=%d  audit rows=%d  run seq=%s  checkpoint seq=%d"
          % (len(run.result.get("rows", [])), len(rows), run.audit_seq, head.seq))
        w("  Pin public-key.pem + fingerprint %s BEFORE judging; verify anywhere with "
          "`python -m normalize.release %s`." % (run.fingerprint, d))


def _instructions(*, fingerprint, run, seq, dir) -> str:
    return (
        "DOGFOOD signed release bundle -- verify it yourself\n"
        "===================================================\n\n"
        "This directory binds, in one signed artifact:\n"
        "  ranking.csv                         the published ranking (human-readable)\n"
        "  run.json / inputs.json / result.json   the signed, reproducible normalization run\n"
        "  checkpoint.json + audit-prefix.jsonl   the append-only audit chain, head signed\n"
        "  public-key.pem                      operator Ed25519 public key (fp %s)\n\n"
        "Verify on any machine with Python 3 + numpy + cryptography (no Django, no database,\n"
        "nothing from this deployment):\n\n"
        "    python -m normalize.release %s\n\n"
        "A PASS means: the audit chain recomputes and its head is signed; the run's signature is\n"
        "valid and its ranking reproduces byte-for-byte from the pinned ballots; ranking.csv is\n"
        "exactly that signed result; and the run's finalization (%s) is recorded as event seq %s\n"
        "inside the signed chain. In short: this CSV came from this run, committed to this\n"
        "signed checkpoint.\n\n"
        "HONEST SCOPE (docs/THREAT-MODEL.md): the operator holds the private key, so a signature\n"
        "is not evidence against the operator by itself -- a key-holder could re-sign a rewritten\n"
        "history, or sign two conflicting ones. This bundle is decisive only if you (an independent\n"
        "party) pinned public-key.pem and its fingerprint BEFORE judging and kept your own copy of\n"
        "the checkpoint to compare against. This tool recomputes and cross-checks; it cannot bind\n"
        "the operator on its own.\n" % (fingerprint, dir, run, seq))
