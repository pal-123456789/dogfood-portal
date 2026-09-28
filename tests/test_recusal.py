# tests/test_recusal.py
"""Pure tests for organizer-declared judge<->team recusals: the team->submission expansion
(src/judging/recusal.py) and its composition with the auto-assignment planner
(src/judging/assignment.py).

DB-free on purpose, like test_assignment.py: nothing here needs a database. They pin the novel logic
#135 adds -- expanding a team recusal to that team's submission ext_ids -- and prove that when the
expanded set is fed to the planner as a judge's `recused` set (exactly what judging.services does at
plan time), the planner never assigns that judge to any of the recused team's projects, while other
judges still cover them. The DB-backed test that judging.services reads JudgeRecusal rows lives in
src/judging/tests.py.
"""
from judging import assignment as A
from judging.recusal import expand_recusals


def _judge(ext, tracks=("t",), teams=(), recused=(), load=0):
    return {"ext_id": ext, "tracks": list(tracks), "teams": list(teams),
            "assigned": [], "recused": list(recused), "load": load}


def _project(ext, track="t", team="tX", reviews=0):
    return {"ext_id": ext, "track": track, "team": team, "reviews": reviews,
            "assigned_judges": []}


# --- expand_recusals -------------------------------------------------------------------------
def test_expand_maps_each_team_to_its_submissions():
    team_subs = {"tA": ["pA1", "pA2"], "tB": ["pB1"]}
    assert expand_recusals([("j0", "tA")], team_subs) == {"j0": {"pA1", "pA2"}}


def test_expand_unions_multiple_teams_for_one_judge():
    team_subs = {"tA": ["pA1"], "tB": ["pB1", "pB2"]}
    out = expand_recusals([("j0", "tA"), ("j0", "tB")], team_subs)
    assert out == {"j0": {"pA1", "pB1", "pB2"}}


def test_expand_skips_unknown_empty_and_blank():
    team_subs = {"tA": ["pA1"], "tEmpty": []}
    out = expand_recusals(
        [("j0", "tGhost"), ("j0", "tEmpty"), ("", "tA"), ("j1", ""), ("j2", "tA")],
        team_subs)
    assert out == {"j2": {"pA1"}}          # ghost / empty / blank all contribute nothing


def test_expand_coerces_ids_to_str():
    out = expand_recusals([("j0", 10)], {"10": [1, 2]})
    assert out == {"j0": {"1", "2"}}       # team id matched via str fallback, submissions -> str


def test_expand_empty_pairs_is_empty():
    assert expand_recusals([], {"tA": ["pA1"]}) == {}


# --- composition with the planner ------------------------------------------------------------
def test_recused_team_projects_never_go_to_that_judge():
    team_subs = {"tA": ["pA1", "pA2"], "tB": ["pB1"]}
    recused = expand_recusals([("j0", "tA")], team_subs)          # {"j0": {"pA1","pA2"}}
    judges = [_judge("j0", recused=recused.get("j0", ())),
              _judge("j1"), _judge("j2"), _judge("j3")]
    projects = [_project("pA1", team="tA"), _project("pA2", team="tA"),
                _project("pB1", team="tB")]
    plan = A.plan_assignments(judges, projects, k=2, seed=0)
    assert ("j0", "pA1") not in plan and ("j0", "pA2") not in plan
    summary = A.summarize_plan(judges, projects, plan, k=2)
    assert summary["covered_after"] == 3                          # others still reach k=2


def test_recused_judge_still_reviews_other_teams():
    # pB1 is on track "u", reviewable ONLY by j0; j0 is recused from team tA (track "t").
    judges = [_judge("j0", tracks=("t", "u"), recused=("pA1",)),
              _judge("j1", tracks=("t",))]
    projects = [_project("pA1", track="t", team="tA"),
                _project("pB1", track="u", team="tB")]
    plan = A.plan_assignments(judges, projects, k=1, seed=0)
    assert ("j0", "pB1") in plan            # recusal from tA does not bar j0 from tB
    assert ("j0", "pA1") not in plan        # but it does bar the recused team
    assert ("j1", "pA1") in plan            # and j1 covers the recused project


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for f in fns:
        f()
    print("test_recusal: %d passed" % len(fns))


if __name__ == "__main__":
    _run()
