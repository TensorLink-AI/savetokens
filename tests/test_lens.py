"""ALL, API or PLANS: what the dashboard counts, and the data each view needs."""
from __future__ import annotations

from pathlib import Path

import pytest

from savetokens import cli, dashboard, pools, spend
from savetokens.store import Store, Usage

from conftest import H, T0

WEB = Path(__file__).resolve().parents[1] / "src/savetokens/web"


def _use(store, sid, harness, n, at=T0 - H, billing=None, provider=None, usd=1.0):
    store.add_usage([Usage(harness, sid, f"{sid}-{i}", at + i, "m", output=1000, cost_usd=usd, billing=billing,
                           provider=provider) for i in range(n)])


def test_sessions_say_who_serves_them_and_each_billing_keeps_its_own_top_list(store):
    for i in range(10):
        _use(store, f"plan{i}", "claude-code", 5)                               # on the plan
    _use(store, "say1", "claude-code", 2, billing="api", provider="saygm.com", usd=0.01)
    got = dashboard.top_sessions(store, T0)
    api = [x for x in got if x.get("billing") == "api" and x["session"]]
    assert [x["provider"] for x in api] == ["saygm.com"] and api[0]["usd"] == pytest.approx(0.02)
    assert sum(1 for x in got if x.get("billing") == "plan" and x["session"]) == 8   # the plan's top 8 ...
    assert any(x["session"] is None and x["billing"] == "plan" for x in got)        # ... and the rest, summed


def test_spend_says_how_many_tokens_were_paid_as_you_go(store):
    _use(store, "a", "claude-code", 3)
    _use(store, "b", "claude-code", 2, billing="api", provider="saygm.com")
    month = spend.summary(store, T0)["periods"]["month"]
    assert month["tokens"] == 5000 and month["api_tokens"] == 2000


def test_budgets_for_two_providers_are_two_budgets(store, capsys):
    assert cli.main(["budget", "--provider", "saygm.com", "--usd", "20", "--per", "month"]) == 0
    assert cli.main(["budget", "--provider", "chutes", "--usd", "20", "--per", "month"]) == 0
    with Store() as s:
        labels = sorted(g["label"] for g in pools.goals_(s).values())
    assert labels == ["Chutes", "saygm.com"]


def test_every_tab_follows_the_lens():
    html, js = (WEB / "index.html").read_text(), (WEB / "app.js").read_text()
    for lens in ('data-lens="all"', 'data-lens="api"', 'data-lens="plans"', 'id="plancards"'):
        assert lens in html
    for part in ("function drawPlans", "inLens(x.billing)", "inLens(m.billing)", "apiBudgets(s)", 'h.get("lens")'):
        assert part in js
