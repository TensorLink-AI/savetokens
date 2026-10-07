"""Backtest mechanics on synthetic usage: windows, the steering simulation and scoring."""
from __future__ import annotations

from array import array

from conftest import T0
from savetokens import backtest
from savetokens.backtest import HOUR, WINDOW


def test_windows_start_at_the_first_request_after_the_last_one_ended():
    evs = [(T0, 1.0), (T0 + 4 * HOUR, 1.0), (T0 + 5 * HOUR + 1, 2.0), (T0 + 20 * HOUR, 3.0)]
    wins = backtest.limit_windows(evs)
    assert [(s - T0, len(e)) for s, _, e in wins] == [(0, 2), (5 * HOUR + 1, 1), (20 * HOUR, 1)]
    assert all(end - s == WINDOW for s, end, _ in wins)


def test_simulate_blocks_usage_past_the_limit():
    win = (T0, T0 + WINDOW, [(T0 + i * 600, 10.0) for i in range(10)])   # $100 of demand
    r = backtest.simulate(win, 60.0, lambda *a: False, lean=0.2)
    assert r == {"hit": True, "blocked": 40.0, "lean_h": 0.0}


def test_economising_early_avoids_the_hit():
    win = (T0, T0 + WINDOW, [(T0 + i * 600 + 1, 10.0) for i in range(10)])
    always = backtest.simulate(win, 85.0, lambda *a: True, lean=0.2)     # $80 spent: under the limit
    assert not always["hit"] and always["blocked"] == 0
    assert always["lean_h"] == 2.0       # half-hours up to the last request (at 1:30), not idle time
    # reacting at 80% used comes too late here: no checkpoint falls between 80% and the hit
    late = backtest.simulate(win, 85.0, lambda t, used, w: used >= 0.8 * 85, lean=0.2)
    assert late == {"hit": True, "blocked": 15.0, "lean_h": 0.0}


def test_forecast_rule_uses_the_chance_of_a_hit():
    # 10 sample paths of 24 hours; 4 of them carry $50 in the first hour, the rest nothing
    data = array("d")
    for p in range(10):
        data.extend([50.0 if p < 4 else 0.0] + [0.0] * 23)
    fc = {"baseline": {T0: {"start": T0, "hours": 24, "n": 10, "data": data}}}
    win = (T0, T0 + WINDOW, [])
    rules = backtest.policies(fc, 40.0, ("baseline",))
    assert rules["baseline@0.3"](T0, 0.0, win)          # 40% chance of reaching $40
    assert not rules["baseline@0.5"](T0, 0.0, win)
    assert rules["baseline@0.2"](T0, 0.0, win)


def test_steering_compares_against_unsteered_hits():
    wins = [(T0 + d * 86400, T0 + d * 86400 + WINDOW, [(T0 + d * 86400 + i * 600 + 1, c) for i in range(10)])
            for d, c in enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])]
    sc = backtest.steering(wins, {}, (0.2,), 0.2, ())[0]
    assert sc["policies"]["none"]["hits"] == 2
    assert sc["policies"]["oracle"]["hits"] <= sc["policies"]["none"]["hits"]
    assert sc["policies"]["oracle"]["needless_lean_h"] == 0


def test_pinball_and_coverage():
    loss, inside = backtest._pinball([float(x) for x in range(101)], 50.0)
    assert inside and loss < 5
    loss_far, inside_far = backtest._pinball([float(x) for x in range(101)], 500.0)
    assert not inside_far and loss_far > loss


def test_surge_detection_counts_alarms_and_injected_runaways():
    o = T0 - T0 % 3600
    hours = {o - d * 86400 + k * 3600: 1.0 for d in range(0, 20) for k in range(24)}   # $1 every hour
    data = array("d")
    for p in range(100):
        data.extend([1.0 + p / 100] * 24)                 # every hour: p95 about $1.94, p99 about $1.98
    fc = {"baseline": {o: {"start": o, "hours": 24, "n": 100, "data": data}}}
    out = backtest.surges(fc, hours, [o], sizes=(0.5, 5.0))
    one, two = out["baseline 1h>p99"], out["baseline 2h>p95"]
    assert one["alarms_per_week"] == 0 and two["alarms_per_week"] == 0   # real usage stays inside the band
    assert one["caught"]["$5/h"] == 1.0 and two["caught"]["$5/h"] == 1.0
    assert one["caught"]["$0.5/h"] == 0.0
    assert out["fixed 1h>3x usual"]["caught"]["$0.5/h"] == 0.0           # 1.5 < 3 x usual
