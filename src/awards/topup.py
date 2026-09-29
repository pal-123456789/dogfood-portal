# src/awards/topup.py
"""Pure, stdlib-only "prize-line review top-up" planner (no Django, no numpy).

An organizer aid computed from the LIVE (unsigned) standings: for each podium prize, look at the
rank cutoff that prize implies and surface the contenders whose position there is still unsettled --
the places where a few more reviews would most reduce uncertainty BEFORE results are finalized and
signed. It ranks nothing of its own, assigns no score, and is NOT fraud detection; awarding a prize
stays a separate, explicit organizer action and the official podium is always derived from the
signed result. Deterministic and side-effect free: the same inputs produce the same plan and the
input lists/dicts are never mutated.

Kept free of Django and numpy so it unit-tests without a database (see tests/test_podium.py's
sibling tests/test_topup.py). The live leaderboard rows it reads are produced by
normalize.engine.compute_leaderboard and carry, per row: `submission` (ext_id), `rank` (1-based),
`title`, `track` (track ext_id), `q`, `rank_lo`/`rank_hi` (bootstrap rank interval), and `n_ballots`
(recorded reviews for that submission). We read those keys and tolerate a couple of id aliases.
"""
from __future__ import annotations

_ID_KEYS = ("submission", "submission_ext_id", "ext_id")


def _submission_id(row):
    """The submission ext_id carried by a leaderboard row, or None if none is present."""
    for key in _ID_KEYS:
        value = row.get(key)
        if value:
            return value
    return None


def _to_int(value):
    """`value` as an int, or None if missing/uncoercible."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value):
    """`value` as a float, or None if missing/uncoercible."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ordered_pool(rows, track):
    """The rows in one pool -- the whole field when `track` is falsy, else only rows whose `track`
    matches -- ordered by the live `rank` (rank-less rows last, input order stable) and numbered into
    1-based pool places. Returns a list of (place, row); the row objects are the originals (read
    only, never mutated)."""
    items = []
    for index, row in enumerate(rows or ()):
        if not isinstance(row, dict) or _submission_id(row) is None:
            continue
        if track and (row.get("track") or "") != track:
            continue
        items.append((index, row))

    def _key(pair):
        index, row = pair
        rank = _to_int(row.get("rank"))
        if rank is None:
            return (1, 0, index)
        return (0, rank, index)

    return [(place, row) for place, (_index, row) in enumerate(sorted(items, key=_key), start=1)]


def _contender(place, row):
    """A plain, JSON-friendly snapshot of one pool row at `place` (no judge data, no PII)."""
    return {
        "place": place,
        "rank": _to_int(row.get("rank")),
        "submission": _submission_id(row),
        "title": row.get("title", ""),
        "track": row.get("track", ""),
        "q": _to_float(row.get("q")),
        "n_ballots": _to_int(row.get("n_ballots")),
        "rank_lo": _to_int(row.get("rank_lo")),
        "rank_hi": _to_int(row.get("rank_hi")),
        "reasons": [],
    }


def review_topup_plan(rows, prizes, *, min_ballots=3, gap=0.25):
    """Per-podium-prize review top-up suggestions from the LIVE leaderboard `rows`.

    `prizes` is an iterable of descriptors {position, track, name, ext_id, track_name}; only podium
    prizes (position >= 1) imply a cutoff, so a special award (position 0) is skipped. For a prize at
    place P the cutoff sits between pool places P and P+1. Within the prize's pool (its track when
    track-scoped, else the whole field) a contender at or straddling that cutoff is flagged when ANY
    of:
      * thin coverage -- its recorded ballot count is below `min_ballots`;
      * a straddling interval -- its bootstrap rank interval [rank_lo, rank_hi] reaches from at or
        above P to at or below P+1, i.e. resampling alone can move it across the line;
      * a close margin -- it sits at place P or P+1 and the q-gap across the cutoff is below `gap`.

    Considered contenders are the two boundary places P and P+1 plus any pool row whose interval
    straddles the line. Returns one entry per podium prize:
        {prize, prize_ext_id, position, track, track_name, boundary, pool_size, needs_review,
         contenders: [ {place, rank, submission, title, track, q, n_ballots, rank_lo, rank_hi,
                        reasons: [...]} ]}
    `contenders` holds only the flagged rows; an unflagged cutoff yields an empty list and
    needs_review=False. Deterministic; `rows`/`prizes` are never mutated.
    """
    plan = []
    for prize in (prizes or ()):
        if not isinstance(prize, dict):
            continue
        position = _to_int(prize.get("position"))
        if position is None or position < 1:
            continue                       # special / non-podium award: no cutoff to review
        track = prize.get("track") or ""
        pool = _ordered_pool(rows, track)
        by_place = {place: row for place, row in pool}

        # q-gap across the cutoff (place P vs P+1), only when both sides of the line exist.
        q_here = _to_float(by_place[position].get("q")) if position in by_place else None
        q_next = _to_float(by_place[position + 1].get("q")) if (position + 1) in by_place else None
        close_margin = (q_here is not None and q_next is not None and abs(q_here - q_next) < gap)

        contenders = []
        for place, row in pool:
            rank_lo = _to_int(row.get("rank_lo"))
            rank_hi = _to_int(row.get("rank_hi"))
            straddles = (rank_lo is not None and rank_hi is not None
                         and rank_lo <= position and rank_hi >= position + 1)
            at_boundary = place in (position, position + 1)
            if not (at_boundary or straddles):
                continue
            reasons = []
            n_ballots = _to_int(row.get("n_ballots"))
            if min_ballots and n_ballots is not None and n_ballots < min_ballots:
                reasons.append("only %d review%s recorded (below %d)"
                               % (n_ballots, "" if n_ballots == 1 else "s", min_ballots))
            if straddles:
                reasons.append("rank interval %d–%d spans the place-%d cutoff"
                               % (rank_lo, rank_hi, position))
            if at_boundary and close_margin:
                reasons.append("within %.2f q of the place-%d cutoff (Δ=%.3f)"
                               % (gap, position, abs(q_here - q_next)))
            if reasons:
                entry = _contender(place, row)
                entry["reasons"] = reasons
                contenders.append(entry)

        plan.append({
            "prize": prize.get("name", ""),
            "prize_ext_id": prize.get("ext_id", ""),
            "position": position,
            "track": track,
            "track_name": prize.get("track_name", ""),
            "boundary": "places %d/%d" % (position, position + 1),
            "pool_size": len(pool),
            "needs_review": bool(contenders),
            "contenders": contenders,
        })
    return plan
