import json

from savetokens import limits, report
from savetokens.adapters import claude_code as cc
from savetokens.store import UsageEvent

from conftest import T0


def test_fit_recovers_per_model_rates():
    # truth: Opus moves the weekly limit 0.02%/$, Sonnet 0.05%/$
    rows = []
    for i in range(1, 30):
        opus, sonnet = 10.0 * i, 3.0 * (i % 7)
        rows.append((0.02 * opus + 0.05 * sonnet, {"claude-opus-5-5": opus, "claude-sonnet-5-5": sonnet}))
    f = limits.fit(rows, shrink=0.01)
    assert abs(f["models"]["claude-opus-5-5"] - 0.02) < 1e-3
    # Sonnet has ~15x less spend, so it is pulled ~10% towards the pooled rate by design
    assert abs(f["models"]["claude-sonnet-5-5"] - 0.05) < 6e-3
    assert f["r2"] > 0.99


def test_sparse_model_shrinks_to_pooled():
    rows = [(1.0 * i, {"claude-opus-5-5": 50.0 * i}) for i in range(1, 20)]
    rows.append((20.0, {"claude-opus-5-5": 1000.0, "claude-haiku-4-5": 0.01}))
    f = limits.fit(rows)
    assert abs(f["models"]["claude-haiku-4-5"] - f["pooled"]) / f["pooled"] < 0.2


def test_account_plan_reads_plan_fields_only(homes, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (homes / "claude").mkdir(parents=True, exist_ok=True)
    (homes / "claude" / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "billingType": "stripe_subscription", "organizationType": "claude_max",
        "organizationRateLimitTier": "default_claude_max_20x", "hasExtraUsageEnabled": True,
        "emailAddress": "someone@example.com", "accountUuid": "uuid-1"}}))
    p = limits.account_plan()
    assert p["account"] and len(p["account"]) == 12 and "uuid" not in p["account"]
    assert {k: v for k, v in p.items() if k != "account"} == {
        "billing": "subscription", "plan": "claude_max", "tier": "default_claude_max_20x", "extra_usage": True}
    assert "someone" not in json.dumps(p)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert limits.account_plan()["billing"] == "api"


def test_statusline_tags_billing_and_model(store):
    cc.record_statusline({"session_id": "sub1", "model": {"id": "claude-opus-5-5"},
                          "rate_limits": {"seven_day": {"used_percentage": 10, "resets_at": T0 + 1e5}}}, store)
    cc.record_statusline({"session_id": "gw1", "model": {"id": "claude-sonnet-5-5"},
                          "rate_limits": {"spend_limit": {"used_percentage": 5, "resets_at": T0 + 1e5}}}, store)
    cc.record_statusline({"session_id": "sub1", "model": {"id": "claude-fable-5-1"}}, store)   # no limits yet
    rows = {r["session_id"]: r for r in store.conn.execute("SELECT * FROM sessions")}
    assert rows["sub1"]["billing"] == "subscription" and rows["sub1"]["model"] == "claude-fable-5-1"
    assert rows["sub1"]["source"] == "statusline"     # a later reading without limits doesn't downgrade it
    assert rows["gw1"]["billing"] == "api"


def _seed_week(store, rate=0.02):
    """Seven days of $100/day subscription usage and weekly readings consistent with `rate` %/$."""
    resets = T0 + 3 * 86400
    start = resets - 7 * 86400
    events, spent = [], 0.0
    t = start + 3600
    while t < T0:
        events.append(UsageEvent("claude-code", "sub", f"u{t}", t, model="claude-opus-5-5", cost_usd=100 / 24))
        t += 3600
    store.add_usage(events)
    for e in events[::6]:
        spent = sum(x.cost_usd for x in events if x.ts <= e.ts)
        store.add_limits("claude-code", "sub", ts=e.ts + 1, seven_day_pct=rate * spent, seven_day_resets=resets)
    store.set_meta("claude_code_billing", "subscription")
    return resets


def test_calibrate_learns_from_readings_and_logs_history(store):
    _seed_week(store, rate=0.02)
    cal = limits.calibrate(store, now=T0, force=True)
    assert abs(cal["seven_day"]["models"]["claude-opus-5-5"] - 0.02) < 1e-3
    assert limits.history(store)[-1]["pct_per_usd"] > 0
    limits.calibrate(store, now=T0 + 10)        # within the hour: no refit, no new log row
    assert len(limits.history(store)) == 1


def test_report_in_limit_units(store):
    _seed_week(store, rate=0.1)
    limits.calibrate(store, now=T0, force=True)
    r = report.build(store, days=7, now=T0)
    assert r["billing"]["subscription"]["pct7d"] > 0
    assert "of a weekly limit" in report.render(r)
