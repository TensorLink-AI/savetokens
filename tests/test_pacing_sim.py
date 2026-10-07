"""Pacing replay mechanics: arrivals from usage, the pacing plan and the hard cap."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "pacing"))
import sim  # noqa: E402

from conftest import T0  # noqa: E402

H = sim.HOUR
RUNGS = [{"model": "a", "pass": 0.8, "cost": 1.0, "retry_pass": 0.8, "retry_cost": 1.0},
         {"model": "b", "pass": 0.5, "cost": 0.2, "retry_pass": 0.9, "retry_cost": 0.7}]


def test_arrivals_follow_usage_and_carry_fractions():
    hours = {T0: 1.0, T0 + H: 0.5, T0 + 2 * H: 0.5}
    a = sim.arrivals(hours, T0, T0 + 3 * H, per_dollar=2)
    assert len(a) == 4 and a[0] == T0 + 0.25 * H and a[-1] == T0 + 2.5 * H


def test_plan_mixes_two_rungs_to_fit_the_budget():
    assert sim.pace_plan(0, 100, [10] * 10, RUNGS, retry=False) == (0, 1.0)       # top rung fits
    i, x = sim.pace_plan(0, 6, [10] * 10, RUNGS, retry=False)                       # $0.60 per task
    assert i == 0 and abs(x - 0.5) < 1e-9
    assert sim.pace_plan(0, 1, [10] * 10, RUNGS, retry=False) == (1, 1.0)          # nothing fits: cheapest
    # retry makes the cheap rung dearer, so the same budget buys less of the top rung
    assert sim.pace_plan(0, 8, [10] * 10, RUNGS, retry=True)[1] < sim.pace_plan(0, 8, [10] * 10, RUNGS, retry=False)[1]


def test_plan_uses_the_high_end_of_the_forecast():
    # 80% of outcomes say 10 tasks left, 20% say 40: at risk 20% the plan covers 10, not 40
    rem = [10] * 8 + [40] * 2
    assert sim.pace_plan(0, 10, rem, RUNGS, retry=False) == (0, 1.0)
    rem = [10] * 7 + [40] * 3
    assert sim.pace_plan(0, 10, rem, RUNGS, retry=False)[0] == 1 or sim.pace_plan(0, 10, rem, RUNGS, retry=False)[1] < 1


def test_hard_cap_blocks_the_rest_of_the_week():
    results = {"all": {"t": {"a": [(1.0, 1.0)], "b": [(0.0, 0.2)]}}}
    week = {"start": T0, "arrivals": [T0 + i for i in range(5)]}
    draws = [("all", "t", 0.0, 0.0, False)] * 5
    r = sim.replay(week, draws, lambda *a: 0, 3.0, results, ["a", "b"], retry=False, soft=False)
    assert r["solved"] == 3 and r["blocked"] == 2 and r["hit"]
    soft = sim.replay(week, draws, lambda *a: 0, 3.0, results, ["a", "b"], retry=False, soft=True)
    assert soft["solved"] == 5 and soft["over"] == 2.0


def test_retry_redoes_a_failure_on_the_top_rung():
    results = {"all": {"t": {"a": [(1.0, 1.0)], "b": [(0.0, 0.2)]}}}
    week = {"start": T0, "arrivals": [T0]}
    r = sim.replay(week, [("all", "t", 0.0, 0.0, False)], lambda *a: 1, 10.0, results, ["a", "b"], retry=True, soft=False)
    assert r["solved"] == 1 and abs(r["spent"] - 1.2) < 1e-9


def test_held_plan_spreads_the_mix_across_tasks():
    pols = sim.make_policies(RUNGS, False, {}, per_dollar=1, every_h=24)
    week = {"start": T0, "arrivals": [T0 + i * H for i in range(4)]}
    # perfect forecast: 4 tasks, budget 2.4 -> $0.60 per task -> half on each rung
    picks = [pols["pace:perfect"](t, 0.0, 2.4, week) for t in week["arrivals"]]
    assert sorted(picks) == [0, 0, 1, 1]


def test_background_moves_down_before_interactive():
    steps = sim.configs(RUNGS, background=0.3)
    assert [(x["bg"], x["ia"]) for x in steps] == [(0, 0), (1, 0), (1, 1)]
    assert abs(steps[1]["cost"] - (0.3 * 0.2 + 0.7 * 1.0)) < 1e-9
    assert [(x["bg"], x["ia"]) for x in sim.configs(RUNGS, background=0.0)] == [(0, 0), (1, 1)]


def test_switching_a_warm_session_costs_a_reread_and_safe_switching_waits():
    results = {"all": {"t": {"a": [(1.0, 1.0)], "b": [(1.0, 0.2)]}}}
    week = {"start": T0, "arrivals": [T0, T0 + 600, T0 + 3 * H]}
    draws = [("all", "t", 0.0, 0.0, False)] * 3
    picks = iter([0, 1, 1])
    r = sim.replay(week, draws, lambda *a: next(picks), 100.0, results, ["a", "b"], False, False,
                   switch_cost=0.5, cache_ttl_h=1.0)
    assert r["switches"] == 1 and abs(r["switch_usd"] - 0.1) < 1e-9          # 0.5 x b's mean task cost
    picks = iter([0, 1, 1])
    safe = sim.replay(week, draws, lambda *a: next(picks), 100.0, results, ["a", "b"], False, False,
                      switch_cost=0.5, cache_ttl_h=1.0, safe=True)
    assert safe["switches"] == 0 and safe["share"] == [2 / 3, 1 / 3]          # held on a until the cache went cold
