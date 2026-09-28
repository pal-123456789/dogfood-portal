# src/judging/recusal.py
"""Pure expansion of organizer-declared judge<->team recusals into the per-submission recusal set
the auto-assignment planner consumes.

DB-FREE on purpose, the same split judging/assignment.py keeps from judging/services.py: the service
reads JudgeRecusal rows and the event's submissions, hands this module plain tuples and a plain
team->submissions map, and gets back {judge_ext_id: {submission_ext_id, ...}} to drop into each
judge's planner input. Nothing here imports Django, so the team-to-submissions expansion is a
deterministic function of its inputs and unit-testable without a database.

A recusal is filed at TEAM granularity (a conflict of interest is with the people, so it must cover
that team's future submissions too, not only the ones that exist when it is filed); the planner
works in submission ext_ids, so this expands each (judge, team) pair to every current submission of
that team. It is ADDITIVE to the automatic own-team exclusion the planner already applies via each
judge's `teams` set -- this covers a COI with a team the judge is NOT a member of.
"""
from __future__ import annotations

__all__ = ["expand_recusals"]


def expand_recusals(recused_pairs, team_submissions):
    """Map judge<->team recusals to each judge's recused submission ext_ids.

    recused_pairs:     iterable of (judge_ext_id, team_ext_id) -- one per filed recusal.
    team_submissions:  {team_ext_id: iterable of submission_ext_id} for the event.

    Returns {judge_ext_id: set(submission_ext_id)}. A pair naming a team with no current submissions
    contributes nothing; a judge recused from several teams gets the union. All ids are coerced to
    str so the result compares equal to the planner's string-set inputs regardless of the caller's id
    types (a str-key fallback also matches when the map is keyed by str but the pair carries a raw
    id). A blank judge or team ext_id is skipped -- a blank-ext_id judge is not planned anyway.
    """
    out = {}
    for jext, text in recused_pairs:
        if not jext or not text:
            continue
        subs = team_submissions.get(text)
        if subs is None:
            subs = team_submissions.get(str(text))
        if not subs:
            continue
        out.setdefault(str(jext), set()).update(str(s) for s in subs)
    return out
