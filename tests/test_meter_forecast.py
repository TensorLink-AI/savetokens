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


def test_another_accounts_window_under_this_accounts_label_isnt_usage(store):
    a1, a2 = T0 + 5 * 86400, T0 + 5 * 86400 - 6 * H          # a1's new week; a2's started 6 hours earlier
    store.add_meter("claude-code", "a1", {"seven_day": (0.0, a1)}, ts=T0 - 4 * H)
    for i, pct in enumerate([24.0, 21.0, 23.0]):               # a2's meter, read under a1's label
        store.add_meter("claude-code", "a1", {"seven_day": (pct, a2)}, ts=T0 - 4 * H + 300 * (i + 1))
        store.add_meter("claude-code", "a1", {"seven_day": (1.0, a1)}, ts=T0 - 4 * H + 300 * (i + 1) + 60)
    hist, _ = meter.demand(store, T0)
    assert abs(sum(v for _, v in hist) - 1.0) < 1e-9          # a1 used 1%, not 24% in an hour
    assert meter.latest(store, "seven_day", T0, account="a1")["pct"] == 1.0
    assert meter.latest(store, "seven_day", T0)["pct"] == 1.0


def test_each_claude_config_folder_reads_its_own_account(store, monkeypatch, tmp_path):
    from savetokens import capture, hooks
    monkeypatch.setattr(capture, "account", lambda: {"account": "acct-" + __import__("os").environ["CLAUDE_CONFIG_DIR"][-1]})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "a"))
    assert hooks._account(store) == "acct-a"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "b"))   # within the minute: not a's cached account
    assert hooks._account(store) == "acct-b"


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
    o = {x["name"]: x for x in forecast.outlook(store, T0)}["seven_day"]
    flags = alerts.projection_flags(store, T0, o)
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


def test_each_account_has_its_own_rate_so_a_5x_and_a_20x_plan_dont_blend(store):
    from savetokens import meter
    from savetokens.store import Usage
    for acct, pct_per_usd in (("max5", 0.4), ("max20", 0.1)):   # a 5x plan fills four times as fast per dollar
        for i in range(12):
            t = T0 - 12 * H + i * H
            store.add_meter("claude-code", acct, {"seven_day": (pct_per_usd * 10 * (i + 1), T0 + 3 * 86400)}, ts=t)
            store.add_usage([Usage("claude-code", acct, f"{acct}{i}", t - 60, "claude-opus-5-5", cost_usd=10.0,
                                   account=acct)])
    assert abs(meter.rate(store, account="max5") - 0.4) < 0.01
    assert abs(meter.rate(store, account="max20") - 0.1) < 0.01
    assert 0.1 < meter.rate(store) < 0.4                                   # together, they blend
    assert meter.rate(store, account="new") == meter.rate(store)          # no data of its own yet: the pooled rate


def test_a_limit_reset_early_with_the_same_reset_time_is_followed(store):
    """The meter reaches 50%, is reset to ~10% (its reset time unchanged), and climbs to 30%: 20 points used since,
    which must show as usage, not be hidden under the old 50% until it's passed."""
    r = T0 + 4 * 86400
    t = T0 - 20 * H
    for pct in [40, 45, 50]:                                    # before the early reset
        store.add_meter("claude-code", "a1", {"seven_day": (pct, r)}, ts=t)
        t += 600
    store.add_meter("claude-code", "a1", {"seven_day": (50, r)}, ts=T0 - 16 * H)   # an idle session's last word
    for i, pct in enumerate(range(10, 31, 2)):                  # after it: every reading lower than 50
        store.add_meter("claude-code", "a1", {"seven_day": (float(pct), r)}, ts=T0 - 12 * H + i * 1800)
    hist, _ = meter.demand(store, T0)
    used = sum(v for h, v in hist if h >= T0 - 12 * H - 3600)
    assert abs(used - 20.0) < 1e-6
    assert meter.latest(store, "seven_day", T0, "a1")["pct"] == 30.0     # not the 50% from before the reset
