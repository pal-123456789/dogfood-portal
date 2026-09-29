# src/normalize/services.py
"""Bridge the DB (Ballot + RubricWeight) to the pure-numpy normalization engine.

READ-ONLY, and none of it sits on the acceptance checker's five routes, so nothing here can
move 7/7. engine.py knows nothing about Django; this module is the only meeting point - it
pulls the weighted 0..5 ballots for an event and hands the engine plain lists (y, judge, sub).
"""
from __future__ import annotations

import numpy as np

from events.models import Event
from judging.models import Ballot, RubricWeight
from submissions.models import Submission

from . import diagnostics, duplicates, engine


def current_event():
    """The single seeded event (the checker and the gallery use the same 'oldest row' rule)."""
    return Event.objects.order_by("id").first()


def rubric_weights(event):
    """criterion -> weight from the DB; a missing criterion defaults to 1.0 (equal weights).

    The organizer owns these rows, so a re-weight is a DB change, never a code change; the
    engine's composite reads exactly what is stored.
    """
    rows = {rw.criterion: float(rw.weight) for rw in RubricWeight.objects.filter(event=event)}
    return {c: rows.get(c, 1.0) for c in engine.CRITERIA}


def observed_full(event):
    """(y, judge_keys, sub_keys, vectors): the weighted 0..5 composite of each ballot PLUS its raw
    (functionality, quality, innovation) triple in engine.CRITERIA order.

    judge key = membership ext_id (jdg_01...), submission key = its ext_id (prj_01...); string keys
    keep the engine's matrix columns legible and its output deterministic. `vectors` powers the
    diagnostics' distinct-vector + scale-use stats. observed() delegates here so the composite
    inputs (y, jk, sk) it feeds the leaderboard stay byte-identical - one query, one ordering.
    """
    weights = rubric_weights(event)
    qs = (Ballot.objects
          .filter(assignment__submission__event=event)
          .select_related("assignment__judge", "assignment__submission"))
    y, jk, sk, vectors = [], [], [], []
    for b in qs:
        raw = {"functionality": b.functionality, "quality": b.quality, "innovation": b.innovation}
        y.append(engine.composite(raw, weights))
        jk.append(b.assignment.judge.ext_id)
        sk.append(b.assignment.submission.ext_id)
        vectors.append(tuple(raw[c] for c in engine.CRITERIA))
    return y, jk, sk, vectors


def observed(event):
    """(y, judge_keys, sub_keys): the weighted 0..5 composite of each ballot (see observed_full)."""
    y, jk, sk, _vectors = observed_full(event)
    return y, jk, sk


def _display(event):
    """ext_id -> (title, track_ext_id) for labelling the leaderboard rows."""
    return {s.ext_id: (s.title, s.track.ext_id)
            for s in Submission.objects.filter(event=event).select_related("track")}


def leaderboard(event, n_boot=1000, seed=0, lam=None):
    """Normalized ranking + bootstrap uncertainty for the event, JSON-serializable.

    Both the HTML and the JSON view render this dict. The assembly lives in
    engine.compute_leaderboard -- the SAME pure function the offline verifier (normalize.verify)
    runs -- so a published, signed run reproduces this exact dict byte for byte. Returns rows=[]
    when no ballots exist yet so the page degrades gracefully instead of raising. `lam=None`
    self-selects lambda (the live default); a published run passes its pinned lambda so
    reproduction never re-runs CV.
    """
    y, jk, sk = observed(event)
    if not y:
        return {"rows": [], "n_ballots": 0}
    return engine.compute_leaderboard(y, jk, sk, _display(event),
                                      n_boot=n_boot, seed=seed, lam=lam)

def _spearman(a, b):
    """Rank correlation with no scipy dependency: Pearson on the rank-transformed vectors."""
    ar = np.argsort(np.argsort(a)).astype(float)
    br = np.argsort(np.argsort(b)).astype(float)
    return float(np.corrcoef(ar, br)[0, 1])


def proof_report(event, n_boot=1000, seed=0):
    """The honest diagnostic bundle behind the write-up + ablation table (real numbers).

    Everything here is derivable from the observed ballots alone - ground truth is never read,
    so nothing can leak into lambda or the ranking. This is what the normalize_report command
    prints and what the ablation table quotes; no number in the write-up is hand-entered.
    """
    y, jk, sk = observed(event)
    comp_by_sub, comp_by_judge, ncomp = engine.connected_components(jk, sk)
    lam, table = engine.select_lambda(y, jk, sk, seed=seed)
    rep = engine.rank_report(y, jk, sk, lam=lam, n_boot=n_boot, seed=seed)
    subs = rep["subs"]
    idx = {s: i for i, s in enumerate(subs)}
    q = rep["q"]
    raw = engine.raw_means(y, sk)
    raw_vec = np.array([raw[s] for s in subs])
    top_raw = max(subs, key=lambda s: raw[s])
    top_norm = subs[int(np.argmax(q))]
    raw_order = sorted(range(len(subs)), key=lambda i: -raw_vec[i])
    norm_order = list(np.argsort(-q))
    changed = sum(1 for a, b in zip(raw_order, norm_order) if a != b)
    pred = np.array([q[idx[s]] + rep["b"][j] for s, j in zip(sk, jk)])
    yv = np.asarray(y, dtype=float)
    raw_resid = np.array([v - raw[s] for v, s in zip(y, sk)])
    sw_raw = float(raw_resid.std(ddof=1))
    sw_model = float((yv - pred).std(ddof=1))
    jmeans = np.array(list(engine.judge_means(y, jk).values()))
    raw_judge_spread = float(jmeans.std(ddof=1)) if jmeans.size > 1 else 0.0
    dup = None
    if "prj_07" in idx and "prj_41" in idx:
        dup = {"raw_gap": round(abs(raw["prj_07"] - raw["prj_41"]), 4),
               "norm_gap": round(abs(q[idx["prj_07"]] - q[idx["prj_41"]]), 4)}
    sig = engine.signal_test(y, jk, sk, lam=lam, seed=seed)
    return {
        "n_ballots": len(y), "n_submissions": len(subs), "n_judges": len(rep["b"]),
        "n_components": ncomp,
        "gauge_error": float(engine.component_gauge_error(rep["b"], comp_by_judge)),
        "lambda": float(lam),
        "lambda_table": {float(k): round(v, 4) for k, v in table.items()},
        "sigma": round(float(rep["sigma"]), 4),
        "raw_judge_spread": round(raw_judge_spread, 4),
        "sigma_within_raw": round(sw_raw, 4),
        "sigma_within_model": round(sw_model, 4),
        "sigma_within_reduction": round(sw_raw / sw_model, 3) if sw_model else None,
        "top_raw": top_raw, "top_norm": top_norm,
        "spearman_raw_norm": round(_spearman(q, raw_vec), 4),
        "ranks_changed": changed,
        "unresolved_count": len(rep["unresolved"]),
        # Honest credibility layer -- display only, never fed back into q or the signed ranking:
        # the no-signal permutation p-value and a typical/worst per-rank standard error.
        "signal_p": sig["p_value"],
        "signal_observed": sig["observed"],
        "signal_perm_p95": sig["perm_p95"],
        "signal_significant": sig["significant"],
        "q_se_median": round(float(np.median(rep["q_se"])), 4),
        "q_se_max": round(float(np.max(rep["q_se"])), 4),
        "duplicate": dup,
    }


def pairwise_report(event, n_boot=400, top_k=8, seed=0):
    """Model-based P(A outranks B) for the top contenders -- a LIVE recompute, never signed.

    Reuses the SAME parametric bootstrap as the leaderboard's rank intervals: engine.rank_report
    already returns `win_prob`, the fraction of bootstrap draws in which q_i exceeds q_j (its exact
    definition of "i outranks j"), so no new sampler and no new random model is introduced here. We
    only reorder that matrix into current normalized rank order and keep the top_k rows.

    Returns {labels, matrix, n_boot, top_k, unresolved_pairs}:
      * labels -- "ext_id - title" for the top_k submissions, best-q first (the order the leaderboard
        ranks by);
      * matrix -- matrix[i][j] = P(labels[i] outranks labels[j]) in [0, 1]; the diagonal is 0.0;
      * unresolved_pairs -- (i, j) with i < j whose order the data cannot resolve, using the SAME
        pre-registered band the leaderboard uses: win-prob in engine.UNRESOLVED_PROB ([0.10, 0.90])
        OR |q_i - q_j| < engine.UNRESOLVED_GAP (0.25);
      * top_k -- the number of contenders actually shown (min of the requested top_k and the field).
    Returns empty labels/matrix (no bootstrap run) when the event has no ballots yet, so the view can
    render a graceful empty state.
    """
    y, jk, sk = observed(event)
    if not y:
        return {"labels": [], "matrix": [], "n_boot": n_boot, "top_k": 0, "unresolved_pairs": []}
    rep = engine.rank_report(y, jk, sk, n_boot=n_boot, seed=seed)
    subs, q, win = rep["subs"], rep["q"], rep["win_prob"]
    order = sorted(range(len(subs)), key=lambda i: -float(q[i]))   # normalized rank order, best first
    top = order[:top_k]
    disp = _display(event)
    labels = ["%s - %s" % (subs[i], disp.get(subs[i], (subs[i], ""))[0]) for i in top]
    matrix = [[float(win[a][b]) for b in top] for a in top]
    q_top = [float(q[i]) for i in top]
    lo, hi = engine.UNRESOLVED_PROB
    unresolved_pairs = [
        (a, b)
        for a in range(len(top))
        for b in range(a + 1, len(top))
        if (lo <= matrix[a][b] <= hi) or abs(q_top[a] - q_top[b]) < engine.UNRESOLVED_GAP
    ]
    return {"labels": labels, "matrix": matrix, "n_boot": rep["n_boot"],
            "top_k": len(top), "unresolved_pairs": unresolved_pairs}


def diagnostics_report(event, lam=None, seed=0):
    """Organizer review-diagnostics panel for the event (see normalize.diagnostics -- NOT fraud
    detection): leave-one-ballot-out residuals, single-ballot + single-judge decision influence,
    graph coverage / articulation judges, and low-discrimination + scale-use stats.

    READ-ONLY and off the acceptance checker's five routes, so it cannot move replay. Self-selects
    lambda by CV on observed ballots (the live default) unless a run's lambda is pinned. Returns
    n_ballots=0 when nothing is recorded yet so the page degrades instead of raising.
    """
    y, jk, sk, vectors = observed_full(event)
    if not y:
        return {"n_ballots": 0, "scope": "review diagnostics, not fraud detection"}
    return diagnostics.report(y, jk, sk, _display(event), vectors=vectors, lam=lam, seed=seed)


def duplicate_clusters(event):
    """Display-only within-track duplicate-title diagnostic for the review panel (see
    normalize.duplicates -- NOT fraud detection). Scopes to the event's SUBMITTED submissions -- the
    same draft/withdrawn-excluded set the public gallery and API use -- builds plain
    {ext_id, team, track, title} rows, and hands them to the pure detector.

    READ-ONLY and independent of ballots (submissions cluster before any judging), so it can never
    move the signed result, the leaderboard, or any hash. Returns [] when nothing is flagged.
    """
    subs = (Submission.objects
            .filter(event=event, state=Submission.SUBMITTED)
            .select_related("team", "track"))
    rows = [{"ext_id": s.ext_id, "team": s.team.ext_id,
             "track": s.track.ext_id, "title": s.title} for s in subs]
    return duplicates.find_duplicate_clusters(rows)
