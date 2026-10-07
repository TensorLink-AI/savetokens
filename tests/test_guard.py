import io
import json

import pytest

from savetokens import cli, guard
from savetokens.adapters import claude_code as cc
from savetokens.store import ToolEvent, UsageEvent, load_config, save_config

from conftest import T0


def post(store, tool, inp, *, ok=True, sid="s1", tid=None, event=None):
    payload = {"hook_event_name": event or ("PostToolUse" if ok else "PostToolUseFailure"), "session_id": sid,
               "tool_name": tool, "tool_input": inp, "tool_use_id": tid, "tool_response": {"stdout": "ok"}}
    return cc.handle_hook(payload["hook_event_name"], payload, store)


def test_tool_loop_warns_once_within_threshold(store):
    outs = [post(store, "Bash", {"command": "ls  -la"}, tid=f"t{i}") for i in range(6)]
    assert outs[:3] == [None, None, None]
    assert "4 times in a row" in outs[3]["hookSpecificOutput"]["additionalContext"]
    assert outs[4] is None and outs[5] is None   # fired once, not on every call


def test_whitespace_variants_count_as_the_same_call(store):
    for i, cmd in enumerate(["ls -la", "ls  -la", " ls -la", "ls -la "]):
        out = post(store, "Bash", {"command": cmd}, tid=f"t{i}")
    assert "4 times in a row" in out["systemMessage"]


def test_repeated_reads_reset_by_edit(store):
    for i in range(3):
        assert post(store, "Read", {"file_path": "/a.py", "offset": i}, tid=f"r{i}") is None
    post(store, "Edit", {"file_path": "/a.py", "old_string": "a", "new_string": "b"}, tid="e1")
    for i in range(3):
        assert post(store, "Read", {"file_path": "/a.py", "offset": 10 + i}, tid=f"r{10 + i}") is None
    out = post(store, "Read", {"file_path": "/a.py", "offset": 99}, tid="r99")
    assert "read 4 times" in out["systemMessage"]


def test_failing_tests_without_edit(store):
    for i in range(2):
        assert post(store, "Bash", {"command": f"pytest -x tests/ -k t{i}"}, ok=False, tid=f"p{i}") is None
    out = post(store, "Bash", {"command": "cd app && pytest"}, ok=False, tid="p2")
    assert "failed 3 runs in a row" in out["hookSpecificOutput"]["additionalContext"]


def test_passing_run_or_edit_breaks_the_failing_streak(store):
    post(store, "Bash", {"command": "pytest a"}, ok=False, tid="a")
    post(store, "Bash", {"command": "pytest b"}, ok=False, tid="b")
    post(store, "Edit", {"file_path": "/x.py"}, tid="e")
    assert post(store, "Bash", {"command": "pytest c"}, ok=False, tid="c") is None


def test_burn_rule_uses_the_sessions_own_pace(store):
    cfg = load_config()
    # steady $0.50 per 5 minutes for an hour, then $5 in the last five minutes
    events = [UsageEvent("claude-code", "s1", f"r{i}", T0 + i * 300, model="claude-opus-5-5", cost_usd=0.5)
              for i in range(12)]
    events.append(UsageEvent("claude-code", "s1", "spike", T0 + 12 * 300 - 60, model="claude-opus-5-5", cost_usd=5))
    store.add_usage(events)
    alerts = guard.evaluate(store, "claude-code", "s1", cfg=cfg, now=T0 + 12 * 300)
    burn = [a for a in alerts if a.rule == "burn"]
    assert burn and "x its usual pace" in burn[0].message
    # a steady session does not alert
    store.add_usage([UsageEvent("claude-code", "s2", f"q{i}", T0 + i * 300, cost_usd=0.5) for i in range(12)])
    assert not [a for a in guard.evaluate(store, "claude-code", "s2", cfg=cfg, now=T0 + 3600) if a.rule == "burn"]


def test_context_alert_goes_to_user_only(store):
    store.add_usage([UsageEvent("claude-code", "s1", "r1", T0, cache_read=250_000, cost_usd=0.05)])
    alerts = guard.evaluate(store, "claude-code", "s1", now=T0 + 1)
    ctx = [a for a in alerts if a.rule == "context"]
    assert ctx and ctx[0].audience == guard.USER


def test_block_is_off_by_default_and_denies_when_on(store):
    for i in range(4):
        post(store, "Bash", {"command": "make"}, tid=f"m{i}")
    pre = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "make"}}
    assert cc.handle_hook("PreToolUse", pre, store) is None
    cfg = load_config()
    cfg["block"] = True
    save_config(cfg)
    out = cc.handle_hook("PreToolUse", pre, store)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    pre["tool_input"] = {"command": "make test-other"}
    assert cc.handle_hook("PreToolUse", pre, store) is None


def test_hook_entry_point_fails_open(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["hook", "claude-code"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_entry_point_emits_json(monkeypatch, capsys):
    for i in range(4):
        payload = {"hook_event_name": "PostToolUse", "session_id": "s9", "tool_name": "Grep",
                   "tool_input": {"pattern": "foo"}, "tool_use_id": f"g{i}", "tool_response": {}}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        cli.main(["hook", "claude-code"])
    out = capsys.readouterr().out
    assert json.loads(out)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_parallel_subagents_are_not_one_loop(store):
    for i in range(4):
        payload = {"session_id": "s1", "agent_id": f"a{i}", "tool_name": "Grep", "tool_input": {"pattern": "x"},
                   "tool_use_id": f"g{i}", "tool_response": {}}
        assert cc.handle_hook("PostToolUse", payload, store) is None
    assert {t.session_id for t in store.tools()} == {"s1/a0", "s1/a1", "s1/a2", "s1/a3"}


def test_surge_needs_two_complete_hours_above_the_forecast_p99(store):
    from array import array
    from savetokens import guard, windows
    from savetokens.store import UsageEvent, load_config
    h = T0 - T0 % 3600
    data = array("d")
    for p in range(100):
        data.extend([0.5 + p / 100] * 24)                 # p99 about $1.48 every hour
    for unit in windows.UNITS:   # billing is unknown in tests, so usage may count as either unit
        windows.save_paths(store, "baseline", unit, h - 3 * 3600, h - 3 * 3600, 24, data)
    cfg = load_config()
    spend = lambda sid, hour, usd, n: store.add_usage([UsageEvent("claude-code", sid, f"{sid}{hour}{i}",
                                                                  hour + 60 * (i + 1), model="claude-sonnet-5-5",
                                                                  cost_usd=usd / n) for i in range(n)])
    spend("loud", h - 3600, 3.0, 3)                       # one hot hour: not enough on its own
    assert guard.surge(store, h + 60, cfg, "quiet") is None
    spend("loud", h - 7200, 2.0, 2)                       # the hour before was hot too
    a = guard.surge(store, h + 60, cfg, "quiet")
    assert a.rule == "surge" and a.audience == guard.USER and "session loud" in a.message
    assert guard.surge(store, h + 60, cfg, "loud").audience == guard.AGENT
    assert windows.surge_thresholds(store, "baseline", "sub_usd")[h - 7200] == pytest.approx(1.48, abs=0.01)
