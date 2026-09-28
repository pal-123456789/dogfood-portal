# tests/test_assignment.py
"""Pure-planner invariants for the connectivity-aware auto-assignment planner
(src/judging/assignment.py).

DB-free on purpose, exactly like test_normalize_engine.py: nothing here is marked django_db, so CI
never pays for a test database. These assert the properties the feature rests on -- k-coverage,
eligibility exclusions (team / track / recusal / already-assigned), determinism under a fixed seed,
load balance, and -- the point of the whole thing -- that a field that starts DISCONNECTED ends as a
single connected component, verified with the SAME counter the ranking uses
(normalize.engine.connected_components). The DB-backed view test lives in src/judging/tests.py.
"""
from judging import assignment as A
from normalize import engine as E


def _judge(ext, tracks=("t",), teams=(), assigned=(), recused=(), load=0):
    return {"ext_id": ext, "tracks": list(tracks), "teams": list(teams),
            "assigned": list(assigned), "recused": list(recused), "load": load}


def _project(ext, track="t", team=None, reviews=0, assigned_judges=()):
    return {"ext_id": ext, "track": track, "team": team or (ext + "_team"),
            "reviews": reviews, "assigned_judges": list(assigned_judges)}


def _existing_edges(judges, projects):
    edges = set()
    for j in judges:
        for p in j.get("assigned", []):
            edges.add((j["ext_id"], p))
    for p in projects:
        for jx in p.get("assigned_judges", []):
            edges.add((jx, p["ext_id"]))
    return edges


def _ncomp(edges):
    """Component count over exactly these edges, using the ranking engine's own union-find."""
    if not edges:
        return 0
    jk = [e[0] for e in edges]
    sk = [e[1] for e in edges]
    return E.connected_components(jk, sk)[2]
# TESTS_APPEND
def test_reaches_k_coverage_and_returns_only_new_edges():
    judges = [_judge("j%d" % i) for i in range(4)]
    projects = [_project("p%d" % i) for i in range(3)]
    plan = A.plan_assignments(judges, projects, k=2, seed=0)
    summary = A.summarize_plan(judges, projects, plan, k=2)
    assert summary["covered_after"] == 3                       # every project reaches k=2
    assert all(r["after"] >= 2 for r in summary["projects"])
    assert set(plan).isdisjoint(_existing_edges(judges, projects))   # only genuinely new edges
    assert len(plan) == len(set(plan))                         # and no duplicates


def test_determinism_same_seed_same_plan():
    judges = [_judge("j%d" % i) for i in range(5)]
    projects = [_project("p%d" % i) for i in range(4)]
    a = A.plan_assignments(judges, projects, 3, seed=0)
    b = A.plan_assignments(judges, projects, 3, seed=0)
    assert a == b                                              # identical order and content
    c = A.plan_assignments(judges, projects, 3, seed=7)        # a different seed only reshuffles ties
    assert A.summarize_plan(judges, projects, c, k=3)["covered_after"] == 4


def test_team_conflict_of_interest_excluded():
    judges = [_judge("j0", teams=["owns"]), _judge("j1")]
    projects = [_project("p0", team="owns")]
    plan = A.plan_assignments(judges, projects, k=1, seed=0)
    assert ("j0", "p0") not in plan                            # own team -> never assigned
    assert ("j1", "p0") in plan


def test_track_mismatch_excluded():
    judges = [_judge("j0", tracks=["tA"]), _judge("j1", tracks=["tB"])]
    projects = [_project("p0", track="tB")]
    plan = A.plan_assignments(judges, projects, k=1, seed=0)
    assert ("j0", "p0") not in plan                            # wrong track
    assert ("j1", "p0") in plan


def test_recusal_excluded():
    judges = [_judge("j0", recused=["p0"]), _judge("j1")]
    projects = [_project("p0")]
    plan = A.plan_assignments(judges, projects, k=1, seed=0)
    assert ("j0", "p0") not in plan
    assert ("j1", "p0") in plan


def test_already_assigned_is_not_re_added():
    judges = [_judge("j0", assigned=["p0"], load=1), _judge("j1")]
    projects = [_project("p0", reviews=1, assigned_judges=["j0"])]
    plan = A.plan_assignments(judges, projects, k=2, seed=0)
    assert ("j0", "p0") not in plan                            # existing edge is never re-emitted
    assert ("j1", "p0") in plan                                # the 2nd review comes from someone else
# TESTS_APPEND2
def test_infeasible_track_leaves_project_uncovered_without_error():
    judges = [_judge("j0", tracks=["tA"])]
    projects = [_project("p0", track="tB")]
    plan = A.plan_assignments(judges, projects, k=1, seed=0)
    assert plan == []                                          # nobody eligible -> no edge, no crash
    summary = A.summarize_plan(judges, projects, plan, k=1)
    assert summary["covered_after"] == 0 and summary["uncovered_after"] == 1


def test_load_is_balanced():
    judges = [_judge("j%d" % i) for i in range(3)]
    projects = [_project("p%d" % i) for i in range(3)]          # 3 projects x k=2 = 6 reviews / 3 judges
    plan = A.plan_assignments(judges, projects, k=2, seed=0)
    loads = [r["after"] for r in A.summarize_plan(judges, projects, plan, k=2)["judges"]]
    assert max(loads) - min(loads) <= 1                        # spread evenly, no piling


def test_disconnected_field_becomes_one_component():
    # two pre-existing edges form two separate components; two more projects are unreviewed.
    judges = [_judge("j0", assigned=["p0"], load=1), _judge("j1", assigned=["p1"], load=1),
              _judge("j2"), _judge("j3")]
    projects = [_project("p0", team="a", reviews=1, assigned_judges=["j0"]),
                _project("p1", team="b", reviews=1, assigned_judges=["j1"]),
                _project("p2", team="c"), _project("p3", team="d")]
    assert _ncomp(_existing_edges(judges, projects)) == 2      # started disconnected
    plan = A.plan_assignments(judges, projects, k=1, seed=0)
    final = _existing_edges(judges, projects) | set(plan)
    assert _ncomp(final) == 1                                  # engine agrees: one component
    summary = A.summarize_plan(judges, projects, plan, k=1)
    assert summary["components_before"] == 2                   # local union-find matches the engine
    assert summary["components_after"] == 1 and summary["connected"] is True
    assert summary["covered_after"] == 4


def test_single_multitrack_judge_bridges_two_tracks():
    # pA and pB live in disjoint tracks; only the multi-track judge jX can join the two islands.
    judges = [_judge("jA", tracks=["tA"]), _judge("jB", tracks=["tB"]),
              _judge("jX", tracks=["tA", "tB"])]
    projects = [_project("pA", track="tA", team="a"), _project("pB", track="tB", team="b")]
    plan = A.plan_assignments(judges, projects, k=1, seed=0)
    final = _existing_edges(judges, projects) | set(plan)
    assert _ncomp(final) == 1                                  # bridged via the multi-track judge
    assert A.summarize_plan(judges, projects, plan, k=1)["connected"] is True


