# src/awards/podium.py
"""Pure podium derivation from a frozen, signed normalization result -- stdlib only, no Django.

The awards feature never recomputes a ranking and never signs anything of its own. It reads the
FROZEN rows of the already-signed, published normalization run (normalize.results.current_results
-> result["rows"]) and projects a small "podium" view over them: the top-N places, optionally
scoped to one track's submissions. Because every value here is copied from rows the signed run
already committed to, the podium is DERIVED FROM THE SIGNED RESULT -- it adds no new property to it
and introduces no new randomness.

Kept free of Django (and of everything but the standard library) so it can be unit-tested without a
database and imported as `awards.podium` on its own. Deterministic and side-effect free: the same
rows in produce the same podium out, and the input list is never mutated.
"""
from __future__ import annotations

# The frozen leaderboard rows produced by normalize.engine.compute_leaderboard carry the submission
# ext_id under "submission", the 1-based place under "rank", the display title under "title", the
# track ext_id under "track", and the normalized quality under "q" (see normalize/engine.py). We
# read those keys but tolerate a couple of obvious aliases for the id so a caller passing a lightly
# reshaped row (e.g. an API projection) still works.
_ID_KEYS = ("submission", "submission_ext_id", "ext_id")


def submission_id(row):
    """The submission ext_id carried by a frozen result row, or None if none is present."""
    for key in _ID_KEYS:
        value = row.get(key)
        if value:
            return value
    return None


def _rank(row):
    """The 1-based rank carried by a frozen result row, or None if missing/uncoercible."""
    value = row.get("rank")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def compute_podium(rows, top_n=3, allowed_ext_ids=None):
    """Project the top-`top_n` places of a frozen result onto a small podium list.

    `rows` is the frozen signed run's row list (the list found at
    normalize.results.current_results()["result"]["rows"]). Steps, in order:

      1. keep only dict rows that name a submission (defensive against malformed input);
      2. if `allowed_ext_ids` is given, keep only rows whose submission ext_id is in that set
         (e.g. one track's submissions) -- applied BEFORE truncation, so a track podium is the
         top-N *within* the track, not the overall top-N filtered down afterward;
      3. order by the frozen `rank` ascending (rows lacking a rank sort last, input order stable);
      4. take the first `top_n` (all rows when `top_n` is None or exceeds the count);
      5. re-number the survivors into podium `place` 1..N.

    Returns a list of plain dicts {place, submission, title, track, q, source_rank}. Deterministic
    and side-effect free -- `rows` is never mutated. Every value is copied from the frozen result,
    so the podium introduces no new ranking.
    """
    items = [r for r in (rows or ()) if isinstance(r, dict) and submission_id(r) is not None]
    if allowed_ext_ids is not None:
        allowed = set(allowed_ext_ids)
        items = [r for r in items if submission_id(r) in allowed]

    def _sort_key(pair):
        index, row = pair
        rank = _rank(row)
        # Ranked rows first (group 0, by ascending rank); rank-less rows last (group 1), each group
        # broken by original index so the sort is stable and fully deterministic.
        if rank is None:
            return (1, 0, index)
        return (0, rank, index)

    ordered = sorted(enumerate(items), key=_sort_key)
    if top_n is not None:
        ordered = ordered[:max(0, int(top_n))]

    podium = []
    for place, (_index, row) in enumerate(ordered, start=1):
        podium.append({
            "place": place,
            "submission": submission_id(row),
            "title": row.get("title", ""),
            "track": row.get("track", ""),
            "q": row.get("q"),
            "source_rank": _rank(row),
        })
    return podium


def place_at(podium, position):
    """The podium entry whose `place` == `position`, or None.

    `position` is the 1-based podium place a prize targets; position 0 (a special, non-podium award)
    never matches, so such prizes only ever show an explicitly assigned winner.
    """
    for entry in podium:
        if entry.get("place") == position:
            return entry
    return None
