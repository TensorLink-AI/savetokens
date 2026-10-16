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


def test_the_dashboard_shows_the_call_that_needs_you_and_your_answer_clears_it(store, monkeypatch):
    from savetokens import dashboard
    gid = pools.add_budget(store, 5, "month", providers=["chutes.ai"])
    pools.set_mode(store, gid, "decide")
    _spend(store, "chutes.ai", 6.0)
    _model(monkeypatch, "wait_for_reset", ask="raise_budget")
    decide.calls(store, {"tool": "hermes", "provider": "chutes.ai", "project": None, "billing": "api"})
    snap = dashboard.snapshot(store)
    [now] = snap["decide_now"]
    assert now["id"] == gid and now["blocks"] and now["short"] == "Stopped: waits for the reset" and not now["pause"]
    [b] = snap["projects"]["budgets"]
    assert b["decision"]["history"][0]["sent"]["used_pct"] == 120.0 and "wait_for_reset" in b["decision"]["history"][0]["choices"]
    pools.apply_budget(store, {"id": gid, "usd": 20})                           # RAISE BUDGET
    assert pools.goals_(store)[gid]["amount"] == 20 and not dashboard.snapshot(store)["decide_now"]
    try:
        pools.apply_budget(store, {"id": gid, "usd": -1})
        raise AssertionError("a bad amount was taken")
    except ValueError:
        pass


def test_calls_go_to_the_hub_once_each(running, store, monkeypatch):
    users, token, url = running
    import json as _json
    import urllib.request
    gid = pools.add_budget(store, 5, "month", providers=["chutes.ai"])
    pools.set_mode(store, gid, "decide")
    _spend(store, "chutes.ai", 6.0)
    _model(monkeypatch, "stop_now")
    decide.calls(store, {"tool": "hermes", "provider": "chutes.ai", "project": "p1", "billing": "api"})
    calls = decide.unsent(store)
    assert len(calls) == 1 and "here" not in calls[0]

    def post(body):
        req = urllib.request.Request(url + "/v1/settings", _json.dumps(body).encode(),
                                     {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return _json.load(r)
    assert post({"machine": "laptop", "decisions": calls + [{"at": "x"}]})["added"] == 1   # a bad one is dropped
    assert post({"machine": "laptop", "decisions": calls})["added"] == 0                 # and none twice
    with users.store("chris") as s:
        [d] = s.meta("decisions")
        assert d["machine"] == "laptop" and d["do"] == "stop_now"


def test_each_choice_carries_its_measured_what_if_and_the_track_record_goes_back(store, monkeypatch):
    from array import array

    from savetokens import forecast, meter
    gid = pools.add_budget(store, 100, "month", providers=["chutes.ai"])
    pools.set_mode(store, gid, "decide")
    _spend(store, "chutes.ai", 30.0, model="big", tokens=10_000_000)            # $3 per million, today
    _spend(store, "chutes.ai", 0.5, model="small", tokens=1_000_000, ago=5 * 86400)
    now = time.time()
    forecast.save_paths(store, f"goal:{gid}", "baseline", now, meter.hour_floor(now), 800, array("d", [0.1] * 800 * 10))
    o = next(x for x in forecast.outlook(store, now, goals=True) if x["pool"] == f"goal:{gid}")
    here = {"tool": "hermes", "provider": "chutes.ai", "project": None, "billing": "api"}
    f = decide.facts(store, o, pools.goals_(store)[gid], here, now)
    w = f["if_chosen"]
    assert w["stop_now"]["p50"] == round(o["used"]) and w["carry_on"]["p50"] == round(o["p50"])
    assert w["stop_now"]["p50"] < w["cheaper_model"]["p50"] < w["carry_on"]["p50"]   # repriced at $0.50/M
    assert "finish_then_stop" not in w                                         # no finished tasks: not measured
    do, _ = decide.choices(f, pools.goals_(store)[gid])
    assert "likely ends the period at" in do["carry_on"] and "isn't measured" in do["finish_then_stop"]
    assert f["track_record"] == {"periods_ended": 0, "periods_within_budget": 0, "last_calls": []}
    sent = []
    _model(monkeypatch, "cheaper_model", sent=sent)
    d = decide.ask_model(store, gid, o, pools.goals_(store)[gid], here, now, "dm_test")
    assert sent[0]["model"] == "s1-pro-3" and decide.runner_up({**d, "probabilities": {"cheaper_model": 0.6,
                                                                                         "carry_on": 0.3}})["p"] == 0.3
    f2 = decide.facts(store, o, pools.goals_(store)[gid], here, now)
    assert f2["track_record"]["last_calls"][0]["call"] == "cheaper_model"


def test_the_dashboard_line_gives_each_choice_its_likely_end():
    d = {"choices": {"carry_on": "", "finish_then_stop": "", "move": "", "stop_now": ""},
         "facts": {"room_elsewhere": [{"name": "B"}],
                   "if_chosen": {"carry_on": {"p10": 44, "p50": 44, "p90": 46}, "stop_now": {"p10": 43, "p50": 43, "p90": 43},
                                 "move": {"p10": 43, "p50": 43, "p90": 43, "there_pct": 17}}}}
    assert decide.would(d) == "carry on → 44% (44–46%) · finish then stop: not measured · move to B → 43%, B 17% · stop now → 43%"
    assert decide.would({"choices": {"stop_now": ""}, "facts": {}}) is None   # a call from before the what-ifs
