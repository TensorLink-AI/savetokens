"""The recap: under or over plan and forecast, limit hits, weekly on the next prompt, 30 days at install."""
from __future__ import annotations

from savetokens import maintain, pools, recap
from savetokens.store import Usage

from conftest import H, T0

D = 24 * H


def _spend(store, harness, usd, at, n=1, billing="api"):
    store.add_usage([Usage(harness, "s", f"r{harness}{at}{i}", at + i, "m", cost_usd=usd / n, billing=billing)
                     for i in range(n)])


def test_an_api_budget_shows_under_plan_and_where_the_period_is_heading(store):
    pools.set_budget(store, "hermes", 100, "week")
    _spend(store, "hermes", 30, T0 - 2 * D)
    r = recap.recap(store, T0, 7)
    b = r["budgets"][0]
    assert round(b["planned_usd"]) == 100 and round(b["under_usd"]) == 70
    out = recap.text(r)
    assert "Hermes budget: $30 of the $100 planned for these days, $70 under plan." in out
    _spend(store, "hermes", 200, T0 - D)
    assert "over plan" in recap.text(recap.recap(store, T0, 7)) and recap.recap(store, T0, 7)["budgets"][0]["runs_out"]


def test_a_weekly_limit_is_compared_with_the_forecast_made_early_that_week(store):
    ended = T0 - 2 * D
    for i, pct in enumerate([10, 30, 50, 62]):
        store.add_meter("claude-code", "a1", {"seven_day": (pct, ended)}, ts=ended - (6 - i) * D)
    store.conn.execute("INSERT INTO outlook (account, name, source, made_at, window_end, used, p50)"
                       " VALUES ('a1', 'seven_day', 'ephemeris', ?, ?, 10, 75)", (ended - 6 * D, ended))
    _spend(store, "claude-code", 400, T0 - 3 * D, billing=None)
    out = recap.text(recap.recap(store, T0, 7))
    assert "Claude Code $400 (API-equivalent, on your plan)" in out
    assert "weekly limit: ended at 62%" in out and "13 points under the 75% forecast early that week" in out
    assert "No limit hits." in out


def test_limit_hits_are_compared_with_the_days_before_and_nothing_means_no_recap(store):
    assert recap.text(recap.recap(store, T0, 7)) is None
    _spend(store, "claude-code", 5, T0 - D, billing=None)
    store.add_hits([("claude-code", "a1", "s", T0 - D, "weekly", None),
                    ("claude-code", "a1", "s", T0 - 9 * D, "weekly", None),
                    ("claude-code", "a1", "s2", T0 - 10 * D, "session", None)])
    assert "Limit hits: 1 (the 7 days before: 2)." in recap.text(recap.recap(store, T0, 7))


def test_the_weekly_recap_comes_once_a_week_with_the_alerts(store, monkeypatch):
    monkeypatch.setattr(maintain, "update", lambda *a, **k: [])
    from savetokens import capture
    monkeypatch.setattr(capture, "backfill", lambda *a, **k: 0)
    pools.set_budget(store, "hermes", 70, "week")
    _spend(store, "hermes", 20, T0 + 5 * D)            # during the first week
    assert maintain.run(store, T0) == []                       # the first week starts counting
    assert maintain.run(store, T0 + 3 * D) == []
    new = maintain.run(store, T0 + 7 * D)
    assert len(new) == 1 and new[0]["name"] == "recap" and "under plan" in new[0]["message"]
    assert maintain.run(store, T0 + 7 * D + H) == []           # not again for a week
    from savetokens import alerts
    assert any("under plan" in m for m in alerts.unseen(store, T0 + 7 * D + 2 * H))   # shown on the next prompt


def test_recap_command(store, capsys):
    from savetokens import cli
    import json
    pools.set_budget(store, "hermes", 70, "week")
    _spend(store, "hermes", 10, __import__("time").time() - D)
    assert cli.main(["recap", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["ok"] and got["days"] == 7 and got["budgets"][0]["tool"] == "hermes"


def test_early_resets_end_a_window_and_many_windows_make_one_line(store):
    # Codex resets early: the next window begins before the last one's reset time
    starts = [T0 - 9 * D, T0 - 7 * D, T0 - 5 * D, T0 - 3 * D]
    for k, s in enumerate(starts):
        for i, pct in enumerate([0, 40, 70, 100 if k == 1 else 80]):
            store.add_meter("codex", None, {"seven_day": (pct, s + 7 * D)}, ts=s + i * 10 * H)
    windows = recap._windows(store, T0)
    assert [round((w["ended"] - T0) / D) for w in windows] == [-7, -5, -3]    # each ended when the next began
    store.add_usage([Usage("codex", "s", "c1", T0 - D, "gpt-x", output=2_000_000)])   # no price known
    out = recap.text(recap.recap(store, T0, 8))
    assert "Codex 2.0M tokens" in out and "$0" not in out
    assert "Codex weekly limit: 3 windows ended, at 80–100%; you ran out in 1" in out


def test_readings_from_before_the_account_was_known_join_its_week(store):
    end = T0 - D
    store.add_meter("claude-code", None, {"seven_day": (11, end)}, ts=end - 6 * D)
    store.add_meter("claude-code", "a1", {"seven_day": (37, end)}, ts=end - 2 * D)
    assert [(w["account"], w["final"]) for w in recap._windows(store, T0)] == [("a1", 37)]
