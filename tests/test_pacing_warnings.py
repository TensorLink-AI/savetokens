"""Warning score mechanics: forecast cadence in the replay, and catching hits ahead of time."""
from __future__ import annotations

import sys
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals" / "pacing"))
import warn_score as wscore  # noqa: E402

from conftest import T0  # noqa: E402

H = 3600
H0 = (int(T0) // H) * H


def test_replay_refreshes_like_the_product():
    hours = {H0 + k * H: 1.0 for k in range(0, 3)}          # active for 3 hours, then idle
    os_ = wscore.origins(hours, H0, H0 + 30 * H)
    assert os_[:4] == [H0, H0 + H, H0 + 2 * H, H0 + 3 * H]     # hourly while active
    assert H0 + 4 * H not in os_                               # idle and nothing new: no refresh
    assert H0 + 15 * H in os_                                  # but never older than 12 hours


def test_burn_rules_catch_a_steady_climb_before_the_hit():
    start, end = H0, H0 + 5 * H
    events = [(start + k * 600, 10.0) for k in range(30)]       # $60 an hour for 5 hours: $300 in all
    res = wscore.score_windows([(start, end)], events, {}, 200.0, 15 * 60, ())
    assert res["burn"]["hits"] == 1 and res["burn"]["caught"] == 1
    assert res["used@80%"]["leads"][0] < res["burn"]["leads"][0]   # 80% used warns far later than the burn rate


def test_forecast_rule_uses_the_chance_of_reaching_the_limit():
    start, end = H0, H0 + 5 * H
    events = [(start + k * 600, 10.0) for k in range(30)]
    data = array("d", [60.0] * 5 * 10)                          # every path: $60 an hour
    fc = {"baseline": {start: {"start": start, "hours": 5, "n": 10, "data": data}}}
    res = wscore.score_windows([(start, end)], events, fc, 200.0, 15 * 60, ("baseline",))
    assert res["baseline@0.8"]["caught"] == 1


def test_quiet_window_counts_no_false_alarm():
    start, end = H0, H0 + 5 * H
    events = [(start + 60, 1.0)]
    res = wscore.score_windows([(start, end)], events, {}, 200.0, 15 * 60, ())
    assert res["burn"]["false"] == 0 and res["burn"]["quiet"] == 1
