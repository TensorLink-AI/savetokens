"""The dashboard snapshot, its terminal drawing, and the server's copy of it."""
from __future__ import annotations

import json

from savetokens import dashboard, maintain
from savetokens.store import Usage

from conftest import H, T0, week_of_readings


def test_snapshot_has_everything_the_pane_draws_and_is_json(store):
    week_of_readings(store, T0 - 48 * H, 48, per_hour=1.5, resets=T0 + 20 * H)
    store.add_usage([Usage("claude-code", "s", "sub", T0 - 60, "claude-sonnet-5-5", subagent=True, cost_usd=5.0)])
    maintain.update(store, T0, use_ephemeris=False)
    snap = json.loads(json.dumps(dashboard.snapshot(store, T0)))
    assert {l["name"] for l in snap["limits"]} == {"five_hour", "seven_day"}
    assert all("stage" in l for l in snap["limits"])
    assert snap["demand"]["past"] and snap["demand"]["next"]
    assert snap["models"][0]["model"] == "claude-opus-5-5"
    assert snap["accounts"][0]["active"]


def test_render_draws_bars_sparklines_and_the_run_out_time(store):
    week_of_readings(store, T0 - 48 * H, 48, per_hour=1.5, resets=T0 + 20 * H)
    maintain.update(store, T0, use_ephemeris=False)
    lines = dashboard.render(dashboard.snapshot(store, T0), width=90, color=False)
    text = "\n".join(lines)
    assert "weekly limit" in text and "█" in text and "usage per hour" in text
    assert "out around" in text                        # 72% used at 1.5% an hour, 20 hours to go


def test_bar_shows_now_likely_and_high_end():
    assert dashboard.bar(25, 30, 50, 75, width=4) == "█▒░·"


def test_sessions_show_who_used_the_most_and_who_is_running(store):
    week_of_readings(store, T0 - 48 * H, 48, per_hour=1.0, resets=T0 + 20 * H)
    store.add_usage([Usage("claude-code", "big", f"b{i}", T0 - 120 - i, "claude-opus-5-5", cost_usd=20.0,
                           project="synth") for i in range(3)]
                    + [Usage("claude-code", "old", "o1", T0 - 5 * H, "claude-sonnet-5-5", cost_usd=5.0, project="web")])
    rows = {x["project"]: x for x in dashboard.top_sessions(store, T0, rate=0.1)}
    assert rows["synth"]["running"] and not rows["web"]["running"]
    assert rows["synth"]["share"] > rows["web"]["share"] and abs(rows["synth"]["pct_week"] - 6.0) < 1e-9
    assert abs(rows["synth"]["pace"] - 6.0) < 1e-9                      # all of it in the last hour
    text = "\n".join(dashboard.render(dashboard.snapshot(store, T0), width=100, color=False))
    assert "sessions, last 24h" in text and "synth" in text


def test_project_names_are_never_synced():
    from savetokens.store import SYNCED
    assert "project" not in SYNCED["usage"]


def test_stopping_a_busy_session_moves_the_run_out_time(store):
    from savetokens import alerts, forecast
    from array import array
    store.add_meter("claude-code", "a1", {"seven_day": (90.0, T0 + 30 * H)}, ts=T0)
    forecast.save_paths(store, "a1", "baseline", T0, T0 - T0 % H, 48, array("d", [2.0] * 48 * 10))
    store.add_usage([Usage("claude-code", "busy", f"b{i}", T0 - 60 * i, "claude-opus-5-5", cost_usd=6.0,
                           project="synth") for i in range(1, 10)]
                    + [Usage("claude-code", "small", "s1", T0 - 120, "claude-opus-5-5", cost_usd=6.0, project="web")])
    rows = {x["project"]: x for x in dashboard.top_sessions(store, T0, rate=0.1)}
    synth, web = rows["synth"]["if_stopped"], rows["web"]["if_stopped"]
    # 90% used at 2% an hour: out in 5h. synth is 90% of the pace; pausing it for 5h gets to 91%, then 4.5h more
    assert abs(synth["eta"] - (T0 + 5 * H)) < 120 and abs(synth["eta_if_stopped"] - (T0 + 9.5 * H)) < 120
    assert web["eta"] < web["eta_if_stopped"] < synth["eta_if_stopped"]
    o = {x["name"]: x for x in forecast.outlook(store, T0)}["seven_day"]
    assert alerts.best_pause(store, T0, o) == " Pausing synth would buy about 4.5 h."
    assert "out " in dashboard.stopping(rows["web"], T0) and "→" in dashboard.stopping(rows["web"], T0)
