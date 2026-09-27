# src/normalize/management/commands/normalize_report.py
"""Print the honest normalization proof numbers for the current event.

Reproducibility contract: every figure in docs/JUDGING and the ablation table is emitted by
this command, so the write-up can never drift from the code. Run: `manage.py normalize_report`
(add --json for the raw dict, --boot N to change bootstrap draws).
"""
import json

from django.core.management.base import BaseCommand

from normalize import services


class Command(BaseCommand):
    help = "Print normalization proof diagnostics (components, gauge, lambda sweep, winners)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="emit the raw dict as JSON")
        parser.add_argument("--boot", type=int, default=1000, help="bootstrap draws")

    def handle(self, *args, **opts):
        event = services.current_event()
        if event is None:
            self.stderr.write("no event configured")
            return
        rep = services.proof_report(event, n_boot=opts["boot"])
        if opts["json"]:
            self.stdout.write(json.dumps(rep, indent=2, sort_keys=True))
            return
        w = self.stdout.write
        w("DOGFOOD normalization proof - event %s" % event.ext_id)
        w("  ballots=%d  submissions=%d  judges=%d  components=%d"
          % (rep["n_ballots"], rep["n_submissions"], rep["n_judges"], rep["n_components"]))
        w("  gauge_error = %.2e   (per-component mean(b)=0 to machine precision)"
          % rep["gauge_error"])
        w("  lambda (CV on observed ballots only) = %g" % rep["lambda"])
        w("  lambda sweep (mean held-out RMSE):")
        for lam in sorted(rep["lambda_table"]):
            mark = "   <- selected" if lam == rep["lambda"] else ""
            w("      %-9g  %.4f%s" % (lam, rep["lambda_table"][lam], mark))
        w("  sigma (ballot noise) = %.4f" % rep["sigma"])
        w("  raw judge spread (stdev of per-judge mean composite) = %.4f"
          % rep["raw_judge_spread"])
        w("      (fixture's pre-normalization severity spread; an UPPER BOUND on removable")
        w("       severity - it conflates true leniency with each judge's assignment mix)")
        w("  within-submission sigma: raw=%.4f  model=%.4f  reduction=%.3fx"
          % (rep["sigma_within_raw"], rep["sigma_within_model"], rep["sigma_within_reduction"]))
        w("  top-1 by raw mean     = %s" % rep["top_raw"])
        w("  top-1 by normalized q = %s" % rep["top_norm"])
        w("  spearman(raw, norm) = %.4f   positions changed = %d/%d"
          % (rep["spearman_raw_norm"], rep["ranks_changed"], rep["n_submissions"]))
        w("  unresolved adjacent pairs = %d/%d"
          % (rep["unresolved_count"], rep["n_submissions"] - 1))
        if rep["duplicate"]:
            d = rep["duplicate"]
            w("  duplicate control prj_07/prj_41 gap: raw=%.4f  normalized=%.4f"
              % (d["raw_gap"], d["norm_gap"]))
