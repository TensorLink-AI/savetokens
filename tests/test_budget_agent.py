"""The budget agent: per budget, off, watch (the default), advise or guard."""
from __future__ import annotations

import json
import time

from savetokens import alerts, cli, hooks, pools, server
from savetokens.store import Usage


def _spend(store, provider, usd, harness="hermes", n=1):
    now = time.time()
    store.add_usage([Usage(harness, f"s-{provider}", f"{provider}-{i}-{usd}", now - 60 + i, "m", output=1000,
                           cost_usd=usd, billing="api", provider=provider) for i in range(n)])


def test_a_budget_says_which_work_counts_toward_it():
    chutes = {"scope": {"providers": ["chutes"]}, "count": "api"}
    assert pools.covers(chutes, "hermes", "chutes.ai", None, "api")
    assert not pools.covers(chutes, "hermes", "openrouter", None, "api")
    assert pools.covers(chutes, "codex", None, None, None)                  # provider not known: may count
    overall = {"scope": {}, "count": "api"}
    assert pools.covers(overall, "claude-code", "anthropic", "p1", "api")
    assert not pools.covers(overall, "claude-code", "anthropic", "p1", "plan")   # API spend only
    project = {"scope": {"projects": ["p1"]}, "count": "all"}
    assert pools.covers(project, "claude-code", "anthropic", "p1", "plan") and not pools.covers(
        project, "claude-code", "anthropic", "p2", "plan")
    assert pools.mode({}) == "watch" and pools.mode({"mode": "guard"}) == "guard"


def test_modes_are_set_from_the_cli_and_listed(store, capsys):
    assert cli.main(["budget", "--provider", "chutes", "--usd", "20", "--mode", "advise"]) == 0
    assert pools.goals_(store)["chutes"]["mode"] == "advise"
    assert cli.main(["budget", "--name", "Chutes", "--mode", "guard"]) == 0
    assert pools.goals_(store)["chutes"]["mode"] == "guard"
    capsys.readouterr()
    assert cli.main(["budget"]) == 0 and "[guard]" in capsys.readouterr().out
    assert cli.main(["budget", "--mode", "guard"]) == cli.ERROR                 # which budget?
    assert cli.main(["budget", "--name", "nope", "--mode", "off"]) == cli.ERROR


def test_guard_blocks_a_claude_prompt_once_its_budget_is_used_up(store, monkeypatch):
    gid = pools.add_budget(store, 10, "month", providers=["saygm.com"])
    _spend(store, "saygm.com", 6.0, harness="claude-code", n=2)                # $12 of $10
    monkeypatch.setattr(hooks.capture, "endpoint", lambda env=None: "saygm.com")
    payload = {"session_id": "s1", "cwd": None}
    got = hooks.handle("UserPromptSubmit", payload, store)
    assert not got or "decision" not in got                                   # watch: never blocks
    pools.set_mode(store, gid, "guard")
    got = hooks.handle("UserPromptSubmit", payload, store)
    assert got["decision"] == "block" and "saygm.com budget is used up" in got["reason"]
    monkeypatch.setattr(hooks.capture, "endpoint", lambda env=None: None)    # on the plan: not this budget
    monkeypatch.setattr(hooks.capture, "billing", lambda store=None: None)
    got = hooks.handle("UserPromptSubmit", {"session_id": "s2"}, store)
    assert not got or "decision" not in got


def test_guard_makes_check_exit_4_for_the_work_it_covers(store, capsys):
    gid = pools.add_budget(store, 5, "month", providers=["chutes.ai"])
    _spend(store, "chutes.ai", 6.0)
    pools.set_mode(store, gid, "guard")
    assert cli.main(["check", "--tool", "hermes", "--provider", "chutes"]) == cli.WONT_FIT
    assert "guarded" in capsys.readouterr().out
    assert cli.main(["check", "--tool", "hermes", "--provider", "openrouter"]) != cli.WONT_FIT


def test_advise_tells_the_agent_at_most_hourly(store, monkeypatch):
    gid = pools.add_budget(store, 10, "month", providers=["saygm.com"])
    pools.set_mode(store, gid, "advise")
    _spend(store, "saygm.com", 6.0, harness="claude-code", n=2)
    here = {"tool": "claude-code", "provider": "saygm.com", "project": None, "billing": "api"}
    text = hooks.advise(store, here, "s1")
    assert text and "Pace yourself" in text and "don't make them yourself" in text
    assert hooks.advise(store, here, "s1") is None                           # told: not again within the hour
    assert hooks.advise(store, {**here, "provider": "openrouter"}, "s2") is None


def test_off_raises_no_alerts(store, monkeypatch):
    gid = pools.add_budget(store, 10, "month", providers=["chutes.ai"])
    pools.set_mode(store, gid, "off")
    _spend(store, "chutes.ai", 9.5)
    assert not [a for a in alerts.check(store) if a["name"] == "goal"]


def test_the_hub_takes_a_mode_and_refuses_a_bad_one(running):
    users, token, url = running
    import urllib.request

    def post(body):
        req = urllib.request.Request(url + "/v1/budget", json.dumps(body).encode(),
                                     {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    assert post({"usd": 20, "provider": "chutes", "mode": "guard"}) == 200
    assert post({"id": "chutes", "mode": "advise"}) == 200
    assert post({"id": "chutes", "mode": "yolo"}) == 400
    with users.store("chris") as s:
        assert pools.goals_(s)["chutes"]["mode"] == "advise"
    good = {"label": "x", "unit": "usd", "amount": 1, "period": "month", "start": 1, "scope": {}}
    assert server._valid_goals({"x": {**good, "mode": "guard"}}) and not server._valid_goals({"x": {**good, "mode": "yolo"}})
