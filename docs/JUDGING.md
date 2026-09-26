# Judging normalization

DOGFOOD ranks submissions from judge ballots. Judges disagree on *level*: a harsh judge's 3
and a lenient judge's 4 can encode the same underlying quality. Ranking on the raw mean
therefore rewards whoever happened to draw the kinder panel. This page documents the engine
that separates project quality from judge severity, the honest evidence for what it does on
the real DOGFOOD fixture, and — the part most normalization write-ups omit — where the data
simply does not support a confident answer.

Every number on this page is emitted by `python manage.py normalize_report` (add `--json` for
the raw dict). The estimator (`src/normalize/engine.py`) is pure NumPy, seeded, and
deterministic; nothing here is hand-entered.

## The model

Each ballot is reduced to one weighted 0..5 score (the criterion weights live in the
`RubricWeight` table, owned by the organizer — the engine never hard-codes them). We then fit

```
y_ij = q_i + b_j + e_ij
```

where `y_ij` is judge *j*'s composite for submission *i*, `q_i` is the submission's quality,
`b_j` is judge *j*'s severity offset, and `e_ij` is noise. Ranking on `q` instead of on the
raw mean of `y` is the whole point: `q` is the quality estimate with judge severity removed.

## The estimator: ridge is partial pooling

We minimise

```
sum (y_ij - q_i - b_j)^2  +  lambda * sum_j b_j^2
```

Only the judge effects `b` are penalised; `q` is free. Two facts fall out of that choice, and
both shape the code:

**The gauge is pinned by the penalty, not chosen by hand.** Summing the stationarity
conditions over one connected component, the `q`-equations and the `b`-equations share the
same ballot sum, so `lambda * sum_j b_j = 0`, i.e. `mean(b) = 0` within every component — for
*any* `lambda > 0`. That is what lands `q` back on the 1..5 ballot scale instead of floating.
On the real fixture the measured gauge error is `8.8e-18` (machine precision), confirming the
identity holds exactly rather than approximately.

**`lambda = 0` is singular.** With no penalty the location null space is unconstrained; a
solver either raises or an `lstsq` fallback silently picks a different gauge and shifts `q`
off-scale. So the search grid starts strictly above zero.

## Identifiability: connected components

The gauge is *per connected component* of the judge↔submission graph. If that graph splits,
each piece has its own free level and cross-piece ranking is not identified by ratings alone —
no estimator can compare two submissions that share no judge, even transitively.

The real DOGFOOD panel (126 ballots, 41 submissions, 30 judges) forms **exactly one connected
component**, so the global ranking *is* data-identified and needs no bridging assumption. The
engine still computes the component structure on every run and refuses to compare across
components when a fixture does split.

## Choosing lambda honestly

`lambda` is selected by repeated 5×5-fold cross-validation on the **observed ballots only** —
ground truth is never consulted, so the choice cannot leak. A held-out ballot is scored only
when both its judge and its submission also appear in the training fold (otherwise that effect
is unestimated); such orphans are skipped, not guessed. Averaging over five fold-partitions
de-noises the pick so it does not hinge on one lucky split. The sweep is a clean U with an
interior minimum:

| lambda | mean held-out RMSE |
|--------|--------------------|
| 0.01   | 1.0271 |
| 0.03   | 1.0103 |
| 0.10   | 0.9788 |
| 0.30   | 0.9352 |
| 1.0    | 0.8743 |
| 3.0    | 0.8291 |
| **10.0** | **0.8121  ← selected** |
| 30.0   | 0.8128 |
## What normalization does on the real panel — honestly

At the CV-selected `lambda = 10`:

| # | Project | Track | q | raw | Δ | 90% rank |
|---|---------|-------|-----|-----|------|----------|
| 1 | Iron Switch (prj_34) | trk_08 | 4.333 | 4.333 | −0.000 | [1, 18] |
| 2 | Salt Ledger (prj_11) | trk_02 | 4.313 | 4.333 | −0.020 | [1, 16] |
| 3 | Dry Relay (prj_25) | trk_02 | 4.148 | 4.111 | +0.037 | [1, 25] |
| 4 | Still Beacon (prj_10) | trk_08 | 4.103 | 4.167 | −0.063 | [1, 30] |
| 5 | Salt Loom (prj_37) | trk_07 | 4.082 | 4.083 | −0.002 | [2, 23] |

The raw leaderboard has a **tie at the top**: prj_11 and prj_34 are both 4.333. Severity
adjustment breaks it — prj_34 edges ahead because prj_11's high marks leaned slightly more on
lenient judges. So the normalized #1 (Iron Switch) is not the raw #1 (Salt Ledger). Across the
field, 29 of 41 projects change rank position, though Spearman correlation with the raw order
stays high at 0.986 — the reshuffle is concentrated near ties, not global chaos.

But we do **not** claim a large accuracy win, because this panel does not contain one. The
within-submission dispersion falls only 1.08× (0.542 → 0.502): DOGFOOD's judges were fairly
consistent, so there is little severity to remove. This is the opposite of the 3× reductions
you can manufacture on synthetic data with strong planted bias, and reporting the real 1.08×
is the point. Normalization's value on *this* data is tie-breaking and top-order correction,
not variance collapse.

## The integrity punchline: report uncertainty, do not manufacture a winner

A point ranking is not a result without a sense of whether the data supports it. We attach a
parametric bootstrap on the **fixed observed graph**: resample ballots as `q + b + N(0, sigma)`
with `sigma = 0.756`, holding the observed (judge, submission) edges fixed so the uncertainty
reflects noise on *this* panel rather than an imagined re-assignment. The findings are blunt:

- `P(prj_34 > prj_11) = 0.518` — the "winner" is a coin-flip.
- The top two both have median rank 4, with 90% rank intervals [1, 18] and [1, 16].
- **All 40 adjacent pairs** fall inside the pre-registered *unresolved* band (pairwise
  win-probability in [0.10, 0.90], or a quality gap below 0.25). At roughly three ballots per
  submission, the ratings do not resolve a fine-grained order anywhere in the table.

The band `(P ∈ [0.10, 0.90], Δq < 0.25)` was fixed **before** any result was computed, so it
cannot be tuned to flatter a conclusion. The leaderboard surfaces this directly: it shows each
project's rank interval and flags every unresolved adjacent pair, so an organizer reads "these
two are a tie the data can't break," not a false podium.

## Duplicate consistency control

The fixture plants two identical submissions — prj_07 and prj_41 are the same team (tm_07),
same track (trk_03), same title ("Dry Harbour"). A perfect estimator would score them equally.
The raw gap is 0.500; at `lambda = 10` the normalized gap is 0.481. Normalization tightens the
twins only slightly at the CV-optimal `lambda` (it tightens them more under lighter shrinkage,
but CV rejects that as overfitting). This is an honest empirical floor: with this little data,
even two identical projects land about 0.48 apart, which is itself a reason to distrust any
sub-0.5 quality gap in the table.

## Ablation

| Ranking method | Produces a ranking? | In-sample RMSE | Held-out ballot RMSE | Within-sub σ | Top-1 |
|---|---|---|---|---|---|
| Global mean | no (all equal) | 0.648 | 0.654 | — | none |
| Raw submission mean | yes | 0.540 | 0.828 | 0.542 | prj_11 |
| **Ridge q+b, λ=10** | yes | 0.500 | **0.812** | 0.502 | prj_34 |

Read this table carefully, because the naive reading is a trap. *Held-out ballot RMSE rewards
refusing to differentiate*: the global mean wins that column (0.654) only because most ballot
variance is idiosyncratic noise (σ = 0.756) and a single constant cannot overfit — but it
assigns every project the same score and therefore has **zero ranking power**. It is a
reference floor, not a competitor. `lambda` is selected *within* the ridge family, where `q`
is free and the gauge is pinned; that family's held-out floor is 0.812 at λ=10. Among the
methods that actually rank, partial pooling beats the raw submission mean on held-out ballots
(0.812 < 0.828) and lowers within-submission dispersion — modest, but real and in the honest
direction. Whether the resulting *order* is trustworthy is a question RMSE cannot answer; the
bootstrap answers it, and here it says the top is unresolved.

## Reproducibility and wiring

The engine is pure NumPy with zero Django imports (`src/normalize/engine.py`); the ORM bridge
(`src/normalize/services.py`) is read-only and touches none of the five acceptance-checker
routes, so the normalization surface cannot move the 7/7 result. The organizer-only leaderboard
lives at `/normalize/` (HTML) and `/normalize/leaderboard.json`; an unauthenticated caller gets
401 and a non-organizer 403, the same gate as the CSV export. `src/normalize/tests.py` asserts
the invariants — single component, gauge ≈ 0, `lambda > 0` inside the grid, determinism, and
the access gate. Regenerate every figure above with:

```
python manage.py normalize_report          # human-readable
python manage.py normalize_report --json    # machine-readable
```
