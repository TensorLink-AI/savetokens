"""OpenTelemetry in: Claude Code's events and the GenAI conventions, counted once alongside readers."""
from __future__ import annotations

import gzip
import json
import urllib.request

from savetokens import otel

from conftest import T0


def _kv(d):
    out = []
    for k, v in d.items():
        val = {"intValue": str(v)} if isinstance(v, int) else {"doubleValue": v} if isinstance(v, float) \
            else {"stringValue": v}
        out.append({"key": k, "value": val})
    return out


def _claude_logs(n=2, session="sess-1", agent=None):
    recs = []
    for i in range(n):
        a = {"event.name": "api_request", "event.timestamp": "2026-10-12T00:00:0%dZ" % i, "model": "claude-opus-5-5",
             "input_tokens": 10, "output_tokens": 500, "cache_read_tokens": 40_000, "cache_creation_tokens": 1000,
             "request_id": f"req_{session}_{i}", "session.id": session, "cost_usd": 0.5}
        if agent:
            a["agent.name"] = agent
        recs.append({"timeUnixNano": str(int(T0 * 1e9)), "eventName": "claude_code.api_request", "attributes": _kv(a)})
    return {"resourceLogs": [{"resource": {"attributes": _kv({"service.name": "claude-code", "host.name": "ci-7"})},
                              "scopeLogs": [{"logRecords": recs}]}]}


def _genai_spans():
    a = {"gen_ai.provider.name": "openai", "gen_ai.operation.name": "chat", "gen_ai.request.model": "gpt-6-sol",
         "gen_ai.response.id": "resp_1", "gen_ai.conversation.id": "conv-9", "gen_ai.usage.input_tokens": 12_000,
         "gen_ai.usage.cache_read.input_tokens": 10_000, "gen_ai.usage.output_tokens": 300,
         "gen_ai.agent.name": "researcher"}
    return {"resourceSpans": [{"resource": {"attributes": _kv({"service.name": "my-agent"})},
                               "scopeSpans": [{"spans": [{"traceId": "t1", "spanId": "s1", "attributes": _kv(a),
                                                          "endTimeUnixNano": str(int(T0 * 1e9))}]}]}]}


def test_claude_code_events_become_usage():
    got = otel.parse(_claude_logs(agent="explorer"))
    e = got[0]
    assert (e.harness, e.session_id, e.request_id, e.model) == ("claude-code", "sess-1", "otel:req_sess-1_0",
                                                               "claude-opus-5-5")
    assert (e.input, e.output, e.cache_read, e.cache_write_5m) == (10, 500, 40_000, 1000)
    assert e.subagent and e.thread == "sess-1/explorer" and e.machine == "otel:ci-7" and e.cost_usd > 0


def test_genai_spans_become_usage_with_cached_input_split_out():
    e = otel.parse(_genai_spans())[0]
    assert (e.harness, e.provider, e.model, e.session_id) == ("my-agent", "openai", "gpt-6-sol", "conv-9")
    assert (e.input, e.cache_read, e.output) == (2_000, 10_000, 300)          # input_tokens includes cached
    assert e.thread == "conv-9/researcher" and e.subagent


def _post(url, token, body, gz=False, ctype="application/json"):
    data = json.dumps(body).encode()
    h = {"Authorization": f"Bearer {token}", "Content-Type": ctype}
    if gz:
        data, h["Content-Encoding"] = gzip.compress(data), "gzip"
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_the_hub_takes_otlp_json_and_a_reader_replaces_telemetry_for_its_sessions(running):
    users, token, url = running
    assert _post(url + "/v1/logs", token, _claude_logs()) == 200
    assert _post(url + "/v1/traces", token, _genai_spans(), gz=True) == 200
    assert _post(url + "/v1/logs", "nope", _claude_logs()) == 401
    assert _post(url + "/v1/logs", token, {}, ctype="application/x-protobuf") == 415
    with users.store("chris") as s:
        assert s.conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 3
    from savetokens import sync
    from savetokens.store import Store, Usage
    import tempfile, pathlib
    m = Store(pathlib.Path(tempfile.mkdtemp()) / "m.db")       # a machine that reads Claude Code itself
    m.add_usage([Usage("claude-code", "sess-1", "msg_1:req_sess-1_0", T0, "claude-opus-5-5", cost_usd=0.5,
                       thread="sess-1")])
    m.set_meta("project_key_from", url)
    sync.push(m, {"server_url": url, "server_token": token})
    with users.store("chris") as s:
        rows = s.conn.execute("SELECT machine, session_id FROM usage ORDER BY machine").fetchall()
        assert [(r[0].startswith("otel:"), r[1]) for r in rows] == [(False, "sess-1"), (True, "conv-9")]
    assert _post(url + "/v1/logs", token, _claude_logs()) == 200     # telemetry for a session read: not added
    with users.store("chris") as s:
        assert s.conn.execute("SELECT COUNT(*) FROM usage WHERE session_id = 'sess-1'").fetchone()[0] == 1
    m.close()


def test_otel_command_needs_a_hub(capsys):
    from savetokens import cli
    assert cli.main(["otel", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["fix"] == "savetokens server --setup"
