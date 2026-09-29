"""DB-free unit tests for awards.podium (the pure, stdlib-only podium projection).

Run standalone -- no Django, no database:
    cd portal && PYTHONPATH=src python3 tests/test_podium.py
It prints "test_podium: N passed" and exits nonzero on the first failure. pytest also collects the
test_* functions (there is no @pytest.mark.django_db here because nothing touches the ORM).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from awards.podium import compute_podium, place_at, submission_id  # noqa: E402


# A frozen-result-shaped row list (mirrors normalize result["rows"]): deliberately out of rank
# order so the tests prove compute_podium sorts by the frozen `rank`, not by input order.
def _rows():
    return [
        {"rank": 2, "submission": "prj_b", "title": "Beta", "track": "trk_1", "q": 3.0},
        {"rank": 1, "submission": "prj_a", "title": "Alpha", "track": "trk_1", "q": 4.0},
        {"rank": 4, "submission": "prj_d", "title": "Delta", "track": "trk_2", "q": 1.0},
        {"rank": 3, "submission": "prj_c", "title": "Gamma", "track": "trk_2", "q": 2.0},
        {"rank": 5, "submission": "prj_e", "title": "Epsilon", "track": "trk_1", "q": 0.5},
    ]


def test_top_n_truncation_and_rank_ordering():
    pod = compute_podium(_rows(), top_n=3)
    assert [e["place"] for e in pod] == [1, 2, 3]
    assert [e["submission"] for e in pod] == ["prj_a", "prj_b", "prj_c"]
    assert [e["source_rank"] for e in pod] == [1, 2, 3]
    assert [e["title"] for e in pod] == ["Alpha", "Beta", "Gamma"]
    assert pod[0]["q"] == 4.0


def test_default_top_n_is_three():
    assert len(compute_podium(_rows())) == 3


def test_track_filter_before_truncation_then_renumber():
    # Scope to track 2's submissions (ranks 3 and 4). Filtering happens BEFORE truncation and the
    # survivors are renumbered 1..N, so the within-track podium is [prj_c, prj_d] at places [1, 2].
    pod = compute_podium(_rows(), top_n=3, allowed_ext_ids={"prj_c", "prj_d"})
    assert [e["submission"] for e in pod] == ["prj_c", "prj_d"]
    assert [e["place"] for e in pod] == [1, 2]
    assert [e["source_rank"] for e in pod] == [3, 4]


def test_scope_can_promote_a_low_ranked_submission():
    # prj_e is overall rank 5 (outside the overall top 3), but scoping to just it makes it place 1.
    pod = compute_podium(_rows(), top_n=3, allowed_ext_ids={"prj_e"})
    assert [e["submission"] for e in pod] == ["prj_e"]
    assert pod[0]["place"] == 1 and pod[0]["source_rank"] == 5


def test_top_n_within_scope():
    pod = compute_podium(_rows(), top_n=1, allowed_ext_ids={"prj_c", "prj_d"})
    assert [e["submission"] for e in pod] == ["prj_c"]


def test_fewer_rows_than_top_n():
    pod = compute_podium(_rows()[:2], top_n=3)
    assert len(pod) == 2
    assert [e["place"] for e in pod] == [1, 2]


def test_empty_and_none_input():
    assert compute_podium([]) == []
    assert compute_podium(None) == []
    assert compute_podium([], top_n=5, allowed_ext_ids={"prj_a"}) == []


def test_rankless_rows_sort_last():
    rows = _rows() + [{"submission": "prj_z", "title": "Zeta", "track": "trk_3"}]
    pod = compute_podium(rows, top_n=10)
    assert pod[-1]["submission"] == "prj_z"
    assert pod[-1]["source_rank"] is None
    # Every ranked row precedes the rank-less one.
    assert [e["submission"] for e in pod[:5]] == ["prj_a", "prj_b", "prj_c", "prj_d", "prj_e"]


def test_id_key_aliases():
    assert submission_id({"submission_ext_id": "prj_x"}) == "prj_x"
    assert submission_id({"ext_id": "prj_y"}) == "prj_y"
    assert submission_id({"title": "no id"}) is None
    pod = compute_podium([{"rank": 1, "submission_ext_id": "prj_x", "title": "X", "q": 9}])
    assert pod[0]["submission"] == "prj_x"


def test_rows_not_a_dict_are_ignored():
    pod = compute_podium([None, 7, "nope", {"rank": 1, "submission": "prj_a", "title": "A"}])
    assert [e["submission"] for e in pod] == ["prj_a"]


def test_input_is_not_mutated():
    rows = _rows()
    before = [dict(r) for r in rows]
    pod = compute_podium(rows, top_n=3)
    assert rows == before                      # untouched
    assert all(pod[i] is not rows[i] for i in range(len(pod)))   # fresh dicts


def test_place_at():
    pod = compute_podium(_rows(), top_n=3)
    assert place_at(pod, 1)["submission"] == "prj_a"
    assert place_at(pod, 3)["submission"] == "prj_c"
    assert place_at(pod, 0) is None            # special (non-podium) award never matches
    assert place_at(pod, 9) is None
    assert place_at([], 1) is None


def _run():
    tests = sorted((name, obj) for name, obj in globals().items()
                   if name.startswith("test_") and callable(obj))
    passed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # a failing assertion must surface and exit nonzero
            print("test_podium: FAILED at %s: %r" % (name, exc))
            sys.exit(1)
        passed += 1
    print("test_podium: %d passed" % passed)


if __name__ == "__main__":
    _run()
