"""Where a project's tokens went (tool, provider, plan or API), budgets that say what they count, and project
names on your own hub only when you ask."""
from __future__ import annotations

import json

import pytest

from savetokens import cli, dashboard, forecast, pools, projects, sync
from savetokens.store import Store, Usage

from conftest import H, T0
from test_projects import _repo


def _use(store, cwd, harness, usd, billing=None, provider=None, n=4, at=T0 - H):
    store.add_usage([Usage(harness, "s", f"{cwd}{harness}{billing}{at}{i}", at + i * 60, "m", cost_usd=usd / n,
                           output=1000, cwd=cwd, thread="s", billing=billing, provider=provider) for i in range(n)])


def test_a_project_splits_by_tool_provider_and_plan_or_api(store, tmp_path):
    web = str(_repo(tmp_path / "webapp", "git@github.com:org/webapp.git"))
    _use(store, web, "claude-code", 30)                                         # on the plan
    _use(store, web, "codex", 10, billing="api")                                # on an API key: real money
    _use(store, web, "hermes", 5, billing="api", provider="openrouter")
    p = projects.report(store, T0 - 86400)[0]
    assert p["name"] == "webapp" and p["value_usd"] == pytest.approx(45) and p["api_usd"] == pytest.approx(15)
    got = {(b["tool"], b["provider"], b["billing"]): b["value_usd"] for b in p["by"]}
    assert got == {("claude-code", "anthropic", "plan"): pytest.approx(30), ("codex", "openai", "api"): pytest.approx(10),
                   ("hermes", "openrouter", "api"): pytest.approx(5)}


def test_a_budget_counts_api_spend_or_everything_and_can_name_providers(store, tmp_path):
    web = str(_repo(tmp_path / "webapp", "git@github.com:org/webapp.git"))
    _use(store, web, "claude-code", 30)
    _use(store, web, "codex", 10, billing="api")
    _use(store, web, "hermes", 5, billing="api", provider="openrouter")
    pid = projects.resolve(store, web)[0]
    base = {"label": "webapp", "unit": "usd", "amount": 100, "period": "month", "start": T0 - 86400, "tz": 0}
    spent = {}
    for gid, extra in {"api": {"count": "api", "scope": {"projects": [pid]}},
                       "all": {"count": "all", "scope": {"projects": [pid]}},
                       "old": {"scope": {"projects": [pid]}},                     # set before --count: everything
                       "openai": {"count": "api", "scope": {"projects": [pid], "providers": ["openai"]}},
                       "codex": {"count": "all", "scope": {"tools": ["codex"]}}}.items():
        pools.set_goal(store, gid, {**base, **extra})
    for p in pools.pools(store, T0, goals=True):
        if p.kind == "goal":
            since, _ = pools.period(p.budget, T0 - H)
            spent[p.id.split(":")[1]] = pools.spent(store, p, since - 86400, T0)
    assert spent == {"api": pytest.approx(15), "all": pytest.approx(45), "old": pytest.approx(45),
                     "openai": pytest.approx(10), "codex": pytest.approx(10)}
    o = next(o for o in forecast.outlook(store, T0, goals=True) if o["pool"] == "goal:openai")
    assert o["counts"] == "API spend · OpenAI"


def test_budget_flags_and_the_note_when_nothing_was_paid_as_api(store, tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "webapp", "git@github.com:org/webapp.git")
    _use(store, str(repo), "claude-code", 30)
    store.close()
    monkeypatch.chdir(repo)
    assert cli.main(["budget", "--project", ".", "--usd", "100"]) == 0
    out = capsys.readouterr().out
    assert "counting API spend" in out and "--count all" in out                 # it's all on the plan
    assert cli.main(["budget", "--project", ".", "--usd", "100", "--count", "all", "--provider", "Anthropic",
                     "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["count"] == "all" and got["scope"]["providers"] == ["anthropic"] and got["counted_last_30_days"] == 4


def test_projects_and_tasks_commands_show_the_split(store, tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "webapp", "git@github.com:org/webapp.git")
    _use(store, str(repo), "claude-code", 30)
    _use(store, str(repo), "codex", 10, billing="api")
    store.close()
    assert cli.main(["projects", "--days", "3650", "--json"]) == 0
    p = json.loads(capsys.readouterr().out)["projects"][0]
    assert p["api_usd"] == pytest.approx(10) and len(p["by"]) == 2
    assert cli.main(["projects", "--days", "3650"]) == 0
    assert "Claude Code plan 50% · Codex API (OpenAI) 50%" in capsys.readouterr().out


def test_the_projects_tab_has_projects_and_budgets(store, tmp_path):
    web = str(_repo(tmp_path / "webapp", "git@github.com:org/webapp.git"))
    _use(store, web, "codex", 10, billing="api")
    pid = projects.resolve(store, web)[0]
    pools.set_goal(store, "webapp", {"label": "webapp", "scope": {"projects": [pid]}, "count": "api", "unit": "usd",
                                     "amount": 100, "period": "month", "start": T0 - 86400, "tz": 0})
    pv = dashboard.snapshot(store, T0)["projects"]
    assert pv["list"][0]["id"] == pid and pv["list"][0]["api_usd"] == pytest.approx(10)
    assert pv["budgets"][0]["scope"]["projects"] == [pid] and pv["unnamed"] == 0


def test_names_reach_your_hub_only_when_you_ask(running, tmp_path):
    users, token, url = running
    cfg = {"server_url": url, "server_token": token}
    s = Store(tmp_path / "a.db")
    repo = str(_repo(tmp_path / "webapp", "git@github.com:org/webapp.git"))
    _use(s, repo, "claude-code", 3)
    sync.push(s, cfg)
    with users.store("chris") as hub:
        pid = hub.conn.execute("SELECT project_id FROM usage").fetchone()[0]
        assert pid and projects.names(hub) == {}                                # ids only, by default
    sync.push(s, {**cfg, "share_names": True})
    with users.store("chris") as hub:
        assert projects.names(hub) == {pid: "webapp"}
        assert dashboard.project_view(hub, T0)["list"][0]["name"] == "webapp"
    s.set_meta("pushed:names", None)                                            # --share-names off
    sync.push(s, cfg)
    with users.store("chris") as hub:
        assert projects.names(hub) == {}
    s.close()


def test_the_hub_rejects_budgets_with_bad_scopes():
    from savetokens.server import _valid_goals
    g = {"label": "w", "unit": "usd", "amount": 5, "period": "month", "start": 1.0, "scope": {"providers": ["openai"]},
         "count": "api"}
    assert _valid_goals({"w": g})
    assert not _valid_goals({"w": {**g, "count": "some"}})
    assert not _valid_goals({"w": {**g, "scope": {"tools": "codex"}}})


def test_each_project_card_has_its_last_seven_days_and_sessions(store, tmp_path):
    web = str(_repo(tmp_path / "webapp", "git@github.com:org/webapp.git"))
    _use(store, web, "claude-code", 3, at=T0 - H)                    # today
    _use(store, web, "claude-code", 3, at=T0 - 3 * 86400 - H)        # three days back
    p = dashboard.project_view(store, T0)["list"][0]
    assert p["last_days"] == [0, 0, 0, 4000, 0, 0, 4000] and p["sessions"] == 1


def test_the_page_is_the_retro_terminal_one_and_keeps_plan_and_api_apart():
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "src/savetokens/web"
    css, js, html = ((web / f).read_text() for f in ("app.css", "app.js", "index.html"))
    assert "VT323" in css and "Red Hat Mono" in css and "style=" not in html   # no inline styles: the CSP allows none
    for part in ("ADD MACHINE", "ALREADY HAS SAVETOKENS", "CONTAINER OR CI", "SPEND MIX", "BUDGETS"):
        assert part in html
    assert "badge(\"subscription\")" in js and "badge(\"api\")" in js and "last_days" in js
