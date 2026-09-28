# tests/test_explain.py
"""Pure-builder invariants for the participant-facing rank explanation
(src/normalize/explain.py).

DB-free on purpose, like test_normalize_engine.py / test_assignment.py: nothing here is marked
django_db, so CI never pays for a test database. These assert the properties the feature rests on
-- that every figure echoes the signed row unchanged (the module interprets, it never recomputes),
that the wording bands land on the right side of each threshold, that the honest caveats are always
present, and that small-sample / adjacent-win / cross-component notes appear exactly when they
should. The DB-backed access-control test lives in src/normalize/tests.py.
"""
from normalize import explain as X


def _row(rank=2, lo=1, hi=3, median=2, q=3.4, raw=3.0, delta=0.4,
         n_ballots=5, win_next=0.8, submission="prj_02", title="Beta", track="trk_01"):
    return {"rank": rank, "rank_lo": lo, "rank_hi": hi, "rank_median": median,
            "q": q, "raw_mean": raw, "delta": delta, "n_ballots": n_ballots,
            "win_next": win_next, "submission": submission, "title": title, "track": track}


def _build(**kw):
    row = _row(**{k: kw.pop(k) for k in list(kw) if k in _row.__code__.co_varnames})
    return X.explain_row(row, field_size=kw.get("field_size", 10),
                         n_boot=kw.get("n_boot", 1000), seed=kw.get("seed", 0),
                         n_components=kw.get("n_components", 1),
                         next_title=kw.get("next_title", "Gamma"))


def test_figures_echo_the_signed_row_unchanged():
    e = _build(rank=2, q=3.4, raw=3.0, delta=0.4, n_ballots=5, win_next=0.8, field_size=10)
    f = e["figures"]
    assert f["rank"] == 2 and f["field_size"] == 10
    assert f["q"] == 3.4 and f["raw_mean"] == 3.0 and f["delta"] == 0.4
    assert f["n_ballots"] == 5 and f["win_pct"] == 80  # 0.8 -> 80%


def test_interval_certain_vs_range_wording():
    certain = _build(lo=4, hi=4, rank=4)
    assert certain["interval"]["certain"] is True
    assert "between rank" not in certain["interval"]["headline"]
    ranged = _build(lo=1, hi=3, rank=2)
    assert ranged["interval"]["certain"] is False
    assert "between rank 1 and rank 3" in ranged["interval"]["headline"]


def test_interval_caveat_discloses_bootstrap_seed_and_is_not_a_promise():
    e = _build(n_boot=1000, seed=7)
    cav = e["interval"]["caveat"]
    assert "1000" in cav and "seed (7)" in cav
    assert "not a promise" in cav


def test_severity_direction_and_scale_wording():
    up = _build(delta=0.4, raw=3.0, q=3.4)["severity"]
    assert "lifted" in up["headline"] and "+0.40" in up["headline"] and "0-5 scale" in up["headline"]
    down = _build(delta=-0.4, raw=3.4, q=3.0)["severity"]
    assert "eased" in down["headline"] and "-0.40" in down["headline"]
    flat = _build(delta=0.0, raw=3.0, q=3.0)["severity"]
    assert "barely moved" in flat["headline"]


def test_severity_bands_pick_the_right_label():
    assert _build(delta=1.0)["severity"]["label"] == "adjusted sharply upward"
    assert _build(delta=0.4)["severity"]["label"] == "adjusted upward"
    assert _build(delta=0.0)["severity"]["label"] == "left essentially unchanged"
    assert _build(delta=-0.4)["severity"]["label"] == "adjusted downward"
    assert _build(delta=-1.0)["severity"]["label"] == "adjusted sharply downward"


def test_severity_caveat_never_blames_a_judge():
    cav = _build(delta=0.4)["severity"]["caveat"]
    assert "not a finding that any judge was biased" in cav


def test_small_sample_flag_toggles_below_three_ballots():
    thin = _build(n_ballots=2)["severity"]
    assert thin["small_sample"] is True and "2 reviews" in thin["small_sample_note"]
    one = _build(n_ballots=1)["severity"]
    assert "Only 1 review fed" in one["small_sample_note"]
    thick = _build(n_ballots=3)["severity"]
    assert thick["small_sample"] is False and thick["small_sample_note"] == ""


def test_adjacent_win_bands_and_wording():
    e = _build(win_next=0.8, next_title="Gamma")["adjacent"]
    assert e["pct"] == 80 and e["next_title"] == "Gamma" and e["label"] == "a clear edge"
    assert "Gamma" in e["headline"] and "80%" in e["headline"]
    assert "not a tally of which project each judge preferred" in e["caveat"]
    assert _build(win_next=0.5)["adjacent"]["label"] == "too close to call"
    assert _build(win_next=0.95)["adjacent"]["label"] == "a decisive edge"


def test_adjacent_absent_for_last_place():
    assert _build(win_next=None, next_title=None)["adjacent"] is None
    assert _build(win_next=0.9, next_title=None)["adjacent"] is None


def test_component_note_only_when_split():
    assert _build(n_components=1)["component_note"] is None
    note = _build(n_components=3)["component_note"]
    assert note is not None and "3 groups" in note and "not identified" in note


def test_limitation_is_honest_about_scope():
    lim = _build()["limitation"]
    assert "does not measure merit" in lim and "does not detect collusion or fraud" in lim


def test_find_row_returns_row_and_next_title():
    result = {"rows": [_row(rank=1, submission="prj_01", title="Alpha"),
                        _row(rank=2, submission="prj_02", title="Beta"),
                        _row(rank=3, submission="prj_03", title="Gamma")]}
    row, nxt = X.find_row(result, "prj_02")
    assert row["submission"] == "prj_02" and nxt == "Gamma"
    last, nxt_last = X.find_row(result, "prj_03")
    assert last["submission"] == "prj_03" and nxt_last is None
    missing, none_title = X.find_row(result, "prj_99")
    assert missing is None and none_title is None


def test_find_row_tolerates_empty_result():
    assert X.find_row({}, "prj_01") == (None, None)
    assert X.find_row(None, "prj_01") == (None, None)
