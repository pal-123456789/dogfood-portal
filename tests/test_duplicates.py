# tests/test_duplicates.py
"""Pure tests for within-track duplicate-submission detection (src/normalize/duplicates.py).

DB-free on purpose, like test_recusal.py / test_assignment.py: nothing here needs a database, so CI
never pays for a test DB. They pin the detector's contract -- group submissions that share a track
AND a normalized title (casefold + collapsed whitespace), report groups of two or more, ignore
singletons, keep matches in different tracks apart, and return a deterministic (track, title) order.
The DB-backed test that the diagnostics view exposes `duplicate_clusters` lives in
src/normalize/tests.py.
"""
from normalize.duplicates import find_duplicate_clusters, normalize_title


def _row(ext_id, team, track, title):
    return {"ext_id": ext_id, "team": team, "track": track, "title": title}


# --- normalize_title -------------------------------------------------------------------------
def test_normalize_title_collapses_and_casefolds():
    assert normalize_title("  Dry   Harbour ") == "dry harbour"
    assert normalize_title("A\tB\nC") == "a b c"                 # any whitespace run -> one space


def test_empty_input_returns_empty_list():
    assert find_duplicate_clusters([]) == []


# --- (a) same track + same title, incl. case/whitespace variants -> one cluster --------------
def test_same_track_same_title_case_and_whitespace_is_one_cluster():
    rows = [
        _row("prj_07", "tm_07", "trk_03", "Dry Harbour"),
        _row("prj_41", "tm_07", "trk_03", "  dry   HARBOUR "),   # case + whitespace variant
    ]
    clusters = find_duplicate_clusters(rows)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["track"] == "trk_03"
    assert c["title"] == "dry harbour"                          # normalized (casefold + collapsed)
    assert c["display_title"] == "Dry Harbour"                  # first raw title seen
    assert c["submissions"] == ["prj_07", "prj_41"]             # sorted by ext_id
    assert c["teams"] == ["tm_07", "tm_07"]                     # parallel to submissions
    assert c["entries"] == [{"ext_id": "prj_07", "team": "tm_07"},
                            {"ext_id": "prj_41", "team": "tm_07"}]


# --- (b) same title, DIFFERENT tracks -> not flagged -----------------------------------------
def test_same_title_different_tracks_not_flagged():
    rows = [
        _row("prj_01", "tm_01", "trk_01", "Aurora"),
        _row("prj_02", "tm_02", "trk_02", "aurora"),            # same title, other track
    ]
    assert find_duplicate_clusters(rows) == []


# --- (c) singletons ignored ------------------------------------------------------------------
def test_singletons_are_ignored():
    rows = [
        _row("prj_01", "tm_01", "trk_01", "Alpha"),
        _row("prj_02", "tm_02", "trk_01", "Beta"),
        _row("prj_03", "tm_03", "trk_02", "Alpha"),             # same title as prj_01, other track
    ]
    assert find_duplicate_clusters(rows) == []


# --- (d) deterministic order by (track, title) -----------------------------------------------
def test_deterministic_order_by_track_then_title():
    rows = [
        _row("prj_09", "tm_09", "trk_02", "Zephyr"),
        _row("prj_02", "tm_02", "trk_01", "Beacon"),
        _row("prj_08", "tm_08", "trk_02", "Zephyr"),
        _row("prj_01", "tm_01", "trk_01", "Beacon"),
        _row("prj_05", "tm_05", "trk_01", "Anchor"),
        _row("prj_06", "tm_06", "trk_01", "anchor"),            # case variant, same track
    ]
    clusters = find_duplicate_clusters(rows)
    keys = [(c["track"], c["title"]) for c in clusters]
    assert keys == [("trk_01", "anchor"), ("trk_01", "beacon"), ("trk_02", "zephyr")]
    assert clusters[0]["submissions"] == ["prj_05", "prj_06"]
    assert clusters[1]["submissions"] == ["prj_01", "prj_02"]
    assert clusters[2]["submissions"] == ["prj_08", "prj_09"]
    # repeatable on the same input; keys + within-cluster ordering do not depend on row order
    assert find_duplicate_clusters(rows) == clusters
    repeat = find_duplicate_clusters(list(reversed(rows)))
    assert [(c["track"], c["title"]) for c in repeat] == keys
    assert [c["submissions"] for c in repeat] == [c["submissions"] for c in clusters]


# --- (e) groups of three or more -------------------------------------------------------------
def test_groups_three_or_more():
    rows = [
        _row("prj_01", "tm_01", "trk_01", "Nimbus"),
        _row("prj_02", "tm_02", "trk_01", "nimbus"),
        _row("prj_03", "tm_03", "trk_01", " NIMBUS "),
        _row("prj_04", "tm_04", "trk_01", "Solo"),              # singleton -> not flagged
    ]
    clusters = find_duplicate_clusters(rows)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["title"] == "nimbus"
    assert c["submissions"] == ["prj_01", "prj_02", "prj_03"]
    assert c["teams"] == ["tm_01", "tm_02", "tm_03"]
    assert len(c["entries"]) == 3


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for f in fns:
        try:
            f()
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %s" % (f.__name__, exc))
    if failed:
        print("test_duplicates: %d FAILED of %d" % (failed, len(fns)))
        raise SystemExit(1)
    print("test_duplicates: %d passed" % len(fns))


if __name__ == "__main__":
    _run()
