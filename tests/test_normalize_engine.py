# tests/test_normalize_engine.py
"""Pure-estimator invariants for the normalization engine (src/normalize/engine.py).

DB-free on purpose, exactly like test_smoke.py: nothing here is marked django_db, so CI never
pays for a test database. These assert the mathematical properties the ranking's defensibility
rests on - the weighted composite, the singular-lambda guard, connected-component counting, the
per-component mean(b)=0 gauge, an in-grid positive lambda pick, and determinism. The DB-backed
service and access-gate tests live in src/normalize/tests.py (run via `manage.py test normalize`).
"""
import numpy as np
import pytest

from normalize import engine as E


def test_composite_is_the_weighted_mean():
    raw = {"functionality": 5, "quality": 3, "innovation": 1}
    assert abs(E.composite(raw, {c: 1.0 for c in E.CRITERIA}) - 3.0) < 1e-12
    heavy = {"functionality": 2.0, "quality": 1.0, "innovation": 1.0}
    assert abs(E.composite(raw, heavy) - (2 * 5 + 3 + 1) / 4.0) < 1e-12


def test_zero_weights_and_zero_lambda_are_rejected():
    with pytest.raises(ValueError):
        E.composite({c: 3 for c in E.CRITERIA}, {c: 0 for c in E.CRITERIA})
    with pytest.raises(ValueError):
        E.fit([3.0, 4.0], ["j1", "j2"], ["s1", "s1"], 0.0)


def test_connected_components_split_then_bridge():
    assert E.connected_components(["j1", "j1", "j2", "j2"],
                                  ["s1", "s2", "s3", "s4"])[2] == 2
    # one shared submission stitches the two halves into a single component
    assert E.connected_components(["j1", "j1", "j2", "j2", "j1"],
                                  ["s1", "s2", "s3", "s4", "s3"])[2] == 1


def test_gauge_is_zero_per_component_and_orders_quality():
    base = {"s_a": 4, "s_b": 3, "s_c": 2}
    bias = [0, 1, -1]                       # neutral / lenient / harsh judge
    jk, sk, y = [], [], []
    for n in range(3):
        for sid, v in base.items():
            jk.append("j%d" % n)
            sk.append(sid)
            y.append(v + bias[n])
    q, b = E.fit(y, jk, sk, 1.0)
    _, cbj, ncomp = E.connected_components(jk, sk)
    assert ncomp == 1
    assert E.component_gauge_error(b, cbj) < 1e-9
    assert q["s_a"] > q["s_b"] > q["s_c"]


def test_select_lambda_is_positive_and_in_grid():
    rng = np.random.default_rng(1)
    jk = ["j%d" % j for j in range(6) for _ in range(6)]
    sk = ["s%d" % ((j * 3 + k) % 8) for j in range(6) for k in range(6)]
    y = list(rng.uniform(1, 5, size=len(jk)))
    lam, _ = E.select_lambda(y, jk, sk, repeats=2)
    assert lam in E.LAMBDA_GRID and lam > 0
    assert 0 not in E.LAMBDA_GRID           # lambda=0 is singular; it must never be offered


def test_rank_report_is_deterministic():
    rng = np.random.default_rng(2)
    jk = ["j%d" % j for j in range(5) for _ in range(5)]
    sk = ["s%d" % ((j + 2 * k) % 7) for j in range(5) for k in range(5)]
    y = list(rng.uniform(1, 5, size=len(jk)))
    r1 = E.rank_report(y, jk, sk, lam=1.0, n_boot=200, seed=0)
    r2 = E.rank_report(y, jk, sk, lam=1.0, n_boot=200, seed=0)
    assert np.allclose(r1["q"], r2["q"])


def test_judge_means_are_per_judge_composite_means():
    # j1 sees {4,3,2} -> mean 3.0 ; j2 sees {5,4,3} -> mean 4.0. Grouped by judge, not submission.
    y = [4.0, 3.0, 2.0, 5.0, 4.0, 3.0]
    jk = ["j1", "j1", "j1", "j2", "j2", "j2"]
    sk = ["s1", "s2", "s3", "s1", "s2", "s3"]
    m = E.judge_means(y, jk)
    assert abs(m["j1"] - 3.0) < 1e-12
    assert abs(m["j2"] - 4.0) < 1e-12
    # the raw judge spread the write-up reports is just the stdev of these means
    assert abs(float(np.std(list(m.values()), ddof=1)) - np.std([3.0, 4.0], ddof=1)) < 1e-12
    # and it groups independently of the submission axis raw_means uses
    assert set(E.raw_means(y, sk)) == {"s1", "s2", "s3"}
