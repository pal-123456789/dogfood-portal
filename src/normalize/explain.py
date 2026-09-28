# src/normalize/explain.py
"""Plain-language, participant-facing explanation of ONE project's place in a published,
signed normalization run -- a pure, read-only interpretation layer over the frozen result.

WHY THIS IS SAFE AND HONEST
    Every figure this module surfaces (rank, the bootstrap rank interval, the severity-adjusted
    quality q, the raw mean, the severity delta, the adjacent-win probability, the ballot count)
    is read from a SINGLE row of a published run's signed `result` -- the very same frozen dict
    served at /normalize/results and results.json. So it recomputes nothing, reads no live ballot,
    touches no signing path, and cannot move a `result_hash`. It adds interpretation, not data.

    The signed result is per-SUBMISSION aggregates only: no judge identity, no per-ballot score.
    There is therefore nothing here to de-anonymise, and these per-project numbers are already
    public. The page is still scoped to the owning team (or an organizer) because the frame is
    "explain MY rank" and as defense-in-depth -- not because the figures are secret.

    Wording is deliberately non-over-claiming: the interval is a bootstrap diagnostic, not a
    guarantee; the severity delta is the model's arithmetic for placing every project on one
    scale, NOT a finding that any judge was biased; a thin ballot count is flagged as such; and a
    project in its own connected component is told, honestly, that cross-component comparison is
    not identified by ratings alone. The exact signed values live on the official results page;
    here they are rounded for reading and pointed back to that page for full precision.
"""
from __future__ import annotations

# The composite ballot/quality scale is 0..5 (engine.composite -> weighted 0..5); deltas share it.
# These bands only choose WORDING for the exact, already-published figures shown beside them.
_DELTA_BANDS = ((-0.75, "adjusted sharply downward"), (-0.25, "adjusted downward"),
                (0.25, "left essentially unchanged"), (0.75, "adjusted upward"))
_DELTA_TOP = "adjusted sharply upward"
_WIN_BANDS = ((0.60, "too close to call"), (0.75, "a slight edge"), (0.90, "a clear edge"))
_WIN_TOP = "a decisive edge"


def _band(value, bands, top_label):
    """First label whose threshold `value` falls strictly below; else `top_label`."""
    for threshold, label in bands:
        if value < threshold:
            return label
    return top_label


def _f(x, places=2):
    """Format a number for prose at fixed precision (e.g. 3.10, -0.42)."""
    return f"{float(x):.{places}f}"


def explain_row(row, *, field_size, n_boot, seed, n_components=1, next_title=None):
    """Return the participant-facing explanation dict for one published leaderboard `row`.

    `row` is one entry of a signed run's result["rows"]. `field_size` is the number of ranked
    projects, `n_boot`/`seed` disclose the bootstrap, `n_components` is the run's connected-
    component count, and `next_title` is the title of the project one rank below (for the
    adjacent-win sentence) or None when this row is in last place.
    """
    rank = int(row["rank"])
    lo, hi = int(row["rank_lo"]), int(row["rank_hi"])
    median = int(row.get("rank_median", rank))
    q = float(row["q"])
    raw = float(row["raw_mean"])
    delta = float(row["delta"])
    n_ballots = int(row.get("n_ballots", 0))
    win_next = row.get("win_next", None)
    certain = lo == hi

    interval = {
        "rank": rank, "lo": lo, "hi": hi, "median": median,
        "certain": certain, "field_size": field_size,
        "headline": (
            f"Your project finished rank {rank} of {field_size}." if certain else
            f"Your project finished rank {rank} of {field_size}, and across the resampled "
            f"analyses it lands between rank {lo} and rank {hi}."),
        "caveat": (
            f"That range is the 5th-to-95th percentile of your rank over {n_boot} parametric "
            f"bootstrap resamples at a fixed, disclosed seed ({seed}). It describes how stable "
            "the ordering is under the model's own noise -- it is not a promise that a \"true\" "
            "rank sits inside it, and it reproduces exactly from the signed run."),
    }

    if delta >= 0.75:
        label = _DELTA_TOP
    else:
        label = _band(delta, _DELTA_BANDS, _DELTA_TOP)
    if delta > 0:
        direction = ("The judges who reviewed your project tended, across every project they "
                     "scored, to rate lower than the panel as a whole, so the model lifted your "
                     f"raw average of {_f(raw)} to {_f(q)} (a shift of +{_f(delta)}).")
    elif delta < 0:
        direction = ("The judges who reviewed your project tended, across every project they "
                     "scored, to rate higher than the panel as a whole, so the model eased your "
                     f"raw average of {_f(raw)} down to {_f(q)} (a shift of {_f(delta)}).")
    else:
        direction = (f"Your reviewers scored close to the panel average, so severity adjustment "
                     f"barely moved your score ({_f(raw)} to {_f(q)}).")
    severity = {
        "label": label, "delta": round(delta, 4), "q": round(q, 4), "raw_mean": round(raw, 4),
        "headline": f"On the 0-5 scale, severity adjustment {label} your score. {direction}",
        "caveat": ("This is the model's arithmetic for placing every project on one comparable "
                   "scale after removing each judge's overall lean. It is not a finding that any "
                   "judge was biased, unfair, or mistaken."),
        "small_sample": n_ballots < 3,
        "small_sample_note": (
            f"Only {n_ballots} review{'' if n_ballots == 1 else 's'} fed this estimate, so it is "
            "more sensitive to any single ballot than a project with more reviews."
            if n_ballots < 3 else ""),
    }

    adjacent = None
    if win_next is not None and next_title:
        pct = int(round(float(win_next) * 100))
        adjacent = {
            "pct": pct, "next_title": next_title, "label": _band(float(win_next), _WIN_BANDS, _WIN_TOP),
            "headline": (f"Against “{next_title}” (the project just below you), your "
                         f"project ranked higher in {pct}% of those {n_boot} resamples -- "
                         f"{_band(float(win_next), _WIN_BANDS, _WIN_TOP)}."),
            "caveat": ("This is a model-based comparison over the same resamples, not a tally of "
                       "which project each judge preferred."),
        }

    component_note = None
    if n_components > 1:
        component_note = (
            f"This event's judging graph split into {n_components} groups that were not all tied "
            "together by shared judges. Severity-adjusted scores are only comparable within a "
            "connected group; comparisons across groups are not identified by the ratings alone.")

    return {
        "figures": {"rank": rank, "field_size": field_size, "q": round(q, 4),
                    "raw_mean": round(raw, 4), "delta": round(delta, 4),
                    "n_ballots": n_ballots, "win_pct": (int(round(float(win_next) * 100))
                                                        if win_next is not None else None)},
        "interval": interval, "severity": severity, "adjacent": adjacent,
        "component_note": component_note,
        "limitation": ("This explains the scores your project received; it does not measure merit "
                       "beyond those scores and does not detect collusion or fraud. The exact "
                       "signed figures are on the official results page."),
    }


def find_row(result, ext_id):
    """Return (row, next_title) for `ext_id` in a signed run `result`, or (None, None).

    `next_title` is the title of the row one rank below (rank+1) for the adjacent-win sentence.
    Matching is by the row's `submission` ext_id, exactly as stored in the signed result.
    """
    rows = (result or {}).get("rows") or []
    by_rank = {int(r["rank"]): r for r in rows}
    for r in rows:
        if r.get("submission") == ext_id:
            nxt = by_rank.get(int(r["rank"]) + 1)
            return r, (nxt.get("title") if nxt else None)
    return None, None
