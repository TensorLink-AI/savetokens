"""Setup: plans detected from what Claude Code and Codex keep locally, one guided pass for the rest
(the Ephemeris key checked as it's pasted), and the same checklist for agents and the browser view."""
from __future__ import annotations

import base64
import io
import json
import sys

from savetokens import cli, dashboard, ephemeris, pools, setup
from savetokens.store import Store, load_config


def _claude(homes, org="claude_max", tier="default_claude_max_20x", creds=None):
    home = homes / "claude"
    home.mkdir(exist_ok=True)
    if org:
        (home / ".claude.json").write_text(json.dumps({"oauthAccount": {
            "accountUuid": "u1", "organizationType": org, "organizationRateLimitTier": tier,
            "emailAddress": "someone@example.com"}}))
    if creds:
        (home / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret", **creds}}))


def _codex(homes, plan="pro", api=False):
    home = homes / "codex"
    home.mkdir(exist_ok=True)
    claims = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {"chatgpt_plan_type": plan}})
                                      .encode()).decode().rstrip("=")
    (home / "auth.json").write_text(json.dumps({"OPENAI_API_KEY": "sk-x" if api else None,
                                                "auth_mode": "apikey" if api else "chatgpt",
                                                "tokens": {"id_token": f"h.{claims}.sig"}}))


def test_plans_are_read_from_what_the_tools_keep(homes):
    assert setup.detected() == {}
    _claude(homes)
    _codex(homes)
    got = setup.detected()
    assert got["claude-code"] == {"billing": "subscription", "plan": "Claude Max 20x", "usd": 200}
    assert got["codex"] == {"billing": "subscription", "plan": "ChatGPT Pro", "usd": 200}
    _claude(homes, org=None, creds={"subscriptionType": "pro", "rateLimitTier": "default_claude_ai"})
    (homes / "claude" / ".claude.json").unlink()
    assert setup.claude_plan()["plan"] == "Claude Pro" and setup.claude_plan()["usd"] == 20
    _codex(homes, api=True)
    assert setup.codex_plan()["billing"] == "api"
    _codex(homes, plan="team")
    assert setup.codex_plan() == {"billing": "subscription", "plan": "ChatGPT Team", "usd": None}   # ask


def test_setup_yes_takes_the_detected_prices_and_lists_the_rest(homes, capsys):
    _claude(homes)
    _codex(homes)
    (homes / "hermes").mkdir()
    assert cli.main(["setup", "--json"]) == 0                      # without --yes: changes nothing
    out = json.loads(capsys.readouterr().out)
    assert out["changed"] == [] and {"plan:anthropic", "plan:openai", "budget:hermes", "forecaster"} <= set(out["left"])
    cmds = {s["id"]: s["command"] for s in out["steps"]}
    assert cmds["plan:anthropic"] == "savetokens plan anthropic --usd 200 --per month"
    assert "someone@example.com" not in json.dumps(out)
    assert cli.main(["setup", "--yes", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["changed"] == ["plan anthropic", "plan openai"] and "plan:anthropic" not in out["left"]
    with Store() as s:
        assert {p: v["usd"] for p, v in pools.plans(s).items()} == {"anthropic": 200, "openai": 200}
        assert pools.budgets(s) == {}                               # a budget is never guessed


def test_the_guided_pass_asks_once_each_and_checks_the_key(homes, monkeypatch):
    _claude(homes)
    _codex(homes, plan="team")
    (homes / "hermes").mkdir()
    monkeypatch.setattr(ephemeris, "balance", lambda k: 1500.0 if k == "good" else (_ for _ in ()).throw(
        RuntimeError("401 Unauthorized")))
    answers = iter(["", "30", "50"])     # Claude Max: the detected $200; ChatGPT Team: $30; Hermes: a $50 budget
    keys = iter(["bad", "good"])
    log = []
    with Store() as s:
        got = setup.run(s, load_config(), ask=lambda q: (log.append(q), next(answers))[1],
                        secret=lambda q: next(keys), out=log.append)
        assert {p: v["usd"] for p, v in pools.plans(s).items()} == {"anthropic": 200, "openai": 30}
        assert pools.budgets(s)["hermes"]["usd"] == 50
    text = "\n".join(map(str, log))
    assert "Claude Max 20x: what does it cost a month? [$200" in text and setup.SIGNUP in text
    assert "didn't accept" in text and "1,500 credits" in text and "good" not in text
    assert got["changed"] == ["plan anthropic", "plan openai", "budget hermes", "ephemeris key"]
    assert ephemeris.api_key() == "good" and not setup.todo([x for x in got["steps"] if not x["optional"]])


def test_a_key_can_come_from_stdin(homes, monkeypatch, capsys):
    monkeypatch.setattr(ephemeris, "balance", lambda k: 10.0)
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped-key\n"))
    assert cli.main(["setup", "--key", "-", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == ["ephemeris key"]
    assert ephemeris.api_key() == "piped-key"
    monkeypatch.setattr(ephemeris, "balance", lambda k: (_ for _ in ()).throw(RuntimeError("401")))
    assert cli.main(["setup", "--key", "nope", "--json"]) == cli.ERROR
    assert setup.SIGNUP in json.loads(capsys.readouterr().out.splitlines()[-1])["fix"]


def test_the_browser_view_gets_the_checklist(homes, store):
    _claude(homes)
    steps = dashboard.snapshot(store)["setup"]
    assert any(x["id"] == "plan:anthropic" and not x["done"] for x in steps)
    server = dashboard.snapshot(store, on_server=True)["setup"]       # a server detects nothing of its own
    assert not any(x["id"].startswith(("tool:", "server")) for x in server)


def test_the_server_names_the_plans_its_machines_found(homes, running, tmp_path):
    from savetokens import sync
    from savetokens.store import Usage
    users, token, url = running
    _claude(homes)
    a = Store(tmp_path / "a.db")
    a.add_usage([Usage("claude-code", "s", "r", 1_790_000_000.0, "opus", input=10)])
    sync.push(a, {"server_url": url, "server_token": token})
    with users.store("chris") as s:
        steps = {x["id"]: x for x in setup.steps(s, 1_790_000_000.0, on_server=True)}
    assert steps["plan:anthropic"]["title"] == "Claude Max 20x: price not set"
    assert steps["plan:anthropic"]["command"] == "savetokens plan anthropic --usd 200 --per month"


def test_signing_in_from_the_terminal(monkeypatch):
    replies = iter([(200, {"device_code": "dc", "user_code": "WDJB-MJHT", "verification_uri": "https://e/device",
                           "verification_uri_complete": "https://e/device?code=WDJB-MJHT", "interval": 1,
                           "expires_in": 600}),
                    (400, {"error": "authorization_pending"}), (400, {"error": "slow_down"}), (400, {"error": "pending"}),
                    (200, {"key": "new-key"})])
    sent, opened, waits, log = [], [], [], []
    monkeypatch.setattr(ephemeris, "_public", lambda path, body: (sent.append((path, body)), next(replies))[1])
    clock = iter(range(0, 10_000, 10))
    key = ephemeris.device_login(out=log.append, open_url=opened.append, sleep=waits.append, clock=lambda: next(clock))
    assert key == "new-key" and opened == ["https://e/device?code=WDJB-MJHT"] and "WDJB-MJHT" in log[0]
    assert waits == [1.0, 1.0, 6.0, 6.0] and sent[0][0] == "device/code" and sent[1] == ("device/token", {"device_code": "dc"})
    monkeypatch.setattr(ephemeris, "_public", lambda path, body: (404, {"error": "not found"}))
    try:
        ephemeris.device_login(out=log.append)
        raise AssertionError("expected NoDeviceLogin")
    except ephemeris.NoDeviceLogin:
        pass


def test_setup_signs_in_with_the_browser_and_falls_back_to_pasting(homes, monkeypatch):
    monkeypatch.setattr(ephemeris, "balance", lambda k: 2000.0)
    monkeypatch.setattr(ephemeris, "device_login", lambda **k: "from-browser")
    log = []
    with Store() as s:
        got = setup.run(s, load_config(), ask=lambda q: "", secret=lambda q: "", out=log.append, open_url=lambda u: None)
    assert got["changed"] == ["ephemeris key"] and ephemeris.api_key() == "from-browser"

    def unsupported(**k):
        raise ephemeris.NoDeviceLogin("404")
    monkeypatch.setattr(ephemeris, "device_login", unsupported)
    (homes / "st" / "ephemeris.env").unlink()
    cfg = load_config()
    cfg.pop("ephemeris_env_file")
    keys = iter(["", "pasted"])          # Enter (sign in: not offered), then paste
    with Store() as s:
        setup.run(s, cfg, ask=lambda q: "", secret=lambda q: next(keys), out=log.append, open_url=lambda u: None)
    assert ephemeris.api_key() == "pasted" and any("isn't available yet" in str(x) for x in log)


def test_running_out_of_credits_is_said_not_hidden(homes, store, capsys, monkeypatch):
    monkeypatch.setenv("EPHEMERIS_API_KEY", "k")
    store.set_meta("ephemeris_last", {"made_at": 100.0})
    store.set_meta("ephemeris_error", {"at": 200.0, "error": "HTTP 402: Insufficient credits"})
    step = {x["id"]: x for x in setup.steps(store, found={})}["forecaster"]
    assert not step["done"] and step["title"] == "Ephemeris: out of credits" and ephemeris.TOPUP in step["detail"]
    store.close()
    cli.main(["status"])
    assert "Ephemeris credits ran out" in capsys.readouterr().out
    with Store() as s:
        s.set_meta("ephemeris_last", {"made_at": 300.0})       # a forecast worked again
        assert ephemeris.problem(s) is None


def test_toto2_is_the_default_model_and_the_ensemble_an_option(monkeypatch, capsys):
    import gnomon
    from savetokens import engine
    made = []
    monkeypatch.setattr(gnomon, "EphemerisProvider", lambda url, **kw: made.append(kw) or object())
    monkeypatch.setattr(gnomon.InferenceEngine, "register", lambda self, *a, **k: None)
    engine.Engine(key="k")
    assert made[-1]["mode"] == "explicit" and made[-1]["model"] == "toto2-313m"
    assert cli.main(["ephemeris", "--model", "ensemble", "--off", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["model"] == "ensemble"
    engine.Engine(key="k")
    assert made[-1] == {"mode": "ensemble", "token_env": "EPHEMERIS_API_KEY", "timeout": 120}


def test_a_first_run_shows_what_it_found(homes, capsys):
    _claude(homes)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "Claude Code (Claude Max 20x)" in out and "savetokens install" in out
    from test_pools import rollout
    from conftest import H, T0
    r = rollout(homes)
    for i in range(3):
        r.turn(T0 - (3 - i) * H, week=1.0 * (i + 1), resets=T0 + 86400)
    import time as t
    real = t.time
    try:
        t.time = lambda: T0
        cli.main(["status"])
    finally:
        t.time = real
    out = capsys.readouterr().out
    assert "Found in your history" in out or "Codex weekly limit" in out
