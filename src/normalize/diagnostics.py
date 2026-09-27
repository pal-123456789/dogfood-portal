# src/normalize/diagnostics.py
"""Review DIAGNOSTICS for a judging panel - pure numpy, zero Django (a sibling of engine.py).

WHAT THIS IS
    A robustness + coverage panel an organizer reads BEFORE trusting a ranking: which ballots the
    additive model predicts poorly, how much any single ballot or judge moves the decision, where
    the comparison graph is thin or hangs on one judge, and which judges contribute no separation.

WHAT THIS IS NOT
    NOT fraud detection. Nothing here scores anyone as dishonest, computes a fraud probability, runs
    Benford / clustering / isolation-forest, or downweights or excludes any ballot or judge. Every
    output is a DESCRIPTIVE prompt for human review. On this data the power is low by construction
    (N is small, scores are bounded 1..5, judge overlap is sparse), so a flag means "look here", not
    "act here". Removing or reweighting a judge stays an organizer decision, made in the open.

    All thresholds are PRE-REGISTERED here as module constants - fixed before any event's data is
    seen - exactly like engine.UNRESOLVED_*, so a flag can never be a knob tuned to a wanted answer.
"""
from __future__ import annotations

from collections import Counter

import numpy as np

from . import engine

# --- Pre-registered thresholds (fixed BEFORE seeing any data) ---------------------------------
RESIDUAL_Z = 3.0           # |leave-one-ballot-out standardized residual| at/above this = "surprising"
INFLUENCE_RANK_SHIFT = 5   # a submission moving this many ranks when one ballot/judge is dropped
INFLUENCE_SCORE_SHIFT = 0.25   # ...or its quality q moving this much
LOWDISC_MIN_BALLOTS = 4    # a judge needs at least this many ballots before "low discrimination" means anything
THIN_COVERAGE = 2          # a submission with at most this many ballots is thinly covered
_SIGMA_FLOOR = 1e-9        # below this the model fits to machine precision: no noise scale, so no residual is "surprising"
_EPS = 1e-12


def _point_order(q_by_sub):
    """Submissions best-first by point quality; ext_id tie-break keeps it deterministic."""
    return sorted(q_by_sub, key=lambda s: (-q_by_sub[s], s))


def _fit_excluding(y, jk, sk, drop, lam):
    """Refit q,b on all ballots whose index is NOT in `drop` (a set), at the SAME pinned lambda.

    Returns (q_by_sub, b_by_judge), or (None, None) if nothing is left. A submission or judge that
    loses all its ballots simply does not appear in the returned dicts (its effect is unestimated,
    never guessed) - callers restrict any comparison to the keys that survive.
    """
    keep = [k for k in range(len(y)) if k not in drop]
    if not keep:
        return None, None
    yy = y[keep]
    jj = [jk[k] for k in keep]
    ss = [sk[k] for k in keep]
    return engine.fit(yy, jj, ss, lam)


def _influence(q_red, q0, base_order):
    """How a refit ranking (q_red) differs from the baseline (q0), over the submissions in BOTH.

    Ranks are compared only among submissions common to both fits (a dropped submission cannot have
    a rank), so a vanished project never manufactures a phantom shift; it is reported separately as
    `dropped`. Returns (max_rank_shift, max_score_shift, winner_changed, top3_changed, dropped).
    """
    common = [s for s in base_order if s in q_red]          # baseline relative order, common subs
    red_seq = [s for s in _point_order(q_red) if s in q_red and s in set(common)]
    b_rank = {s: i for i, s in enumerate(common)}
    r_rank = {s: i for i, s in enumerate(red_seq)}
    max_rank = max((abs(b_rank[s] - r_rank[s]) for s in common), default=0)
    max_score = max((abs(q0[s] - q_red[s]) for s in common), default=0.0)
    winner_changed = bool(common and red_seq and common[0] != red_seq[0])
    top3_changed = set(common[:3]) != set(red_seq[:3])
    dropped = [s for s in base_order if s not in q_red]
    return int(max_rank), float(max_score), winner_changed, top3_changed, dropped


def _is_influential(max_rank, max_score, winner_changed, top3_changed, dropped):
    """A single-drop is worth surfacing if it crosses ANY pre-registered decision threshold."""
    return bool(winner_changed or top3_changed or dropped
                or max_rank >= INFLUENCE_RANK_SHIFT or max_score >= INFLUENCE_SCORE_SHIFT)


def _label(display, s):
    return display.get(s, (s, ""))[0]


def _residuals_and_ballot_influence(y, jk, sk, lam, sigma, q0, base_order, display):
    """One leave-one-BALLOT-out refit per ballot, reused for TWO diagnostics.

    Residual: predict the held-out ballot from the refit (q'+b') and standardize by the pinned
    model sigma; |z| >= RESIDUAL_Z is a ballot the model explains poorly (possible mis-entry or a
    genuine dissenting read - a review prompt, never an accusation). Skipped when dropping the
    ballot orphans its submission or judge (then the prediction is undefined, not guessed).
    Influence: does removing this one ballot alone move the decision? Almost never - the value is
    the aggregate 'no single ballot changes the winner', plus any rare ballot that does.
    """
    n = len(y)
    resid_flags, n_eval, n_uneval, max_abs_z = [], 0, 0, 0.0
    b_winner, b_top3, b_maxrank, b_maxscore, ballot_flags = 0, 0, 0, 0.0, []
    meaningful_sigma = sigma > _SIGMA_FLOOR
    for k in range(n):
        qk, bk = _fit_excluding(y, jk, sk, {k}, lam)
        if qk is None:
            n_uneval += 1
            continue
        s, j = sk[k], jk[k]
        if s in qk and j in bk:
            z = float((y[k] - (qk[s] + bk[j])) / sigma) if meaningful_sigma else 0.0
            n_eval += 1
            max_abs_z = max(max_abs_z, abs(z))
            if meaningful_sigma and abs(z) >= RESIDUAL_Z:
                resid_flags.append({"judge": j, "submission": s, "title": _label(display, s),
                                    "score": round(float(y[k]), 4),
                                    "predicted": round(float(qk[s] + bk[j]), 4), "z": round(z, 2)})
        else:
            n_uneval += 1
        mr, ms, wc, t3, dropped = _influence(qk, q0, base_order)
        b_winner += int(wc); b_top3 += int(t3)
        b_maxrank = max(b_maxrank, mr); b_maxscore = max(b_maxscore, ms)
        if _is_influential(mr, ms, wc, t3, dropped):
            ballot_flags.append({"judge": j, "submission": s, "title": _label(display, s),
                                 "max_rank_shift": mr, "max_score_shift": round(ms, 4),
                                 "winner_changed": wc, "top3_changed": t3, "dropped": dropped})
    resid_flags.sort(key=lambda r: -abs(r["z"]))
    residuals = {"evaluated": n_eval, "unevaluable": n_uneval, "sigma": round(sigma, 4),
                 "z_threshold": RESIDUAL_Z, "max_abs_z": round(max_abs_z, 2),
                 "flagged_count": len(resid_flags), "flagged": resid_flags}
    ballot_influence = {"evaluated": n, "winner_changes": b_winner,
                        "top3_changes": b_top3, "max_rank_shift": b_maxrank,
                        "max_score_shift": round(b_maxscore, 4),
                        "flagged_count": len(ballot_flags), "flagged": ballot_flags}
    return residuals, ballot_influence


def _judge_influence(y, jk, sk, lam, q0, base_order, by_judge, display):
    """Leave-one-JUDGE-out: drop ALL of one judge's ballots, refit, measure the decision move.

    A judge is a bloc, so this is the influence that actually matters. Every judge is reported
    (the panel is small); `flagged` is the subset crossing a pre-registered threshold. This says
    how fragile the ranking is to any one judge - not whether a judge did anything wrong.
    """
    rows, winner_changes, top3_changes, max_rank, max_score = [], 0, 0, 0, 0.0
    for j in sorted(by_judge):
        qj, _bj = _fit_excluding(y, jk, sk, set(by_judge[j]), lam)
        if qj is None:
            rows.append({"judge": j, "n_ballots": len(by_judge[j]), "removed_everything": True})
            continue
        mr, ms, wc, t3, dropped = _influence(qj, q0, base_order)
        winner_changes += int(wc); top3_changes += int(t3)
        max_rank = max(max_rank, mr); max_score = max(max_score, ms)
        rows.append({"judge": j, "n_ballots": len(by_judge[j]), "max_rank_shift": mr,
                     "max_score_shift": round(ms, 4), "winner_changed": wc, "top3_changed": t3,
                     "dropped_submissions": [{"submission": s, "title": _label(display, s)}
                                             for s in dropped],
                     "flagged": _is_influential(mr, ms, wc, t3, dropped)})
    rows.sort(key=lambda r: (-r.get("max_score_shift", 0.0), r["judge"]))
    return {"n_judges": len(by_judge), "winner_changes": winner_changes,
            "top3_changes": top3_changes, "max_rank_shift": max_rank,
            "max_score_shift": round(max_score, 4),
            "flagged_count": sum(1 for r in rows if r.get("flagged")), "judges": rows}


def _coverage(y, jk, sk, by_judge, display):
    """Graph coverage: components, thin submissions, load spread, and articulation judges.

    An articulation judge is one whose removal isolates a submission (no other reviewer) or splits
    a comparison component - the ranking of the affected projects then hangs entirely on that one
    judge. On a healthy panel this list is empty, which is itself the reassuring result.
    """
    comp_by_sub, comp_by_judge, ncomp = engine.connected_components(jk, sk)
    sub_counts, judge_counts = Counter(sk), Counter(jk)
    bps, bpj = sorted(sub_counts.values()), sorted(judge_counts.values())
    thin = [{"submission": s, "title": _label(display, s), "n_ballots": sub_counts[s]}
            for s in sorted(sub_counts) if sub_counts[s] <= THIN_COVERAGE]
    comps = {}
    for s, c in comp_by_sub.items():
        comps.setdefault(c, {"component": c, "n_submissions": 0, "n_judges": 0})["n_submissions"] += 1
    for j, c in comp_by_judge.items():
        comps.setdefault(c, {"component": c, "n_submissions": 0, "n_judges": 0})["n_judges"] += 1
    artic = []
    for j in sorted(by_judge):
        keep = [k for k in range(len(y)) if jk[k] != j]
        if not keep:
            continue
        ss = [sk[k] for k in keep]
        _cbs, _cbj, nc2 = engine.connected_components([jk[k] for k in keep], ss)
        isolated = sorted(set(sk) - set(ss))
        if isolated or nc2 > ncomp:
            artic.append({"judge": j, "components_before": ncomp, "components_after": nc2,
                          "isolated_submissions": [{"submission": s, "title": _label(display, s)}
                                                   for s in isolated]})
    return {"n_submissions": len(sub_counts), "n_judges": len(judge_counts), "n_ballots": len(y),
            "n_components": ncomp, "components": [comps[c] for c in sorted(comps)],
            "ballots_per_submission": {"min": bps[0], "median": int(np.median(bps)), "max": bps[-1]},
            "ballots_per_judge": {"min": bpj[0], "median": int(np.median(bpj)), "max": bpj[-1]},
            "thin_threshold": THIN_COVERAGE, "thin_submissions": thin,
            "articulation_judges": artic}


def _low_discrimination(y, by_judge, vectors):
    """Judges (with enough ballots) whose scores are all identical - they add no separation.

    RELABELLED from the older 'zero-variance' idea: the point is descriptive - such a judge's
    severity offset still fits, and NOTHING is excluded or downweighted (that stays A6 design-only,
    an organizer call). `distinct_vectors` (when the raw triples are supplied) shows whether it is
    literally one repeated (functionality, quality, innovation) vector or a constant composite.
    """
    out = []
    for j in sorted(by_judge):
        idxs = by_judge[j]
        if len(idxs) < LOWDISC_MIN_BALLOTS:
            continue
        col = y[idxs]
        if float(col.std()) <= _EPS:
            distinct = len({tuple(vectors[k]) for k in idxs}) if vectors is not None else None
            out.append({"judge": j, "n_ballots": len(idxs),
                        "composite_value": round(float(col[0]), 4),
                        "distinct_vectors": distinct})
    return out


def _rubric_use(vectors):
    """Descriptive scale-use stats (explicitly allowed): per-criterion mean/std + a 1..5 histogram.

    Shows whether judges use the whole rubric range or bunch up - context for reading the panel,
    not a flag. Returns None when the raw criterion vectors were not supplied.
    """
    if not vectors:
        return None
    arr = np.asarray(vectors, dtype=float)
    by_crit = {c: {"mean": round(float(arr[:, i].mean()), 3), "std": round(float(arr[:, i].std()), 3)}
               for i, c in enumerate(engine.CRITERIA)}
    hist = Counter(int(v) for row in vectors for v in row)
    return {"by_criterion": by_crit, "scale_use": {str(k): hist.get(k, 0) for k in range(1, 6)}}


def report(y, judge_keys, sub_keys, display=None, vectors=None, lam=None, seed=0):
    """Assemble the full diagnostics panel. Pure + deterministic given lam (no bootstrap here).

    `vectors` (optional) is the list of raw (functionality, quality, innovation) triples aligned to
    y - it powers distinct-vector and rubric-use stats; everything else needs only the composite y.
    lam is self-selected by CV on observed ballots when None (the live default), else pinned.
    """
    display = display or {}
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n == 0:
        return {"n_ballots": 0, "scope": "review diagnostics, not fraud detection"}
    jk, sk = list(judge_keys), list(sub_keys)
    if lam is None:
        lam, _ = engine.select_lambda(y, jk, sk, seed=seed)
    q0, b0 = engine.fit(y, jk, sk, lam)
    pred0 = np.array([q0[s] + b0[j] for s, j in zip(sk, jk)])
    sigma = float(np.sqrt(np.sum((y - pred0) ** 2) / max(n - (len(q0) + len(b0)), 1)))
    base_order = _point_order(q0)
    by_judge = {}
    for k, j in enumerate(jk):
        by_judge.setdefault(j, []).append(k)
    residuals, ballot_influence = _residuals_and_ballot_influence(
        y, jk, sk, lam, sigma, q0, base_order, display)
    return {
        "scope": "review diagnostics, not fraud detection",
        "notes": [
            "Thresholds are pre-registered (fixed before data): |z|>=%g, rank shift>=%d, "
            "score shift>=%g, low-discrimination n>=%d."
            % (RESIDUAL_Z, INFLUENCE_RANK_SHIFT, INFLUENCE_SCORE_SHIFT, LOWDISC_MIN_BALLOTS),
            "Flags are prompts for human review, not accusations; nothing here downweights or "
            "excludes any ballot or judge.",
            "Low power by construction: N=%d ballots, scores bounded 1..5, sparse judge overlap."
            % n,
        ],
        "thresholds": {"residual_z": RESIDUAL_Z, "influence_rank_shift": INFLUENCE_RANK_SHIFT,
                       "influence_score_shift": INFLUENCE_SCORE_SHIFT,
                       "lowdisc_min_ballots": LOWDISC_MIN_BALLOTS, "thin_coverage": THIN_COVERAGE},
        "n_ballots": n, "lambda": float(lam), "sigma": round(sigma, 4),
        "winner": base_order[0], "winner_title": _label(display, base_order[0]),
        "residuals": residuals,
        "ballot_influence": ballot_influence,
        "judge_influence": _judge_influence(y, jk, sk, lam, q0, base_order, by_judge, display),
        "coverage": _coverage(y, jk, sk, by_judge, display),
        "low_discrimination": _low_discrimination(y, by_judge, vectors),
        "rubric_use": _rubric_use(vectors),
    }
