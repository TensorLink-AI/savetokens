"""Demand from the meter, the outlook per limit, and pace alerts."""
from __future__ import annotations

from array import array

from savetokens import alerts, forecast, maintain, meter
from savetokens.store import Usage

from conftest import H, T0, week_of_readings


def test_demand_follows_the_meter_and_the_rate_converts_dollars(store):
    start = T0 - 24 * H
    week_of_readings(store, start, 24, per_hour=1.0, resets=T0 + 3 * 86400)
    hist, rate = meter.demand(store, T0)
    assert abs(rate - 0.1) < 1e-9                       # $10 per 1%
    assert abs(sum(v for _, v in hist) - 23.0) < 1e-6    # 23 rises between 24 readings
    assert meter.active_account(store) == "a1"


def test_usage_before_the_first_reading_is_converted_at_the_rate(store):
    store.add_usage([Usage("claude-code", "s", "old", T0 - 48 * H, "claude-opus-5-5", cost_usd=50.0)])
    week_of_readings(store, T0 - 24 * H, 24, per_hour=1.0, resets=T0 + 3 * 86400)
    hist, _ = meter.demand(store, T0)
    assert dict(hist)[meter.hour_floor(T0 - 48 * H)] == 5.0


def test_a_rise_with_nothing_captured_is_spread_over_the_gap(store):
    r = T0 + 86400
    store.add_meter("claude-code", "a1", {"seven_day": (10.0, r)}, ts=T0 - 10 * H)
    store.add_meter("claude-code", "a1", {"seven_day": (20.0, r)}, ts=T0 - 5 * H)   # used elsewhere
    hist, _ = meter.demand(store, T0)
    assert abs(sum(v for _, v in hist) - 10.0) < 1e-9 and max(v for _, v in hist) < 2.5


def test_switching_accounts_keeps_demand(store):
    store.add_meter("claude-code", "a1", {"seven_day": (90.0, T0 + 86400)}, ts=T0 - 6 * H)
    store.add_meter("claude-code", "a1", {"seven_day": (100.0, T0 + 86400)}, ts=T0 - 4 * H)
    store.add_meter("claude-code", "a2", {"seven_day": (1.0, T0 + 5 * 86400)}, ts=T0 - 3 * H)
    store.add_meter("claude-code", "a2", {"seven_day": (6.0, T0 + 5 * 86400)}, ts=T0 - 2 * H)
    hist, _ = meter.demand(store, T0)
    assert abs(sum(v for _, v in hist) - 15.0) < 1e-9      # 10 on a1, 5 on a2
    assert meter.active_account(store) == "a2"


def test_five_hour_ratio_from_paired_readings(store):
    week_of_readings(store, T0 - 20 * H, 20, per_hour=1.0, resets=T0 + 86400, five_ratio=3.0)
    assert abs(meter.five_hour_ratio(store) - 3.0) < 0.6


def _flat_paths(store, account, per_hour, hours=48, start=T0):
    data = array("d", [per_hour] * hours * 10)
    forecast.save_paths(store, account, "baseline", start, meter.hour_floor(start), hours, data)


def test_outlook_projects_to_reset_and_times_the_hit(store):
    store.add_meter("claude-code", "a1", {"seven_day": (80.0, T0 + 30 * H)}, ts=T0)
    _flat_paths(store, "a1", 2.0)                     # 2% an hour: 100% in 10 hours
    o = {x["name"]: x for x in forecast.outlook(store, T0 + 1)}["seven_day"]
    assert o["p_hit"] == 1.0 and abs(o["eta"] - (T0 + 10 * H)) < 120
    assert alerts.stage(o, T0) == "act"               # certain to hit
    assert "around" in alerts.message(o, "act", T0)


def test_stages_climb_and_fire_once(store):
    store.add_meter("claude-code", "a1", {"seven_day": (50.0, T0 + 30 * H)}, ts=T0)
    _flat_paths(store, "a1", 2.0)
    first = alerts.check(store, T0 + 1)
    assert [a["stage"] for a in first] == ["act"]
    assert alerts.check(store, T0 + 2) == []          # once per window
    store.add_meter("claude-code", "a1", {"seven_day": (96.0, T0 + 30 * H)}, ts=T0 + 3)
    assert [a["stage"] for a in alerts.check(store, T0 + 4)] == ["last_call"]
    assert len(alerts.unseen(store, T0 + 5)) == 2 and alerts.unseen(store, T0 + 5) == []


def test_quiet_pace_raises_nothing(store):
    store.add_meter("claude-code", "a1", {"seven_day": (20.0, T0 + 30 * H)}, ts=T0)
    _flat_paths(store, "a1", 0.5)
    assert alerts.check(store, T0 + 1) == []


def test_projection_jump_is_flagged(store):
    store.add_meter("claude-code", "a1", {"seven_day": (20.0, T0 + 100 * H)}, ts=T0)
    rows = [("a1", "seven_day", "baseline", T0 - (10 - i) * H, T0 + 100 * H, 20, 30, 40 + (i % 2), 50, 0.0, None)
            for i in range(10)]
    rows.append(("a1", "seven_day", "baseline", T0 - 0.5 * H, T0 + 100 * H, 20, 50, 70, 90, 0.2, None))
    store.conn.executemany("INSERT INTO outlook VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    _flat_paths(store, "a1", 0.1, hours=120)
    flags = alerts.projection_flags(store, T0, "a1")
    assert flags and flags[0]["stage"].startswith("jump") and "jumped to 70%" in flags[0]["message"]


def test_refresh_cadence(store):
    assert maintain.refresh_due(store, None, T0)
    store.add_usage([Usage("claude-code", "s", "r", T0 - 30 * 60, "claude-opus-5-5", cost_usd=1.0)])
    assert not maintain.refresh_due(store, T0 - 40 * 60, T0)          # fresh
    assert maintain.refresh_due(store, T0 - 70 * 60, T0)              # an hour old while you work
    assert not maintain.refresh_due(store, T0 - 25 * 60, T0 + 2 * H)  # idle since: nothing new
    assert maintain.refresh_due(store, T0 - 25 * 60, T0 + 13 * H)     # but not forever


def test_engine_makes_baseline_paths_and_records_the_outlook(store):
    week_of_readings(store, T0 - 72 * H, 72, per_hour=0.5, resets=T0 + 86400)
    maintain.update(store, T0, use_ephemeris=False)
    assert forecast.load_paths(store, "a1", "baseline")
    assert store.conn.execute("SELECT COUNT(*) FROM outlook").fetchone()[0] == 2


def test_stale_readings_from_idle_sessions_are_not_counted_as_usage(store):
    """Two sessions: the busy one sees 1→2→3%, the idle one keeps reporting 1%. Real use is 2 points."""
    r = T0 + 86400
    for i, pct in enumerate([1.0, 2.0, 1.0, 3.0, 1.0, 3.0, 1.0]):
        store.add_meter("claude-code", "a1", {"seven_day": (pct, r)}, ts=T0 - 6 * H + i * 600)
    hist, _ = meter.demand(store, T0)
    assert abs(sum(v for _, v in hist) - 2.0) < 1e-9
    assert meter.latest(store, "seven_day", T0, "a1")["pct"] == 3.0       # not the idle session's 1%
