# src/normalize/engine.py
"""Score-normalization engine for DOGFOOD judging - pure numpy, zero Django imports.

WHY THIS EXISTS
    Judges disagree on level. A harsh judge's 3 and a lenient judge's 4 can reflect the
    same underlying quality; ranking on raw means rewards whoever drew the kind judges.
    We fit an additive model that separates project quality from judge severity:

        y_ij = q_i + b_j + e_ij          (y = one judge's weighted 0..5 ballot)

    q_i is submission i's quality, b_j is judge j's severity offset, e is noise.

THE ESTIMATOR (ridge == partial pooling)
    Minimise   sum (y_ij - q_i - b_j)^2  +  lambda * sum_j b_j^2 .
    Only the judge effects b are penalised; q is free. Penalising b alone is what pins the
    otherwise-free location: within every connected component of the judge<->submission
    graph the fit gives mean(b)=0, so q lands on the ballot scale. That gauge is a property
    of the penalty, not of lambda - lambda only shrinks the magnitude of b.

TWO FACTS THAT SHAPE THE CODE
    * lambda=0 is singular (the location null space is unpenalised), so LAMBDA_GRID starts
      above zero. A solver handed 0 would raise, or an lstsq fallback would silently pick a
      different gauge and shift q off the 1..5 scale.
    * The gauge is per CONNECTED COMPONENT. If the graph splits, each piece has its own free
      level and cross-piece ranking is NOT identified by ratings alone.
"""
from __future__ import annotations

import numpy as np

CRITERIA = ("functionality", "quality", "innovation")

# Ridge grid: excludes 0 (singular); brackets the CV minimum on data of this shape (~10).
LAMBDA_GRID = (0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0, 30.0)

# Pre-registered "the data cannot resolve this" band, fixed BEFORE seeing any result.
UNRESOLVED_PROB = (0.10, 0.90)   # a pairwise win-prob inside this band is a coin-flip
UNRESOLVED_GAP = 0.25            # ...or a quality gap smaller than this is not meaningful

def composite(raw, weights):
    """Weighted mean of one ballot's criteria -> a single 0..5 score.

    `raw` maps criterion -> int 1..5; `weights` maps criterion -> float. Equal weights (the
    seeded default) make this the plain average. Weights come from DB RubricWeight rows, so
    an organizer can re-weight without a code change; nothing here is hard-coded.
    """
    num = sum(float(weights[c]) * float(raw[c]) for c in CRITERIA)
    den = sum(float(weights[c]) for c in CRITERIA)
    if den == 0:
        raise ValueError("rubric weights sum to zero")
    return num / den


def _index(keys):
    """Ordered-unique index map -> deterministic, stable matrix columns."""
    uniq = sorted(set(keys))
    return {k: i for i, k in enumerate(uniq)}, uniq


def connected_components(judge_keys, sub_keys):
    """Union-find over the bipartite judge<->submission graph the ballots induce.

    Returns (comp_by_sub, comp_by_judge, n_components); the comp_* dicts map each id to a
    0-based component label. Cross-component quality is not comparable on ratings alone; the
    real DOGFOOD fixture forms a single component, so its ranking is global.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:                 # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for j, s in zip(judge_keys, sub_keys):
        union(("J", j), ("S", s))
    roots = {}

    def label(x):
        return roots.setdefault(find(x), len(roots))

    comp_by_sub = {s: label(("S", s)) for s in sorted(set(sub_keys))}
    comp_by_judge = {j: label(("J", j)) for j in sorted(set(judge_keys))}
    return comp_by_sub, comp_by_judge, len(roots)


def fit(y, judge_keys, sub_keys, lam):
    """One ridge/partial-pooling solve. Returns (q_by_sub, b_by_judge).

    Builds the (n_sub+n_judge) normal equations directly - the matrix is tiny (71x71 for the
    real fixture) - and solves. For lam>0 it is positive-definite, so np.linalg.solve is
    exact and no lstsq/gauge fallback is ever taken. Penalty falls on b only, which fixes the
    per-component location at mean(b)=0.
    """
    if lam <= 0:
        raise ValueError("lambda must be > 0; lambda=0 is singular for this model")
    y = np.asarray(y, dtype=float)
    s_map, subs = _index(sub_keys)
    j_map, judges = _index(judge_keys)
    n_sub, n_judge = len(subs), len(judges)
    s_idx = np.fromiter((s_map[s] for s in sub_keys), dtype=int, count=len(sub_keys))
    j_idx = np.fromiter((j_map[j] for j in judge_keys), dtype=int, count=len(judge_keys))
    p = n_sub + n_judge
    A = np.zeros((p, p))
    np.add.at(A, (s_idx, s_idx), 1.0)                     # q diagonal = #ballots per sub
    np.add.at(A, (n_sub + j_idx, n_sub + j_idx), 1.0)     # b diagonal = #ballots per judge
    np.add.at(A, (s_idx, n_sub + j_idx), 1.0)             # q<->b cross terms (symmetric)
    np.add.at(A, (n_sub + j_idx, s_idx), 1.0)
    A[np.arange(n_sub, p), np.arange(n_sub, p)] += lam    # ridge penalty on b only
    rhs = np.zeros(p)
    np.add.at(rhs, s_idx, y)
    np.add.at(rhs, n_sub + j_idx, y)
    theta = np.linalg.solve(A, rhs)
    q_by_sub = {subs[i]: float(theta[i]) for i in range(n_sub)}
    b_by_judge = {judges[i]: float(theta[n_sub + i]) for i in range(n_judge)}
    return q_by_sub, b_by_judge


def raw_means(y, sub_keys):
    """Baseline every un-normalized leaderboard uses: the plain per-submission mean ballot."""
    acc, cnt = {}, {}
    for val, s in zip(y, sub_keys):
        acc[s] = acc.get(s, 0.0) + float(val)
        cnt[s] = cnt.get(s, 0) + 1
    return {s: acc[s] / cnt[s] for s in acc}


def component_gauge_error(b_by_judge, comp_by_judge):
    """Max |mean(b)| over components - ~0 (machine precision) by construction, a self-check."""
    sums, counts = {}, {}
    for j, val in b_by_judge.items():
        c = comp_by_judge[j]
        sums[c] = sums.get(c, 0.0) + val
        counts[c] = counts.get(c, 0) + 1
    return max(abs(sums[c] / counts[c]) for c in sums)


def select_lambda(y, judge_keys, sub_keys, grid=LAMBDA_GRID, folds=5, repeats=5, seed=0):
    """Choose lambda by repeated K-fold CV on the OBSERVED ballots (held-out RMSE of q+b).

    Ground truth is never consulted, so the choice cannot leak. A held-out ballot is scored
    only when both its submission and its judge also appear in training (otherwise that effect
    is unestimated); such orphans are skipped, not guessed. Averaging over `repeats` distinct
    fold partitions de-noises the pick so it does not hinge on one lucky split (a single split
    on ~4 ballots/judge is noisy enough to pin lambda at a grid edge). Returns
    (best_lambda, {lam: mean_rmse}).
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    best, table = None, {}
    for lam in grid:
        rmses = []
        for rep in range(repeats):
            rng = np.random.default_rng(seed + rep)
            parts = np.array_split(rng.permutation(n), folds)
            sse, cnt = 0.0, 0
            for f in range(folds):
                test = parts[f]
                train = np.concatenate([parts[g] for g in range(folds) if g != f])
                q, b = fit(y[train], [judge_keys[k] for k in train],
                           [sub_keys[k] for k in train], lam)
                for k in test:
                    s, j = sub_keys[k], judge_keys[k]
                    if s in q and j in b:
                        sse += (y[k] - (q[s] + b[j])) ** 2
                        cnt += 1
            rmses.append(np.sqrt(sse / cnt) if cnt else np.inf)
        rmse = float(np.mean(rmses))
        table[lam] = rmse
        if best is None or rmse < best[1]:
            best = (lam, rmse)
    return best[0], table


def _unresolved_pairs(subs, q_point, win):
    """Adjacent-in-ranking pairs whose order the data cannot resolve (pre-registered band)."""
    order = np.argsort(-q_point)
    lo, hi = UNRESOLVED_PROB
    out = []
    for r in range(len(order) - 1):
        a, c = int(order[r]), int(order[r + 1])
        p = float(win[a, c])
        if (lo <= p <= hi) or abs(q_point[a] - q_point[c]) < UNRESOLVED_GAP:
            out.append((subs[a], subs[c], p, float(q_point[a] - q_point[c])))
    return out


def rank_report(y, judge_keys, sub_keys, lam=None, n_boot=1000, seed=0):
    """Point estimates plus a parametric-bootstrap uncertainty layer on the fixed graph.

    Returns per-submission quality q, median rank and a [5,95]% rank interval, the pairwise
    win-probability matrix, and the adjacent pairs the data cannot separate. lambda, if not
    given, is chosen by select_lambda first (on observed ballots only). The bootstrap holds
    the observed (judge,submission) edges fixed and resamples ballots as q+b+N(0,sigma), so
    the uncertainty reflects noise on THIS panel, not an imagined re-assignment.
    """
    y = np.asarray(y, dtype=float)
    table = None
    if lam is None:
        lam, table = select_lambda(y, judge_keys, sub_keys, seed=seed)
    q, b = fit(y, judge_keys, sub_keys, lam)
    subs = sorted(q)
    pred = np.array([q[s] + b[j] for s, j in zip(sub_keys, judge_keys)])
    dof = max(len(y) - (len(q) + len(b)), 1)
    sigma = float(np.sqrt(np.sum((y - pred) ** 2) / dof))
    rng = np.random.default_rng(seed + 1)
    qmat = np.empty((n_boot, len(subs)))
    for bi in range(n_boot):
        ystar = pred + rng.normal(0.0, sigma, size=len(y))
        qb, _ = fit(ystar, judge_keys, sub_keys, lam)
        qmat[bi] = [qb[s] for s in subs]
    ranks = (-qmat).argsort(axis=1).argsort(axis=1) + 1     # 1 == best (highest q)
    q_point = np.array([q[s] for s in subs])
    win = np.empty((len(subs), len(subs)))
    for a in range(len(subs)):
        win[a] = (qmat[:, a][:, None] > qmat).mean(axis=0)
    return {
        "lambda": lam, "lambda_table": table, "sigma": sigma,
        "subs": subs, "q": q_point, "b": b,
        "rank_median": np.median(ranks, axis=0),
        "rank_lo": np.percentile(ranks, 5, axis=0),
        "rank_hi": np.percentile(ranks, 95, axis=0),
        "win_prob": win, "n_boot": n_boot,
        "unresolved": _unresolved_pairs(subs, q_point, win),
    }



