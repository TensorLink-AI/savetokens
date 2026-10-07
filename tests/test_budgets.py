"""Pay-as-you-go budgets: scope, periods, pacing, OpenRouter's own figures, Hermes pricing."""
from __future__ import annotations

from datetime import datetime

import pytest

from conftest import T0
from savetokens import budgets, limits, steer
from savetokens.adapters import hermes
from savetokens.store import UsageEvent, load_config, save_config

NOON = datetime(2026, 9, 15, 12).timestamp()     # a Tuesday


def set_budgets(*items, daily=None):
    cfg = load_config()
    cfg["budgets"] = list(items)
    if daily:
        cfg["daily_budget_usd"] = daily
    save_config(cfg)


def spend(store, provider="openrouter", harness="hermes", per_hour=1.0, days=10, until=NOON, sid="h1"):
    evs = []
    t = until - days * 86400
    i = 0
    while t < until:
        evs.append(UsageEvent(harness, sid, f"{sid}:{provider}:{i}", t + 60, model="m", cost_usd=per_hour,
                              provider=provider))
        t += 3600
        i += 1
    store.add_usage(evs)


def test_configured_includes_the_daily_shorthand():
    set_budgets({"usd": 200, "period": "month", "provider": "openrouter"}, daily=20)
    names = [b["name"] for b in budgets.configured()]
    assert names == ["openrouter-month", "daily"]


def test_periods_are_calendar_day_week_month():
    s, e = budgets.period_bounds("week", NOON)
    assert datetime.fromtimestamp(s).weekday() == 0 and e - s == 7 * 86400
    s, e = budgets.period_bounds("month", NOON)
    assert datetime.fromtimestamp(s).day == 1 and datetime.fromtimestamp(e) == datetime(2026, 10, 1)


def test_spend_counts_only_the_scope_and_never_subscriptions(store):
    spend(store, provider="openrouter")
    spend(store, provider="api.engy.ai", sid="h2")
    spend(store, provider="anthropic", harness="claude-code", sid="c1")
    limits.record_session(store, "claude-code", "c1", billing=limits.SUBSCRIPTION, model="m", source="statusline")
    day = {"usd": 50, "period": "day"}
    assert budgets.spent(store, {**day, "provider": "openrouter"}, NOON)[0] == pytest.approx(12.0)
    assert budgets.spent(store, day, NOON)[0] == pytest.approx(24.0)          # Claude's subscription excluded
    assert budgets.spent(store, {**day, "harness": "claude-code"}, NOON)[0] == 0


def test_pacing_by_hour_for_a_day(store):
    spend(store, per_hour=1.0)
    tight = budgets.pace(store, {"name": "t", "usd": 15, "period": "day", "provider": "openrouter"}, NOON)
    assert tight["spent"] == pytest.approx(12.0) and tight["p_over"] == 1.0      # $1/h: ~$24 by midnight
    assert tight["forecast"][1] == pytest.approx(24.0, abs=1.5) and tight["runs_out"] < tight["resets"]
    roomy = budgets.pace(store, {"name": "r", "usd": 60, "period": "day", "provider": "openrouter"}, NOON)
    assert roomy["p_over"] == 0 and roomy["runs_out"] is None


def test_pacing_by_day_for_a_month(store):
    spend(store, per_hour=1.0, days=20)
    p = budgets.pace(store, {"name": "m", "usd": 1000, "period": "month", "provider": "openrouter"}, NOON)
    # 14.5 days spent at $24/day, 15.5 to go
    assert p["spent"] == pytest.approx(14.5 * 24, abs=1) and p["forecast"][1] == pytest.approx(720, abs=30)
    assert p["p_over"] == 0
    assert budgets.pace(store, {"name": "m2", "usd": 500, "period": "month", "provider": "openrouter"},
                        NOON)["p_over"] == 1.0


def test_openrouter_figures_replace_local_totals(store):
    spend(store, per_hour=1.0)
    store.set_meta("openrouter_key", {"usage_daily": 40.0, "limit_remaining": 5.0, "fetched_at": NOON - 60})
    p = budgets.pace(store, {"name": "o", "usd": 100, "period": "day", "provider": "openrouter"}, NOON)
    assert p["spent"] == 40.0 and p["spent_from"] == "openrouter"
    assert p["p_over"] == 1.0          # only $5 left on the key's limit, with ~$12 still to come today


def test_hermes_sees_its_budgets_not_claude_limits(store):
    spend(store, per_hour=1.0)
    set_budgets({"usd": 15, "period": "day", "harness": "hermes"})
    rows = steer.pressure(store, NOON, harness="hermes")
    assert [r["window"] for r in rows] == ["budget:hermes-day"] and rows[0]["p_hit"] == 1.0
    assert rows[0]["used"] == pytest.approx(80.0)


def test_ephemeris_quantiles_are_used_while_they_cover_the_period(store):
    from savetokens import forecast
    spend(store, per_hour=1.0)
    b = {"name": "e", "usd": 100, "period": "day", "provider": "openrouter"}
    start = NOON - NOON % 3600 - 3600                     # made an hour ago
    store.set_meta(f"budget_eph:e", {"step": 3600, "start": start, "made_at": start,
                                     "q": [{"0.1": 5.0, "0.5": 5.0, "0.9": 5.0}] * 20})
    store.set_meta("ephemeris_hourly", {"made_at": NOON})
    assert forecast.preferred_source(store) == "ephemeris"
    p = budgets.pace(store, b, NOON)
    assert p["source"] == "ephemeris" and p["forecast"][1] == pytest.approx(12 + 5 * 12, abs=1)


def test_included_routes_count_as_subscription(store):
    hermes.on_api_request(store, session_id="s", model="gpt-x", provider="openai-codex", ended_at=T0,
                          usage={"input_tokens": 100, "output_tokens": 10}, cost_status="included", cost_usd=0)
    hermes.on_api_request(store, session_id="p", model="kimi-k3", provider="custom", base_url="https://api.engy.ai/v1",
                          ended_at=T0, usage={"input_tokens": 1000, "output_tokens": 100}, cost_usd=0.003,
                          cost_status="estimated", cost_source="models_dev")
    rows = {e.session_id: e for e in store.usage(harness="hermes")}
    assert rows["p"].provider == "api.engy.ai" and rows["p"].cost_usd == 0.003
    assert rows["p"].cost_source == "hermes:models_dev"
    assert limits.billing_map(store)["s"] == limits.SUBSCRIPTION and limits.billing_map(store)["p"] == limits.API


def test_plugin_pricing_fails_soft_outside_hermes():
    from savetokens import hermes_plugin
    assert hermes_plugin._hermes_cost("m", {"input_tokens": 1}, "openrouter", None) == {}


@pytest.fixture
def hermes_cfg(monkeypatch):
    """A fake `hermes config` backed by a dict."""
    from savetokens import levers
    conf = {"model": {"provider": "openrouter", "model": "moonshotai/kimi-k3"},
            "fallback_providers": [{"provider": "engy", "model": "deepseek-v4-flash"}],
            "auxiliary": {"compression": {"provider": "openrouter", "model": "moonshotai/kimi-k3"},
                          "title": {"provider": "", "model": ""}},
            "openrouter": {"min_coding_score": 0.65}}

    def get(key):
        d = conf
        for part in key.split("."):
            if not isinstance(d, dict) or part not in d:
                return levers.MISSING
            d = d[part]
        return d

    def put(key, value):
        parts = key.split(".")
        d = conf
        for part in parts[:-1]:
            d = d.setdefault(part, {})
        if value == levers.MISSING:
            d.pop(parts[-1], None)
        else:
            d[parts[-1]] = value
    monkeypatch.setattr(levers, "_hermes_get", get)
    monkeypatch.setattr(levers, "_hermes_set", put)
    monkeypatch.setattr(levers, "_hermes_exe", lambda cfg=None: "/usr/bin/hermes")
    return conf


def test_hermes_levers_pace_a_budget_and_undo(store, hermes_cfg, monkeypatch):
    import copy
    from savetokens import levers
    store.set_meta("hermes_prices", {"openrouter|moonshotai/kimi-k3": {"in": 2.0, "out": 10.0},
                                     "engy|deepseek-v4-flash": {"in": 0.04, "out": 0.09}})
    cands = {c["role"]: c for c in levers.hermes_candidates(store)}
    assert set(cands) == {"main", "fallback", "side:compression"} and cands["fallback"]["usd_per_mtok"] < 0.1
    cfg = load_config()
    cfg["levers_consent"] = ["hermes"]
    cfg["levers"] = list(levers.DEFAULT_LEVERS) + ["compaction"]      # compaction is opt-in
    save_config(cfg)
    before = copy.deepcopy(hermes_cfg)
    monkeypatch.setattr(levers, "_risk", lambda store, h, now: (0.6, NOON + 3600, "$15/day budget"))
    out = levers.update(store, NOON, harnesses=("hermes",))
    assert sorted(out["hermes"]["applied"]) == sorted(["side tasks on your cheapest configured model",
                                                       "compress context at 150k tokens"])
    assert hermes_cfg["auxiliary"]["compression"] == {"provider": "engy", "model": "deepseek-v4-flash"}
    assert hermes_cfg["model"]["model"] == "moonshotai/kimi-k3"          # the main model is never touched
    assert hermes_cfg["openrouter"]["min_coding_score"] == 0.65          # not on the pareto router
    levers.update(store, NOON + 7200, harnesses=("hermes",))             # the period resets

    def prune(d):     # unsetting a key can leave its empty section behind; the settings are the same
        return {k: prune(v) for k, v in d.items() if v != {}} if isinstance(d, dict) else d
    assert prune(hermes_cfg) == prune(before)


def test_pareto_router_gets_a_lower_score(store, hermes_cfg, monkeypatch):
    from savetokens import levers
    hermes_cfg["model"] = "openrouter/pareto-code"
    lv = next(x for x in levers.LEVERS["hermes"] if x.id == "quality-score")
    assert lv.changes(store) == {"openrouter.min_coding_score": 0.5}
    hermes_cfg["openrouter"]["min_coding_score"] = 0.4
    assert lv.changes(store) == {}
