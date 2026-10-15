"""The decide mode: a decision model's call, from measured facts, acted on; guard's rule when it can't answer."""
from __future__ import annotations

import os
import time

from savetokens import alerts, cli, decide, hooks, pools
from savetokens.store import Usage


def _spend(store, provider, usd, model="m", harness="hermes", n=1, tokens=1000, ago=60):
    now = time.time()
    store.add_usage([Usage(harness, f"s-{provider}", f"{provider}-{model}-{i}-{usd}-{ago}", now - ago + i, model,
                           output=tokens, cost_usd=usd, billing="api", provider=provider) for i in range(n)])


def _model(monkeypatch, do, ask="nothing", sent=None):
    """The decision model answers `do` and `ask`; what it was sent goes in `sent`."""
    def post(body, key):
        if sent is not None:
            sent.append(body)
        return {"id": "dec_1", "model": body["model"], "tier": "eu", "usage": {"input_tokens": 400, "output_tokens": 0,
                "decisions": 2}, "answers": {
                    "do": {"type": "choice", "choice": do, "confidence": 0.9, "probabilities": {do: 0.9}},
                    "ask": {"type": "choice", "choice": ask, "confidence": 0.8, "probabilities": {ask: 0.8}}}}
    monkeypatch.setenv("DECISIONMODELS_API_KEY", "dm_test")
    monkeypatch.setattr(decide, "_post", post)


def test_the_choices_are_the_ones_that_apply(store):
    gid = pools.add_budget(store, 10, "month", providers=["chutes.ai"])
    g = pools.goals_(store)[gid]
    _spend(store, "chutes.ai", 3.0, model="big", tokens=1_000_000)              # $3 per million tokens
    _spend(store, "chutes.ai", 0.5, model="small", tokens=1_000_000, ago=5 * 86400)
    from savetokens import forecast
    o = next(x for x in forecast.outlook(store, goals=True) if x["pool"] == f"goal:{gid}")
    here = {"tool": "hermes", "provider": "chutes.ai", "project": None, "billing": "api"}
    f = decide.facts(store, o, g, here, time.time())
    assert f["model_now"]["model"] == "big" and f["cheaper_model"]["model"] == "small"
    do, ask = decide.choices(f, g)
    assert {"carry_on", "finish_then_stop", "cheaper_model", "wait_for_reset", "stop_now"} <= set(do)
    assert "$0.50 per million tokens" in do["cheaper_model"] and "pause_project" not in ask
    f["used_pct"] = 100.0                                                        # used up: only the person raises it
    assert "carry_on" not in decide.choices(f, g)[0]
    assert "wait_for_reset" not in decide.choices(f, {**g, "period": None, "by": 1})[0]
    assert "pause_project" in decide.choices(f, {**g, "scope": {"projects": ["p1"]}})[1]


def test_only_numbers_and_labels_are_sent_to_the_eu_tier(store, monkeypatch):
    gid = pools.add_budget(store, 5, "month", providers=["chutes.ai"])
    pools.set_mode(store, gid, "decide")
    _spend(store, "chutes.ai", 6.0)
    sent = []
    _model(monkeypatch, "stop_now", sent=sent)
    here = {"tool": "hermes", "provider": "chutes.ai", "project": "p-secret", "billing": "api"}
    [(_, _, _, d)] = decide.calls(store, here)
    assert d["do"] == "stop_now" and sent[0]["model"] == decide.MODEL
    state = sent[0]["state"]
    assert state["used_pct"] == 120.0 and state["work"] == {"tool": "Hermes", "provider": "chutes.ai", "billing": "api"}
    assert "p-secret" not in str(sent[0])
    assert set(sent[0]["questions"]) == {"do", "ask"}


def test_the_hook_does_what_the_model_calls(store, monkeypatch):
    gid = pools.add_budget(store, 10, "month", providers=["saygm.com"])
    pools.set_mode(store, gid, "decide")
    _spend(store, "saygm.com", 6.0, harness="claude-code")
    monkeypatch.setattr(hooks.capture, "endpoint", lambda env=None: "saygm.com")
    monkeypatch.setattr(alerts, "stage", lambda o, now: "act")                  # at risk
    _model(monkeypatch, "finish_then_stop", ask="raise_budget")
    got = hooks.handle("UserPromptSubmit", {"session_id": "s1"}, store)
    assert "decision" not in got and "decision model's call, 90% sure" in got["systemMessage"]
    assert "whether to raise this budget" in got["systemMessage"]
    assert "don't make those changes yourself" in got["hookSpecificOutput"]["additionalContext"]
    assert not hooks.handle("UserPromptSubmit", {"session_id": "s1"}, store)     # told: not again within the hour
    store.set_meta("decision_cache", {})
    _model(monkeypatch, "wait_for_reset")
    got = hooks.handle("UserPromptSubmit", {"session_id": "s1"}, store)
    assert got["decision"] == "block" and "Hold new work until the budget resets" in got["reason"]
    store.set_meta("decision_cache", {})
    _model(monkeypatch, "carry_on")
    assert not hooks.handle("UserPromptSubmit", {"session_id": "s2"}, store)


def test_without_an_answer_guards_rule_holds(store, monkeypatch):
    gid = pools.add_budget(store, 10, "month", providers=["saygm.com"])
    pools.set_mode(store, gid, "decide")
    _spend(store, "saygm.com", 6.0, harness="claude-code", n=2)                  # $12 of $10
    monkeypatch.setattr(hooks.capture, "endpoint", lambda env=None: "saygm.com")
    got = hooks.handle("UserPromptSubmit", {"session_id": "s1"}, store)         # no key
    assert got["decision"] == "block" and "guard's rule holds" in got["reason"]
    tries = []
    monkeypatch.setenv("DECISIONMODELS_API_KEY", "dm_test")
    monkeypatch.setattr(decide, "_post", lambda b, k: tries.append(1) or (_ for _ in ()).throw(TimeoutError()))
    assert hooks.handle("UserPromptSubmit", {"session_id": "s1"}, store)["decision"] == "block"
    assert hooks.handle("UserPromptSubmit", {"session_id": "s1"}, store)["decision"] == "block"
    assert len(tries) == 1                                                       # a failure isn't retried at once
    assert not store.meta("decisions")                                           # and isn't logged as a call


def test_check_exits_4_when_the_model_stops_the_work(store, monkeypatch, capsys):
    gid = pools.add_budget(store, 5, "month", providers=["chutes.ai"])
    pools.set_mode(store, gid, "decide")
    _spend(store, "chutes.ai", 6.0)
    _model(monkeypatch, "stop_now")
    assert cli.main(["check", "--tool", "hermes", "--provider", "chutes"]) == cli.WONT_FIT
    assert "Stop new work" in capsys.readouterr().out
    assert cli.main(["check", "--tool", "hermes", "--provider", "openrouter"]) != cli.WONT_FIT


def test_each_budget_builds_a_track_record(store, monkeypatch, capsys):
    gid = pools.add_budget(store, 10, "month", providers=["chutes.ai"])
    pools.set_mode(store, gid, "decide")
    _spend(store, "chutes.ai", 11.0)
    _model(monkeypatch, "stop_now")
    here = {"tool": "hermes", "provider": "chutes.ai", "project": None, "billing": "api"}
    decide.calls(store, here)
    entries = store.meta("decisions")
    entries[0]["until"] = time.time() - 1                                        # its period has ended
    store.set_meta("decisions", entries)
    r = decide.record(store, gid)
    assert r["calls"] == 1 and r["by_answer"] == {"stop_now": 1} and r["periods_ended"] == 1
    assert r["periods_within"] == 0 and r["cost_usd"] > 0
    assert cli.main(["decide"]) == 0
    out = capsys.readouterr().out
    assert "1 calls (stop now 1)" in out and "within budget in 0 of 1" in out


def test_the_key_is_kept_owner_only(monkeypatch, capsys):
    monkeypatch.setenv("DECISIONMODELS_API_KEY", "dm_secret")
    assert cli.main(["decide", "--key"]) == 0
    assert "dm_secret" not in capsys.readouterr().out
    monkeypatch.delenv("DECISIONMODELS_API_KEY")
    assert decide.api_key() == "dm_secret"
    if os.name != "nt":
        assert decide.key_path().stat().st_mode & 0o777 == 0o600
        decide.key_path().chmod(0o644)
        assert decide.api_key() is None


def test_decide_is_a_mode_everywhere(store, capsys):
    assert cli.main(["budget", "--provider", "chutes", "--usd", "20", "--mode", "decide"]) == 0
    assert "savetokens decide --key" in capsys.readouterr().out                 # no key yet: says so
    assert pools.goals_(store)["chutes"]["mode"] == "decide"
    from savetokens import server
    good = {"label": "x", "unit": "usd", "amount": 1, "period": "month", "start": 1, "scope": {}, "mode": "decide"}
    assert server._valid_goals({"x": good})
