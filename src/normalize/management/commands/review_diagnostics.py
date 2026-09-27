# src/normalize/management/commands/review_diagnostics.py
"""Print the review-diagnostics panel for the current event (see normalize.diagnostics).

Review diagnostics, NOT fraud detection: leave-one-ballot-out residuals, single-ballot and
single-judge decision influence, graph coverage / articulation judges, and low-discrimination +
scale-use stats. Thresholds are pre-registered in code. Run: `manage.py review_diagnostics`
(add --json for the raw dict, --boot is accepted but unused -- diagnostics need no bootstrap).
"""
import json

from django.core.management.base import BaseCommand

from normalize import services


class Command(BaseCommand):
    help = "Print review diagnostics (residuals, decision influence, coverage). NOT fraud detection."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="emit the raw dict as JSON")

    def handle(self, *args, **opts):
        event = services.current_event()
        if event is None:
            self.stderr.write("no event configured")
            return
        rep = services.diagnostics_report(event)
        if opts["json"]:
            self.stdout.write(json.dumps(rep, indent=2, sort_keys=True, default=str))
            return
        w = self.stdout.write
        if not rep.get("n_ballots"):
            w("no ballots recorded yet")
            return
        cov, res = rep["coverage"], rep["residuals"]
        bi, ji = rep["ballot_influence"], rep["judge_influence"]
        w("DOGFOOD review diagnostics - event %s   (NOT fraud detection)" % event.ext_id)
        w("  winner (point q) = %s (%s)" % (rep["winner_title"], rep["winner"]))
        w("  ballots=%d  projects=%d  judges=%d  components=%d  lambda=%g  sigma=%.4f"
          % (rep["n_ballots"], cov["n_submissions"], cov["n_judges"], cov["n_components"],
             rep["lambda"], rep["sigma"]))
        w("  ballots/project min=%d median=%d max=%d   ballots/judge min=%d median=%d max=%d"
          % (cov["ballots_per_submission"]["min"], cov["ballots_per_submission"]["median"],
             cov["ballots_per_submission"]["max"], cov["ballots_per_judge"]["min"],
             cov["ballots_per_judge"]["median"], cov["ballots_per_judge"]["max"]))
        w("  articulation judges = %d   thin projects = %d"
          % (len(cov["articulation_judges"]), len(cov["thin_submissions"])))
        w("  surprising ballots |z|>=%g : %d flagged / %d evaluated (max |z|=%.2f)"
          % (res["z_threshold"], res["flagged_count"], res["evaluated"], res["max_abs_z"]))
        for r in res["flagged"]:
            w("      %s on %s: score=%.2f pred=%.2f z=%+.2f"
              % (r["judge"], r["submission"], r["score"], r["predicted"], r["z"]))
        w("  drop-one-ballot: winner changes=%d top3 changes=%d max rank shift=%d max dq=%.4f"
          % (bi["winner_changes"], bi["top3_changes"], bi["max_rank_shift"], bi["max_score_shift"]))
        w("  drop-one-judge:  winner changes=%d top3 changes=%d max rank shift=%d max dq=%.4f (%d flagged)"
          % (ji["winner_changes"], ji["top3_changes"], ji["max_rank_shift"],
             ji["max_score_shift"], ji["flagged_count"]))
        if rep["low_discrimination"]:
            w("  low-discrimination judges: %s"
              % ", ".join("%s(n=%d)" % (j["judge"], j["n_ballots"]) for j in rep["low_discrimination"]))
