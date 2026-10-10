"""Spend and tokens by provider and model: so far, projected, coming; subscriptions as a fixed cost."""
from __future__ import annotations

import json
import sqlite3

from savetokens import cli, hermes, maintain, mcp, pools, spend, sync
from savetokens.store import Store, Usage

from conftest import H, T0


def test_provider_names():
    assert hermes.provider_name("openrouter", "https://openrouter.ai/api/v1") == "openrouter"
    assert hermes.provider_name("custom", "https://api.synthetic.new/v1") == "synthetic.new"
    assert hermes.provider_name("", "https://llm.chutes.ai/v1") == "chutes.ai"
    assert hermes.provider_name("custom", "http://localhost:8080/v1") == "local"
    assert hermes.provider_name(None, None) == "unknown"
    assert spend.label("synthetic.new") == "Synthetic" and spend.label("acme.dev") == "acme.dev"


def test_rows_from_before_providers_get_theirs(tmp_path):
    path = tmp_path / "old.db"
    with Store(path) as s:
        s.add_usage([Usage("claude-code", "s", "r1", T0, "m"), Usage("hermes", "h", "h1|m|custom|https://api.x.io/v1|"
                                                                     "official_models_api|:100:0", T0, "m")])
    con = sqlite3.connect(path)          # back to v3: no provider column
    con.execute("ALTER TABLE usage DROP COLUMN provider")
    con.execute("PRAGMA user_version=3")
    con.commit()
    con.close()
    with Store(path) as s:
        got = dict(s.conn.execute("SELECT harness, provider FROM usage").fetchall())
    assert got == {"claude-code": "anthropic", "hermes": "x.io"}


def _usage(store, provider, hours, tokens=1_000_000, usd=1.0, harness="hermes", billing="api", model="m1"):
    store.add_usage([Usage(harness, f"{provider}{i}", f"{provider}{model}{i}", T0 - (hours - i) * H + 60, model,
                           input=tokens, cost_usd=usd, billing=billing, provider=provider) for i in range(hours)])


def test_spend_counts_pay_as_you_go_and_plans_and_projects_them(store):
    _usage(store, "openrouter", 72, usd=2.0)
    _usage(store, "openrouter", 72, usd=1.0, model="m2")
    _usage(store, "synthetic.new", 72, usd=0.5)                          # a flat-rate provider: on a plan
    _usage(store, "anthropic", 72, usd=3.0, harness="claude-code", billing=None, model="opus")   # Claude Max
    pools.set_plan(store, "anthropic", 300, "month", tz=0)
    pools.set_plan(store, "synthetic.new", 30, "month", tz=0)
    store.set_meta("tz", 0)
    maintain.update(store, T0, use_ephemeris=False)
    s = spend.summary(store, T0)
    by = {e["provider"]: e for e in s["providers"]}
    day = {p: e["periods"]["day"] for p, e in by.items()}
    start, end = pools.period({"period": "day", "tz": 0}, T0)
    hours = sum(1 for i in range(72) if T0 - (72 - i) * H + 60 >= start)
    assert day["openrouter"]["usd"] == 3.0 * hours and day["openrouter"]["fixed_usd"] == 0
    assert day["synthetic.new"]["usd"] == 0 and abs(day["synthetic.new"]["fixed_usd"] - 30 / 30 * (T0 - start) / 86400) < 1e-6
    assert day["anthropic"]["usd"] == 0 and day["anthropic"]["api_value"] == 3.0 * hours
    assert day["anthropic"]["tokens"] == 1_000_000 * hours
    assert by["openrouter"]["usd_per_mtok"] == 1.5                       # $3 per 2M tokens an hour
    # projections: so far plus what's coming, a whole plan by the period's end
    o = day["openrouter"]["projected"]
    assert o["tokens"][1] > day["openrouter"]["tokens"] and o["cost"][1] > day["openrouter"]["usd"]
    syn = day["synthetic.new"]["projected"]
    assert abs(syn["cost"][1] - 1.0) < 1e-6 and syn["fixed_usd"] == 1.0  # $30 a 30-day month: $1 a day
    assert by["openrouter"]["source"] == "baseline"
    nxt = s["periods"]["month"]["next"]
    assert nxt["cost"][0] <= nxt["cost"][1] <= nxt["cost"][2] and nxt["fixed_usd"] > 0
    # totals add up, and models split their provider by recent share
    t = s["periods"]["day"]
    assert abs(t["cost"] - sum(x["cost"] for x in day.values())) < 1e-6
    models = {m["model"]: m for m in by["openrouter"]["models"]}
    assert abs(models["m1"]["share_usd"] - 2 / 3) < 1e-9 and models["m1"]["share_tokens"] == 0.5
    assert models["m1"]["periods"]["day"]["next_usd"] > models["m2"]["periods"]["day"]["next_usd"]


def test_a_fixed_cost_is_spread_over_its_period():
    plan = {"usd": 300, "period": "month", "tz": 0}
    s, e = pools.period(plan, T0)
    assert abs(spend.fixed_cost(plan, s, e) - 300) < 1e-9
    assert abs(spend.fixed_cost(plan, s, s + 86400) - 300 / ((e - s) / 86400)) < 1e-9
    assert abs(spend.fixed_cost({**plan, "period": "week"}, s, s + 14 * 86400) - 600) < 1e-9


def test_spend_on_the_command_line_and_for_agents(store, capsys, monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: T0)
    _usage(store, "openrouter", 48)
    store.close()
    assert cli.main(["plan", "anthropic", "--usd", "200", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["plan"]["usd"] == 200
    assert cli.main(["spend", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["ok"] and {e["provider"] for e in got["providers"]} == {"openrouter", "anthropic"}
    assert cli.main(["spend", "--models"]) == 0
    text = capsys.readouterr().out
    assert "OpenRouter" in text and "Anthropic (plan)" in text and "  m1" in text
    with Store() as s:
        out = mcp.call("spend_summary", {}, store=s)
    assert out.startswith("today:") and "OpenRouter" in out


def test_plans_reach_other_machines(running, tmp_path):
    users, token, url = running
    cfg = {"server_url": url, "server_token": token}
    a, b = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    pools.set_plan(a, "anthropic", 200, "month", tz=0)
    _usage(a, "openrouter", 30)
    sync.push(a, cfg)
    with users.store("chris") as s:
        assert pools.plans(s)["anthropic"]["usd"] == 200 and s.meta("tz") is not None
        assert {e["provider"] for e in spend.summary(s, T0)["providers"]} == {"anthropic", "openrouter"}
    sync.pull(b, cfg)
    assert pools.plans(b)["anthropic"]["usd"] == 200


def test_an_older_savetokens_setting_the_version_back_is_harmless(tmp_path):
    path = tmp_path / "s.db"
    Store(path).close()
    con = sqlite3.connect(path)
    con.execute("PRAGMA user_version=3")              # what an older version does when it opens the store
    con.execute("INSERT INTO usage (machine, harness, session_id, request_id, ts) VALUES ('m', 'codex', 's', 'r', 1)")
    con.commit()
    con.close()
    with Store(path) as s:
        assert s.conn.execute("SELECT provider FROM usage").fetchone()[0] == "openai"
