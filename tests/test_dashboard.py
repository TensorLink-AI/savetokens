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
