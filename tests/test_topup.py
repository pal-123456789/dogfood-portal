"""DB-free unit tests for awards.topup (the pure, stdlib-only prize-line review top-up planner).

Run standalone -- no Django, no database, no numpy:
    cd portal && PYTHONPATH=src python3 tests/test_topup.py
It prints "test_topup: N passed" and exits nonzero on the first failure. pytest also collects the
test_* functions (there is no @pytest.mark.django_db here because nothing touches the ORM).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from awards.topup import review_topup_plan  # noqa: E402


# A live-leaderboard-shaped row list (mirrors normalize.services.leaderboard()["rows"]).
# Two tracks; deliberately out of rank order to prove the planner sorts by `rank`. Row `d` at the
# 3/4 boundary is thin (1 ballot) and has a wide, straddling rank interval; `c` sits just above it.
# The 1/2 pair (a, b) is deliberately settled: locked intervals (1-1, 2-2) and a wide q-gap (0.30),
# so the top line draws no flag under any of the three rules.
def _rows():
    return [
        {"rank": 2, "submission": "prj_b", "title": "Beta", "track": "trk_1", "q": 3.60,
         "n_ballots": 5, "rank_lo": 2, "rank_hi": 2},
        {"rank": 1, "submission": "prj_a", "title": "Alpha", "track": "trk_1", "q": 3.90,
         "n_ballots": 6, "rank_lo": 1, "rank_hi": 1},
        {"rank": 4, "submission": "prj_d", "title": "Delta", "track": "trk_2", "q": 2.10,
         "n_ballots": 1, "rank_lo": 2, "rank_hi": 5},
        {"rank": 3, "submission": "prj_c", "title": "Gamma", "track": "trk_2", "q": 2.20,
         "n_ballots": 5, "rank_lo": 2, "rank_hi": 4},
        {"rank": 5, "submission": "prj_e", "title": "Epsilon", "track": "trk_1", "q": 0.50,
         "n_ballots": 5, "rank_lo": 5, "rank_hi": 5},
    ]


def _line_for(plan, position):
    matches = [ln for ln in plan if ln["position"] == position]
    assert len(matches) == 1, "expected exactly one line for position %d" % position
    return matches[0]


def test_special_award_is_skipped():
    # position 0 is a special/non-podium award: no cutoff, so it never appears in the plan.
    plan = review_topup_plan(_rows(), [{"position": 0, "name": "Best Use of Data"}])
    assert plan == []


def test_flags_thin_coverage_at_boundary():
    # Event-wide first place: cutoff between places 1 and 2. a and b are well covered (>= min_ballots),
    # their intervals are locked (1-1, 2-2 -> neither straddles), and the q-gap is 0.30 (>= 0.25), so
    # none of the three rules fire and the line is settled.
    plan = review_topup_plan(_rows(), [{"position": 1, "name": "Grand", "ext_id": "prz_1"}])
    line = _line_for(plan, 1)
    assert line["boundary"] == "places 1/2"
    assert line["pool_size"] == 5
    assert line["needs_review"] is False
    assert line["contenders"] == []


def test_flags_at_third_place_cutoff():
    # Event-wide third place: cutoff between places 3 (prj_c) and 4 (prj_d). prj_d has 1 ballot and
    # a rank interval 2-5 that straddles the line, and the q-gap c->d is 0.10 (< 0.25). So both the
    # thin-coverage and straddle reasons fire for d, and the close-margin reason for both c and d.
    plan = review_topup_plan(_rows(), [{"position": 3, "name": "Bronze", "ext_id": "prz_3"}])
    line = _line_for(plan, 3)
    assert line["needs_review"] is True
    flagged = {c["submission"]: c for c in line["contenders"]}
    assert set(flagged) == {"prj_c", "prj_d"}
    d = flagged["prj_d"]
    assert d["place"] == 4 and d["n_ballots"] == 1
    assert any("only 1 review" in r for r in d["reasons"])
    assert any("spans the place-3 cutoff" in r for r in d["reasons"])
    assert any("cutoff" in r for r in flagged["prj_c"]["reasons"])   # close-margin on the upper side


def test_track_scoping_reranks_within_track():
    # A track-2 prize for first place. Track 2 holds prj_c (rank 3) and prj_d (rank 4); within the
    # track they renumber to places 1 and 2, so the cutoff 1/2 is c vs d (q-gap 0.10 < 0.25).
    plan = review_topup_plan(_rows(), [{"position": 1, "track": "trk_2", "name": "Track 2 Winner"}])
    line = _line_for(plan, 1)
    assert line["track"] == "trk_2"
    assert line["pool_size"] == 2
    assert line["boundary"] == "places 1/2"
    assert line["needs_review"] is True
    places = {c["submission"]: c["place"] for c in line["contenders"]}
    assert places == {"prj_c": 1, "prj_d": 2}


def test_min_ballots_zero_disables_thin_flag():
    # With min_ballots=0 the thin-coverage reason is disabled; d is then flagged only by its
    # straddling interval / close margin, never by ballot count.
    plan = review_topup_plan(_rows(), [{"position": 3, "name": "Bronze"}], min_ballots=0)
    d = next(c for c in _line_for(plan, 3)["contenders"] if c["submission"] == "prj_d")
    assert not any("review" in r for r in d["reasons"])


def test_prize_deeper_than_field():
    # A place-9 prize in a 5-project field: no cutoff inside the field, nothing flagged, but the
    # line is still reported with the true pool size so the organizer sees why.
    plan = review_topup_plan(_rows(), [{"position": 9, "name": "Ninth"}])
    line = _line_for(plan, 9)
    assert line["pool_size"] == 5
    assert line["needs_review"] is False
    assert line["contenders"] == []


def test_empty_and_none_inputs():
    assert review_topup_plan([], [{"position": 1, "name": "x"}]) == [{
        "prize": "x", "prize_ext_id": "", "position": 1, "track": "", "track_name": "",
        "boundary": "places 1/2", "pool_size": 0, "needs_review": False, "contenders": []}]
    assert review_topup_plan(None, None) == []
    assert review_topup_plan(_rows(), []) == []


def test_non_dict_prizes_and_rows_ignored():
    plan = review_topup_plan([None, 7, "nope"] + _rows(), [None, 5, {"position": 1, "name": "ok"}])
    assert [ln["prize"] for ln in plan] == ["ok"]
    assert _line_for(plan, 1)["pool_size"] == 5


def test_deterministic_and_input_not_mutated():
    rows, prizes = _rows(), [{"position": 3, "name": "Bronze"}]
    rows_before = [dict(r) for r in rows]
    prizes_before = [dict(p) for p in prizes]
    first = review_topup_plan(rows, prizes)
    second = review_topup_plan(rows, prizes)
    assert first == second                         # deterministic
    assert rows == rows_before                     # rows untouched
    assert prizes == prizes_before                 # prizes untouched


def _run():
    tests = sorted((name, obj) for name, obj in globals().items()
                   if name.startswith("test_") and callable(obj))
    passed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # a failing assertion must surface and exit nonzero
            print("test_topup: FAILED at %s: %r" % (name, exc))
            sys.exit(1)
        passed += 1
    print("test_topup: %d passed" % passed)


if __name__ == "__main__":
    _run()
