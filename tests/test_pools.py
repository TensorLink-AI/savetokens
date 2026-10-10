"""Codex capture, the pools (Claude Code's and Codex's plans, API budgets), and budgets across machines."""
from __future__ import annotations

import json
from array import array

from savetokens import alerts, codex, dashboard, forecast, maintain, meter, pools, sync
from savetokens.store import Store, Usage

from conftest import H, T0, iso, week_of_readings


class Rollout:
    """Codex style rollout JSONL."""

    def __init__(self, path, sid="c1", cwd="/work/api", parent=None):
        self.path, self.total = path, 0
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {"id": sid, "cwd": cwd, **({"parent_thread_id": parent} if parent else {})}
        self._write({"timestamp": iso(T0 - 10 * H), "type": "session_meta", "payload": meta})
        self._write({"timestamp": iso(T0 - 10 * H), "type": "turn_context", "payload": {"model": "gpt-6-astra"}})

    def _write(self, d):
        with open(self.path, "a") as f:
            f.write(json.dumps(d) + "\n")

    def turn(self, ts, week=None, resets=T0 + 3 * 86400, tokens=10_000, limit_id="codex", repeat=False):
        if not repeat:
            self.total += tokens
        rl = {"limit_id": limit_id, "plan_type": "pro" if week is not None else None, "primary": None}
        if week is not None:
            rl["primary"] = {"used_percent": week, "window_minutes": 10080, "resets_at": int(resets)}
        info = {"last_token_usage": {"input_tokens": tokens, "cached_input_tokens": tokens // 2, "output_tokens": 100,
                                     "total_tokens": tokens + 100},
                "total_token_usage": {"total_tokens": self.total}}
        self._write({"timestamp": iso(ts), "type": "event_msg",
                     "payload": {"type": "token_count", "info": info, "rate_limits": rl}})


def rollout(homes, name="rollout-2026-01-01-c1.jsonl", **kw):
    return Rollout(homes / "codex" / "sessions" / "2026" / name, **kw)


def test_codex_sessions_give_usage_and_the_plan_meter(store, homes):
    r = rollout(homes)
    for i in range(6):
        r.turn(T0 - (6 - i) * H, week=10.0 + 2 * i)
    r.turn(T0 - H + 60, week=20.0, repeat=True)                      # repeated event: nothing new used
    r.turn(T0 - H + 120, week=90.0, limit_id="codex_fastmodel")      # another model's own limit: not the plan's
    assert codex.backfill(store) == 7                                    # every turn but the repeat
    rows = store.conn.execute("SELECT harness, project, billing, cost_usd FROM usage").fetchall()
    assert {tuple(x) for x in rows} == {("codex", "api", None, None)}   # no built-in price for Codex models
    assert meter.latest(store, "seven_day", T0, harness="codex")["pct"] == 20.0
    hist, rate = meter.demand(store, T0, harness="codex")
    assert abs(sum(v for _, v in hist) - 10.0) < 1e-9 and rate > 0      # 10% → 20%, weighed by tokens
    assert codex.backfill(store) == 0                                    # read once


def test_codex_on_an_api_key_has_no_meter_and_is_billed_as_api(store, homes):
    (homes / "codex").mkdir()
    (homes / "codex" / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}))
    rollout(homes).turn(T0 - H)
    codex.backfill(store)
    assert store.conn.execute("SELECT billing FROM usage").fetchone()[0] == "api"
    assert meter.harnesses(store) == []


def test_subagent_sessions_are_marked(store, homes):
    rollout(homes, "rollout-x-c2.jsonl", sid="c2", parent="c1").turn(T0 - H, week=5.0)
    codex.backfill(store)
    assert store.conn.execute("SELECT subagent FROM usage").fetchone()[0] == 1


def test_claude_and_codex_plans_are_separate_pools(store, homes):
    week_of_readings(store, T0 - 48 * H, 48, per_hour=0.2, resets=T0 + 86400)    # Claude: well within its limits
    r = rollout(homes)
    for i in range(48):
        r.turn(T0 - (48 - i) * H, week=1.5 * (i + 1), resets=T0 + 2 * 86400)
    codex.backfill(store)
    assert [p.id for p in pools.pools(store, T0)] == ["claude-code", "codex"]
    new = maintain.update(store, T0, use_ephemeris=False)
    looks = {(o["pool"], o["name"]): o for o in forecast.outlook(store, T0)}
    assert looks[("claude-code", "seven_day")]["label"] == "weekly limit"
    cx = looks[("codex", "seven_day")]
    assert cx["label"] == "Codex weekly limit" and cx["used"] == 72.0
    assert cx["eta"] and cx["eta"] < T0 + 2 * 86400                    # 1.5% an hour: out in ~19h, before the reset
    assert "Codex weekly limit" in {a["message"].split(":")[0] for a in new}
    assert not alerts.check(store, T0)                                  # once per stage
    snap = dashboard.snapshot(store, T0)
    assert snap["demand"]["pool"] == "codex"                            # the chart follows the pool running out
    assert "Codex weekly limit" in snap["headline"]["text"]
    assert "Codex wk" in "\n".join(dashboard.render(snap, width=100, color=False))


def test_interleaved_windows_are_followed_separately(store):
    # two Codex accounts can't be told apart: their readings interleave, each in its own window
    for i in range(6):
        store.add_meter("codex", None, {"seven_day": (10.0 + i, T0 + 86400)}, ts=T0 - (12 - 2 * i) * H)
        store.add_meter("codex", None, {"seven_day": (50.0 + i, T0 + 3 * 86400)}, ts=T0 - (11 - 2 * i) * H)
    hist, _ = meter.demand(store, T0, harness="codex")
    assert abs(sum(v for _, v in hist) - (5 + 50 + 5)) < 1e-9   # 5 + 5 of rises, plus the second window's first 50


def _api_usage(store, harness="claude-code", usd_per_hour=2.0, hours=72):
    store.add_usage([Usage(harness, "k", f"{harness}{i}", T0 - (hours - i) * H + 60, "claude-sonnet-5-5",
                           cost_usd=usd_per_hour, billing="api", project="ci") for i in range(hours)])


def test_an_api_budget_is_a_limit_in_dollars(store):
    pools.set_budget(store, "claude-code", 100, "week", tz=0)
    _api_usage(store)
    assert [p.id for p in pools.pools(store, T0)] == ["claude-code:api"]
    hist, rate = pools.history(store, pools.pools(store, T0)[0], T0)
    assert rate is None and abs(sum(v for _, v in hist) - 144.0) < 1e-9
    maintain.update(store, T0, use_ephemeris=False)
    o = forecast.outlook(store, T0)[0]
    start, end = pools.period(pools.budgets(store)["claude-code"], T0)
    spent = 2.0 * sum(1 for i in range(72) if T0 - (72 - i) * H + 60 >= start)
    assert o["name"] == "budget" and abs(o["spent_usd"] - spent) < 1e-9 and abs(o["used"] - spent) < 1e-9
    assert o["p50"] is not None and o["resets"] == end
    st = alerts.stage(o, T0)
    if st:
        assert "of $100 a week" in alerts.message(o, st, T0)
    assert "$" in "\n".join(dashboard.render(dashboard.snapshot(store, T0), width=100, color=False))


def test_a_monthly_budget_projects_past_the_weeks_of_paths(store):
    pools.set_budget(store, "codex", 1000, "month", tz=0)
    p = pools.pools(store, T0)[0]
    _, end = pools.period(p.budget, T0)
    forecast.save_paths(store, p.key, "baseline", T0, meter.hour_floor(T0), 168, array("d", [1.0] * 168 * 10))
    o = forecast.outlook(store, T0)[0]
    assert abs(o["p50"] - 100.0 * (end - T0) / H / 1000) < 0.2         # $1 an hour to the month's end


def test_budget_periods():
    b = {"usd": 1, "period": "month", "tz": 0}
    s, e = pools.period(b, 1_790_000_000)                                # 2026-09-21 UTC
    assert (s, e) == (1_788_220_800, 1_790_812_800)                      # 1 Sep to 1 Oct
    s, e = pools.period({**b, "period": "week"}, 1_790_000_000)
    assert e - s == 7 * 86400 and s <= 1_790_000_000 < e
    s, _ = pools.period({**b, "period": "day", "tz": 3600}, 1_790_000_000)
    assert (s + 3600) % 86400 == 0


def test_budgets_and_api_usage_reach_other_machines(running, tmp_path):
    users, token, url = running
    cfg = {"server_url": url, "server_token": token}
    a, b = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    pools.set_budget(a, "claude-code", 50, "day", tz=0)
    _api_usage(a, hours=30)
    sync.push(a, cfg)
    with users.store("chris") as s:
        assert s.meta("budgets")["claude-code"]["usd"] == 50
        assert s.conn.execute("SELECT COUNT(*) FROM usage WHERE billing = 'api'").fetchone()[0] == 30
        maintain.update(s, T0, use_ephemeris=False)
    sync.pull(b, cfg)
    assert pools.budgets(b)["claude-code"]["period"] == "day"
    assert forecast.outlook(b, T0)[0]["source"] == "baseline"           # the server's paths


def test_an_older_client_without_billing_can_still_push(running):
    _, token, url = running
    from savetokens.store import USAGE_COLS
    cols = [c for c in USAGE_COLS if c not in ("project", "billing")]
    row = [{"harness": "claude-code", "session_id": "s", "request_id": "r", "ts": T0, "machine": "old"}.get(c, 0)
           for c in cols]
    assert sync._call({"server_url": url, "server_token": token}, "/v1/push",
                      {"machine": "old", "table": "usage", "cols": cols, "rows": [row]})["added"] == 1


def test_a_price_counts_codex_usage_against_its_budget(store, homes, monkeypatch):
    from savetokens import cli, pricing
    (homes / "codex").mkdir()
    (homes / "codex" / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}))
    rollout(homes).turn(T0 - H, tokens=1_000_000)
    codex.backfill(store)
    store.close()
    assert cli.main(["price", "gpt-6-astra", "2", "8"]) == 0
    with Store() as s:
        usd = s.conn.execute("SELECT cost_usd FROM usage").fetchone()[0]
    assert abs(usd - (500_000 * 2 + 500_000 * 0.2 + 100 * 8) / 1e6) < 1e-9   # half the input was cached
    assert pricing.rates("gpt-6-astra") == (2.0, 8.0, 0.2)
