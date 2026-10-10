"""The CLI for agents and scripts (--json everywhere, structured errors, exit codes, check, suggest), the
MCP server's setup tool, and the browser view (local and on the server)."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from savetokens import advise, cli, maintain, mcp, pools, web
from savetokens.store import Store, Usage

from conftest import H, T0, week_of_readings


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr().out.strip()
    return code, (json.loads(out) if out.startswith("{") else out)


def test_failures_say_what_went_wrong_and_the_fix(capsys):
    code, out = run(capsys, "pair", "--json")
    assert code == cli.ERROR and out["ok"] is False and out["fix"] == "savetokens join URL CODE"
    code, out = run(capsys, "connect", "http://x", "--json")
    assert code == cli.USAGE and "--token" in out["error"]
    code, out = run(capsys, "install", "--json")                       # would ask: refused, with the fix
    assert code == cli.USAGE and out["fix"] == "savetokens install --yes --json"
    assert cli.main(["pair"]) == cli.ERROR
    err = capsys.readouterr().err
    assert err.startswith("savetokens: ") and "fix: savetokens join" in err


def test_setup_commands_answer_in_json(capsys):
    code, out = run(capsys, "api", "hermes", "--budget", "40", "--per", "week", "--json")
    assert code == 0 and out["ok"] and out["budget"]["usd"] == 40 and out["budget"]["period"] == "week"
    code, out = run(capsys, "price", "gpt-6-astra", "2", "8", "--json")
    assert code == 0 and out["model"] == "gpt-6-astra" and out["repriced"] == 0
    code, out = run(capsys, "backfill", "--json")
    assert code == 0 and out == {"ok": True, "requests": 0, "readings": 0}
    code, out = run(capsys, "install", "--yes", "--no-ephemeris", "--no-schedule", "--json")
    assert code == 0 and out["installed"] and any("Installed" in line for line in out["log"])
    code, out = run(capsys, "uninstall", "--json")
    assert code == 0 and out["removed"]


def test_an_unexpected_failure_still_answers_in_json(capsys, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(advise, "setup", boom)
    code, out = run(capsys, "suggest", "--json")
    assert code == cli.ERROR and out == {"ok": False, "error": "OSError: disk full", "fix": None, "exit": 1}


def _codex_running_hot(store):
    from test_pools import rollout
    from savetokens import codex
    r = rollout(store.path.parent.parent)
    for i in range(48):
        r.turn(T0 - (48 - i) * H, week=1.5 * (i + 1), resets=T0 + 2 * 86400)
    codex.backfill(store)
    week_of_readings(store, T0 - 48 * H, 48, per_hour=0.2, resets=T0 + 86400)
    maintain.update(store, T0, use_ephemeris=False)


def test_check_gives_its_verdict_as_the_exit_code(capsys, monkeypatch):
    code, out = run(capsys, "check", "--json")
    assert code == 0 and out["verdict"] == "no_data"
    with Store() as s:
        _codex_running_hot(s)
    monkeypatch.setattr(time, "time", lambda: T0)
    code, out = run(capsys, "check", "--json")
    assert code == cli.AT_RISK and out["verdict"] == "at_risk"
    assert any(x["label"] == "Codex weekly limit" and x["stage"] for x in out["limits"])
    code, out = run(capsys, "check", "--tool", "claude-code", "--json")
    assert code == 0 and out["verdict"] == "on_track" and {x["pool"] for x in out["limits"]} == {"claude-code"}
    monkeypatch.setattr(advise, "estimate", lambda *a, **k: {"finish": None, "waits": [], "summary": "too big",
                                                              "points": 80.0})
    code, out = run(capsys, "check", "--tool", "claude-code", "--points", "80", "--json")
    assert code == cli.WONT_FIT and out["job"]["points"] == 80.0
    code, out = run(capsys, "check", "--tool", "hermes", "--json")
    assert code == cli.ERROR and out["fix"].startswith("savetokens api hermes")
    assert cli.main(["check", "--tool", "claude-code", "--points", "80"]) == cli.WONT_FIT
    assert capsys.readouterr().out.startswith("won't fit")


def test_suggest_proposes_commands_and_changes_nothing(capsys, store):
    store.add_usage([Usage("hermes", "s", f"r{i}", time.time() - i * H, "acme/unknown-model", billing="api",
                           input=1000) for i in range(3)])
    store.close()
    code, out = run(capsys, "suggest", "--json")
    cmds = [x["command"] for x in out["suggestions"]]
    assert "savetokens api hermes --budget USD --per month" in cmds
    assert "savetokens price acme/unknown-model INPUT OUTPUT" in cmds
    with Store() as s:
        assert pools.budgets(s) == {}
        text = mcp.call("suggest_setup", {}, store=s)
    assert "savetokens api hermes" in text and "ask for the budget" in text
    assert "suggest_setup" in {t["name"] for t in mcp.TOOLS}


# ── the browser view ──

def _get(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.fixture
def local():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.local_handler("k3y", lambda: {"now": T0, "limits": []}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_the_local_view_needs_its_key_and_a_local_host(local):
    code, headers, body = _get(local + "/")
    assert code == 200 and b"<title>savetokens</title>" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"] and "unsafe-inline" not in body.decode()
    for f, kind in (("/app.js", "javascript"), ("/app.css", "css")):
        code, headers, _ = _get(local + f)
        assert code == 200 and kind in headers["Content-Type"]
    assert _get(local + "/v1/dashboard")[0] == 401
    assert _get(local + "/v1/dashboard", {"Authorization": "Bearer nope"})[0] == 401
    code, _, body = _get(local + "/v1/dashboard", {"Authorization": "Bearer k3y"})
    assert code == 200 and json.loads(body)["now"] == T0
    assert _get(local + "/v1/dashboard", {"Authorization": "Bearer k3y", "Host": "evil.example"})[0] == 403


def test_the_server_serves_the_page_and_a_join_code_signs_in(running):
    users, token, url = running
    code, headers, body = _get(url + "/")
    assert code == 200 and b"Sign in" in body and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert _get(url + "/v1/dashboard")[0] == 401
    pair = users.pair("chris")
    code, _, body = _get(url + "/v1/join", {"Content-Type": "application/json"}, json.dumps({"code": pair}).encode())
    browser = json.loads(body)["token"]
    code, _, body = _get(url + "/v1/dashboard", {"Authorization": f"Bearer {browser}"})
    assert code == 200 and "headline" in json.loads(body)


def test_every_command_but_the_long_running_ones_takes_json(capsys):
    for c in ("install", "uninstall", "status", "check", "dashboard", "backfill", "maintain", "ephemeris", "api",
              "price", "advise", "estimate", "suggest", "join", "pair", "connect"):
        with pytest.raises(SystemExit) as e:
            cli.main([c, "--help"])
        out = capsys.readouterr().out
        assert e.value.code == 0 and "--json" in out and "exit codes:" in out, c
