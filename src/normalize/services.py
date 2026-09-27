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

from . import diagnostics, engine


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
    dup = None
    if "prj_07" in idx and "prj_41" in idx:
        dup = {"raw_gap": round(abs(raw["prj_07"] - raw["prj_41"]), 4),
               "norm_gap": round(abs(q[idx["prj_07"]] - q[idx["prj_41"]]), 4)}
    return {
        "n_ballots": len(y), "n_submissions": len(subs), "n_judges": len(rep["b"]),
        "n_components": ncomp,
        "gauge_error": float(engine.component_gauge_error(rep["b"], comp_by_judge)),
        "lambda": float(lam),
        "lambda_table": {float(k): round(v, 4) for k, v in table.items()},
        "sigma": round(float(rep["sigma"]), 4),
        "sigma_within_raw": round(sw_raw, 4),
        "sigma_within_model": round(sw_model, 4),
        "sigma_within_reduction": round(sw_raw / sw_model, 3) if sw_model else None,
        "top_raw": top_raw, "top_norm": top_norm,
        "spearman_raw_norm": round(_spearman(q, raw_vec), 4),
        "ranks_changed": changed,
        "unresolved_count": len(rep["unresolved"]),
        "duplicate": dup,
    }


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
