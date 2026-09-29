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

## Recusals move the assignable graph, so they can move identifiability

A judge who has a conflict of interest with a team should not review that team's work, and the
system records that as a first-class fact: a `JudgeRecusal` row naming a judge, a team, and an
optional reason (`src/judging/models.py`, unique on the judge–team pair). Before the assignment
planner runs, the pure helper `expand_recusals` (`src/judging/recusal.py`) fans that team-level
declaration out to every one of the team's submitted projects, and the planner's eligibility test
refuses those pairs — alongside the standing rule that a judge is never planned onto their own
team's project.

The methodological point is the one worth stating out loud, because it connects a governance
feature to the identifiability argument above. That argument rests entirely on the
**connectivity** of the judge↔submission graph: `q` is comparable across two projects only when
some chain of shared judges links them. A recusal deletes candidate edges from that graph. Enough
recusals — or a few badly placed ones on a sparse panel — can therefore *reduce* connectivity, in
the limit splitting the graph into pieces whose levels are no longer mutually identified. Recusing
is still the right call; the cost is simply not free, and it is a cost the ratings cannot pay back.

So do not reason about recusals in the abstract: read the connectivity the code already computes.
The organizer diagnostics panel reports `coverage.n_components` and a per-component breakdown under
`coverage.components`, plus `coverage.articulation_judges` — the judges whose removal would isolate
a submission or split a component, which is exactly the set where one more recusal is most
expensive. The live leaderboard and every signed run carry the same top-level `n_components`, and
`src/normalize/engine.py`'s `connected_components` is the single primitive behind all of them.
Recuse, then re-read the component count before assigning.

Two honest scope notes on recusal, because it is narrower than the name suggests. It governs only
who MAY be **planned**: it does not block a ballot write, does not touch an existing assignment or
any already-recorded ballot (the append-only history is untouched), and does not gate the manual
assign action. And it is filed through Django admin by a staff user — there is no organizer-facing
recusal route in this build, and the recusal row itself is not written to the audit chain.

## Connectivity-aware assignment planning — a planner, not an autopilot

Because connectivity is what makes the ranking identified, the assignment step is written to
optimise for it rather than to spread reviews at random.
`plan_assignments` (`src/judging/assignment.py`) is pure and DB-free — its only import is
`hashlib`, it reads no clock, and a plan is a deterministic function of its inputs. It carries its
own union-find, deliberately semantics-identical to the estimator's, and uses it twice: as a
tie-break inside the per-project coverage pass, so an edge that would *join* two components is
preferred over one that would not, and again in a dedicated final pass that adds as few
cross-component edges as it can find until the graph is one piece. Alongside the proposed edges,
`summarize_plan` reports `components_before`, `components_after` and `connected`, so an organizer
can see what the plan does to identifiability before anything is written.

It is a **planner whose output an organizer applies** — it does not assign by itself. The pure
function returns a list of candidate judge–project pairs and has no ability to write anything; on
the organizer-only route `/judging/<ext_id>/auto-assign`, a **GET previews the plan and
writes nothing at all**, and only an explicit **POST** applies it, committing each edge through the
ordinary audited `assign_judge` service (one `judge.assigned` audit event per edge, idempotent on
re-apply). The organizer stays the actor of record.

Honest limits, since a planner that oversold itself would undercut the rest of this page. It never
removes or rewrites an existing assignment, only proposes new ones. It balances judge load but does
not cap it, and it models no expertise, preference, or scheduling. It can legitimately **fail** to
connect the graph — when tracks are genuinely disjoint with no commonly eligible judge it stops
early and reports the remaining component count honestly rather than forcing an edge — and it can
leave a project short of the coverage target, reported as `uncovered_after`. Track-matching is
implemented and unit-tested in the pure planner, but this schema has no per-judge track table, so
in production every judge is offered every track and the rule has no live effect. Only submitted
projects are planned.

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

How much judge severity is there to remove in the first place? The **raw judge spread** — the
standard deviation of each judge's mean composite score across the 30 judges — is **σ = 0.420**
(emitted by `normalize_report` as `raw_judge_spread`, computed straight from the fixture
ballots). Judges do differ in average severity, but 0.420 is an **upper bound** on what
normalization can legitimately remove, not the removable amount: in a sparse panel — 126 ballots
over 30 judges and 41 projects, about three ballots per submission — a judge's mean also reflects
*which* projects they were assigned, so it conflates true leniency with assignment mix. The model
subtracts only the part separable from project quality, and cross-validation shrinks even that
(λ = 10).

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

A complementary check asks the one question a point ranking cannot ask of itself: is the spread of
fitted quality **consensus**, or could judge severity plus noise alone produce it? `signal_test`
(`src/normalize/engine.py`) takes `std(q)` across submissions as its statistic and builds the null by
permuting, **within each judge**, which of that judge's submissions received which of that judge's
scores — holding every judge's own severity, score-use, and the judge↔submission graph fixed, and
destroying only cross-judge agreement. It returns a one-sided Monte-Carlo p-value against a
significance level fixed **before** any result (`SIGNAL_ALPHA = 0.05`, exactly like the unresolved
band above), and is emitted in the `normalize_report --json` dict (`signal_p`, `signal_significant`).
It is **display-only — never fed back into `q` or the signed ranking** (`compute_leaderboard` does not
call it). The test is built to *withhold* a verdict rather than manufacture one: when the data cannot
distinguish the observed spread from severity plus noise, a large p-value is the correct, honest
output. The same `--json` report also carries a per-submission standard error on fitted quality
(`q_se`, summarized as `q_se_median` / `q_se_max`), so each `q` comes with its own dispersion, not only
through the pairwise band.

## Leave-one-judge-out: how much of the podium is one judge?

The bootstrap above resamples noise on a fixed graph. A different and blunter question is whether
any *single judge* is carrying the podium — a fair worry on a sparse panel, where one judge is
effectively a voting bloc. The review-diagnostics report answers it directly: for each judge it
drops **all** of that judge's ballots at once, refits the estimator at the same pinned `lambda`,
and compares the resulting point order against the baseline, restricted to the submissions present
in both fits (so a project that vanishes entirely with that judge never manufactures a phantom
shift — it is reported separately as a dropped submission). Per judge it reports whether the
winner changed, whether the top three changed as a set, the largest rank shift and the largest
score shift, and a `flagged` boolean against thresholds **pre-registered as module constants** in
`src/normalize/diagnostics.py`, fixed before any event's data is seen, exactly like the unresolved
band. It surfaces under `judge_influence` in the report, in the "Decision influence" section of the
organizer-only `/normalize/diagnostics` page, and in `manage.py review_diagnostics`.

Read it as what it is: a **sensitivity display**, and nothing more. It says how fragile the ranking
is to any one judge — not that a judge did anything wrong. It is **not an accusation and not fraud
detection**: nothing here scores anyone as dishonest, computes a fraud probability, or downweights
or excludes any ballot or judge, and every payload carries its own scope line to that effect. On a
panel this size the power is low by construction, so a flag means "look here", not "act here";
removing or reweighting a judge remains an organizer decision made in the open. Two further honest
notes: the refits reuse the baseline's pinned `lambda` rather than re-selecting it by
cross-validation per refit, and the whole diagnostics report runs on **live, unsigned** ballots, so
it is a pre-finalization read and carries no property of the signed run.

## Explaining one project's rank, from the frozen result

Teams ask why they placed where they placed, and the honest answer has to be the *same* answer the
official ranking gives — otherwise the explanation becomes a second, unsigned source of truth. So
the per-team page at `/normalize/results/explain/<ext_id>` does not recompute anything. It reads a
**single row out of the published run's frozen, signed `result`** — the very same frozen dict served
on the public results page and re-derived by the offline verifier — and renders it
(`src/normalize/explain.py`, `src/normalize/results.py`). No live ballot is read, no signing path is
touched, and the page cannot move a `result_hash`.

What a team sees is that row restated in words: its rank in the field, its `q`, its raw mean and the
difference between them, its ballot count, its bootstrap rank interval with the seed disclosed and
worded as a range rather than a promise, a plain-language band for the direction and size of the
severity adjustment, a small-sample note when its ballot count is thin, and — when the panel splits
— the cross-component caveat that comparisons across groups are not identified by the ratings alone.
The severity wording is deliberately constrained: it explains the model's arithmetic for putting
every project on one comparable scale after removing each judge's overall lean, and states that this
is **not** a finding that any judge was biased, unfair, or mistaken. The page closes by saying that
it explains the scores a project received, does not measure merit beyond those scores, and does not
detect collusion or fraud.

Access is authenticated and narrow: the **owning team or an event organizer**, with every other case
answered by a uniform 404 so the page leaks no existence, and a 404 for everyone — owner included —
before results are published. One precise boundary worth recording: the adjacent-pair win
percentage shown on this page is *stored* in the frozen result but is deliberately stripped from the
canonical projection before hashing (`_UNSIGNED_ROW_FIELDS` in `src/normalize/engine.py`), so it is
frozen and display-only rather than covered by `result_hash`.

## Duplicate consistency control

The fixture plants two identical submissions — prj_07 and prj_41 are the same team (tm_07),
same track (trk_03), same title ("Dry Harbour"). A perfect estimator would score them equally.
The raw gap is 0.500; at `lambda = 10` the normalized gap is 0.481. Normalization tightens the
twins only slightly at the CV-optimal `lambda` (it tightens them more under lighter shrinkage,
but CV rejects that as overfitting). This is an honest empirical floor: with this little data,
even two identical projects land about 0.48 apart, which is itself a reason to distrust any
sub-0.5 quality gap in the table.

## Not the same thing: the duplicate-title diagnostic

There are now two unrelated things in this system with "duplicate" in the name, and conflating them
would be a real misreading, so the distinction is worth making explicitly.

The section directly above — **duplicate consistency** — is a *normalization sanity check*. It uses
one specific, deliberately planted control pair in the released fixture: prj_07 and prj_41, the same
team in the same track under the same title. Because a perfect estimator would score identical work
identically, the gap between those two is an empirical floor on the engine's own resolution, and the
figures quoted there are measured properties of the estimator. It is a statement about the *model*.

The **duplicate-title diagnostic** (`src/normalize/duplicates.py`) is a newer, separate,
organizer-facing feature that makes no statement about the model at all. It groups any submissions
that share a track *and* a whitespace-collapsed, casefolded title, and lists those clusters in the
"Duplicate submissions" panel of the organizer-only `/normalize/diagnostics` page, so a human can
go look. It is pure, Django-free, **independent of every ballot** — submissions cluster before any
judging happens — changes no state, and is never fed into the leaderboard, the signed result, or any
hash, so it cannot move a `result_hash`. It is display-only and it is **not fraud detection**:
sharing a title is a prompt to look, not a verdict, because two teams can independently pick the
same name and a team may legitimately supersede its own entry. The same title in a *different* track
is not flagged, and lone submissions are ignored.

In short: the consistency control measures how finely the estimator can separate identical work; the
title diagnostic asks a human whether two rows in the field should have been one row. Neither is
derived from the other, and the diagnostic is HTML-only — it is not carried in
`/normalize/diagnostics.json`.

## Review top-up at a prize line — a review-planning aid, not a ranking

Before results are finalized and signed, the most useful question an organizer can ask is not "who
won" but "where would another review actually change the answer". The organizer-only planner at
`/events/<event_ext_id>/awards/topup` (`src/awards/topup.py`) answers exactly that, and nothing
beyond it. For each podium prize, it takes the rank cutoff that prize implies, looks at the
contenders sitting at or straddling that cutoff within the prize's pool — the whole field, or one
track for a track-scoped prize — and flags one for any of three stated reasons: it has fewer
recorded reviews than the coverage target; its bootstrap rank interval spans the cutoff, so
resampling alone can move it across the line; or it sits on the boundary with a `q`-gap across the
cutoff below the closeness threshold. Each flag carries its reason in words, and an untroubled
cutoff yields an empty list.

Every honest caveat attaches to this at once. It is a **pre-finalization review-planning aid**: it
ranks nothing, assigns no score, is not the final ranking, and is **not fraud detection**. Awarding
a prize stays a separate, explicit organizer action, and the official podium is always derived from
the frozen signed result — never from this planner. Its own limitation is the important one: it
reads the **live, unsigned** standings, so its output legitimately changes as reviews arrive and it
is **not reproducible from a signed artifact**. Treat its output as a worklist with a timestamp, not
as a finding. The underlying planner is pure and deterministic — the same inputs produce the same
plan and the input rows are never mutated — so the non-reproducibility is entirely a property of the
live data it reads, not of the arithmetic.

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
routes, so the normalization surface cannot move the 7/7 result. The routes are split by audience,
and the split is the point — a severity-adjusted recompute can reshuffle a podium, so nothing live
is public (verify the whole list against `src/normalize/urls.py`):

- **Organizer-only, live recompute** — `401` unauthenticated, `403` non-organizer, `200` organizer,
  the same gate as the CSV export: `/normalize/` (HTML leaderboard),
  `/normalize/leaderboard.json`, `/normalize/pairwise` (pairwise sensitivity, never part of a signed
  result), `/normalize/diagnostics` (review diagnostics — robustness, coverage, decision influence,
  duplicate-title clusters), and `/normalize/diagnostics.json` (the same report minus the
  HTML-only duplicate panel).
- **Organizer-only, write** — `/normalize/results/publish`: `GET` shows the current publication and
  history, `POST` builds, signs and appends the next version.
- **Public** — `/normalize/results` and `/normalize/results.json`: the **frozen** `result` of a
  signed run served verbatim, never a live recompute. Before an organizer publishes, these return
  `200` with a neutral "not published yet" state rather than a ranking.
- **Owner or organizer, authenticated** — `/normalize/results/explain/<ext_id>`: read from the same
  frozen signed result; a browser without a session is sent to login, and any other caller gets a
  uniform `404` (as does everyone, owner included, before results are published).

`src/normalize/tests.py` asserts the invariants — single component, gauge ≈ 0, `lambda > 0` inside
the grid, determinism, and the access gate. Regenerate every figure above with:


```
python manage.py normalize_report          # human-readable
python manage.py normalize_report --json    # machine-readable
```
