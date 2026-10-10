"""The brief, its options, job estimates, the MCP server, the agent's note, billing detection and join codes."""
from __future__ import annotations

import io
import json
from array import array

import pytest

from savetokens import advise, capture, forecast, hooks, install, maintain, mcp, meter, pricing, sync
from savetokens.store import Store, Usage, save_config

from conftest import H, T0, week_of_readings


def _busy(store, ctx=200_000):
    """A running session with Opus subagents and a long context, on top of a week of readings."""
    week_of_readings(store, T0 - 72 * H, 72, per_hour=1.0, resets=T0 + 30 * H, usd_per_pct=1.0)
    rows = []
    for i in range(30):
        t = T0 - 3000 + i * 90
        rows.append(Usage("claude-code", "synth-1", f"m{i}", t, "claude-opus-5-5", input=2, cache_read=ctx,
                          output=500, cost_usd=0.05, project="synth"))
        rows.append(Usage("claude-code", "synth-1", f"s{i}", t + 30, "claude-opus-5-5", subagent=True,
                          input=10_000, cache_read=40_000, output=2_000, project="synth",
                          cost_usd=pricing.cost("claude-opus-5-5", input=10_000, cache_read=40_000, output=2_000)))
    store.add_usage(rows)
    maintain.update(store, T0, use_ephemeris=False)


def test_the_brief_ranks_measured_options_and_knows_this_session(store):
    _busy(store)
    b = advise.brief(store, T0, cwd="/work/synth")
    assert b["this_session"]["project"] == "synth" and 0.5 < b["this_session"]["subagents"] < 0.8   # by cost
    ids = [o["id"] for o in b["options"]]
    assert "subagents" in ids and "fresh" in ids
    measured = [o["points"] for o in b["options"] if o["points"]]
    assert measured == sorted(measured, reverse=True)
    sub = next(o for o in b["options"] if o["id"] == "subagents")
    assert "model: sonnet" in sub["how"] and sub["points"] > 0
    text = advise.brief_text(b)
    assert text.startswith(b["headline"]["text"]) and "Options, biggest first" in text


def test_an_estimate_waits_for_the_5_hour_reset_and_suggests_a_later_start(store):
    store.add_meter("claude-code", "a1", {"seven_day": (12.0, T0 + 100 * H), "five_hour": (90.0, T0 + 2 * H)}, ts=T0)
    forecast.save_paths(store, "a1", "baseline", T0, meter.hour_floor(T0), 120, array("d", [0.0] * 120 * 10))
    for k in range(4):   # past sessions: 2 points an hour each, for 2 hours (rate 0.1% per $)
        store.add_usage([Usage("claude-code", f"p{k}", f"p{k}-{i}", T0 - (10 + k) * 86400 + i * 300, "claude-opus-5-5",
                               cost_usd=20 * 300 / 3600, project="synth") for i in range(25)])
    store.add_meter("claude-code", "a1", {"seven_day": (10.0, T0 + 100 * H)}, ts=T0 - 3 * H)
    store.add_meter("claude-code", "a1", {"seven_day": (12.0, T0 + 100 * H)}, ts=T0 - 2 * H)
    store.add_usage([Usage("claude-code", "x", "x1", T0 - 2.5 * H, "claude-opus-5-5", cost_usd=20.0)])
    e = advise.estimate(store, T0, points=4, cwd="/work/synth")
    assert e["points"] == 4 and e["pace_points_per_hour"] == pytest.approx(2.0, rel=0.1)
    assert e["waits"] and e["waits"][0]["limit"] == "five_hour" and e["waits"][0]["until"] == T0 + 2 * H
    assert e["start_after"]["at"] == T0 + 2 * H and e["finish"] > T0 + 2 * H
    assert "waits until" in e["summary"] and "Starting after" in e["summary"]
    big = advise.estimate(store, T0, points=200, cwd="/work/synth")
    assert big["finish"] is None and "doesn't fit" in big["summary"]
    assert advise.estimate(store, T0, like="typical", cwd="/work/synth")["points"] == pytest.approx(4.0, rel=0.1)


def test_the_mcp_server_lists_and_calls_its_tools(store, monkeypatch):
    _busy(store)
    store.close()
    monkeypatch.setattr(advise.time, "time", lambda: T0)
    monkeypatch.setattr(mcp.time, "time", lambda: T0)
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-code"}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "pacing_brief", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "estimate_job", "arguments": {"points": 3}}},
             {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}]
    out = io.StringIO()
    mcp.serve(io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n"), out)
    got = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
    assert set(got) == {1, 2, 3, 4, 5}                       # no reply to the notification
    assert got[1]["result"]["serverInfo"]["name"] == "savetokens"
    assert {t["name"] for t in got[2]["result"]["tools"]} == {"pacing_brief", "estimate_job", "spend_summary", "suggest_setup"}
    assert "weekly limit" in got[3]["result"]["content"][0]["text"]
    assert "points" in got[4]["result"]["content"][0]["text"]
    assert got[5]["error"]["code"] == -32602


def test_the_agent_hears_only_when_a_limit_is_at_risk_and_only_if_asked(store):
    _busy(store)
    save_config({"agent_context": True})
    store.set_meta("agent_note", {"at": T0, "text": advise.agent_note(store, T0)})
    assert "savetokens:" in store.meta("agent_note")["text"]
    import time as _t
    store.set_meta("agent_note", {"at": _t.time(), "text": "savetokens: weekly limit 90% used"})
    out = hooks.handle("SessionStart", {}, store)
    assert out["hookSpecificOutput"]["additionalContext"].startswith("savetokens:")
    save_config({"agent_context": False})
    assert hooks.handle("SessionStart", {}, store) is None
    calm = Store(store.path.with_name("calm.db"))
    week_of_readings(calm, T0 - 48 * H, 48, per_hour=0.1, resets=T0 + 100 * H)
    maintain.update(calm, T0, use_ephemeris=False)
    assert advise.agent_note(calm, T0) is None


def test_claude_code_on_an_api_key_is_detected_and_asked_for_a_budget(store, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    t = [T0]
    monkeypatch.setattr(capture.time, "time", lambda: t[0])
    for i in range(12):
        t[0] = T0 + i * 120
        capture.record_statusline(store, {"model": {"id": "claude-opus-5-5"}}, None)
    assert capture.detect_billing(store, t[0]) == "api" and capture.billing(store) == "api"
    maintain.notices(store, t[0], {})
    msg = store.conn.execute("SELECT message FROM alerts WHERE name = 'setup'").fetchone()[0]
    assert "savetokens api claude-code --budget" in msg
    capture.record_statusline(store, {"model": {}, "rate_limits": {"seven_day": {"used_percentage": 3,
                                                                                 "resets_at": T0 + 9e5}}}, "a1")
    assert capture.detect_billing(store, t[0]) == "subscription"


def test_a_plan_without_key_signs_is_never_taken_for_an_api_key(store, monkeypatch):
    monkeypatch.setattr(capture, "account", lambda: {"account": "a1"})
    for i in range(12):
        capture.record_statusline(store, {"model": {"id": "x"}}, None)
    assert capture.detect_billing(store) is None


def test_a_join_code_gives_a_new_machine_a_token_once(running):
    users, token, url = running
    code = sync._call({"server_url": url, "server_token": token}, "/v1/pair", {})["code"]
    assert len(code) == 9 and code[4] == "-"
    import urllib.error
    import urllib.request

    def join(c):
        req = urllib.request.Request(url + "/v1/join", data=json.dumps({"code": c}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.load(r)["token"]
    new = join(code.lower())
    assert users.who(new) == "chris"
    with pytest.raises(urllib.error.HTTPError):
        join(code)                                         # used up


def test_install_registers_the_mcp_server_and_uninstall_removes_it(homes, monkeypatch):
    calls = []

    class R:
        returncode = 0
    monkeypatch.setattr(install, "claude_cli", lambda: "/bin/claude")
    (homes / "codex").mkdir()
    (homes / "codex" / "config.toml").write_text('model = "gpt-6-astra"\n')
    assert install.install(yes=True, no_ephemeris=True, cron=False, out=lambda *_: None,
                           run=lambda a, **k: calls.append(a) or R())
    assert any(c[1:5] == ["mcp", "add", "--scope", "user"] and c[-1] == "mcp" for c in calls)
    toml = (homes / "codex" / "config.toml").read_text()
    assert toml.startswith('model = "gpt-6-astra"') and "[mcp_servers.savetokens]" in toml
    install.install(yes=True, no_ephemeris=True, cron=False, out=lambda *_: None, run=lambda a, **k: R())
    assert (homes / "codex" / "config.toml").read_text().count("[mcp_servers.savetokens]") == 1
    install.uninstall(out=lambda *_: None, run=lambda a, **k: calls.append(a) or R())
    assert (homes / "codex" / "config.toml").read_text() == 'model = "gpt-6-astra"\n'
    assert calls[-1][1:3] == ["mcp", "remove"]
