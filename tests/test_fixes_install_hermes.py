import json

import pytest

from savetokens import fixes, install
from savetokens.adapters import hermes
from savetokens.store import load_config, save_config

from conftest import T0


def settings(homes):
    return homes / "claude" / "settings.json"


def write_settings(homes, data):
    p = settings(homes)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))


@pytest.mark.parametrize("fix_id", [f for f in fixes.FIXES if fixes.FIXES[f].kind == "env"])
@pytest.mark.parametrize("before", [{}, {"env": {"OTHER": "1"}}, {"env": {"BASH_MAX_OUTPUT_LENGTH": "9",
                                                                          "CLAUDE_CODE_SUBAGENT_MODEL": "x",
                                                                          "MAX_MCP_OUTPUT_TOKENS": "7"}}])
def test_env_fixes_apply_and_revert_exactly(store, homes, fix_id, before):
    write_settings(homes, {"model": "opus", **before})
    original = json.loads(settings(homes).read_text())
    fixes.apply(store, fix_id)
    applied = json.loads(settings(homes).read_text())
    assert applied["env"][fixes.FIXES[fix_id].key] == fixes.FIXES[fix_id].value
    with pytest.raises(ValueError):
        fixes.apply(store, fix_id)
    fixes.revert(store, fix_id)
    assert json.loads(settings(homes).read_text()) == original


def test_claude_md_fix_reverts_cleanly(store, homes):
    md = homes / "claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("# mine\nkeep me\n")
    fixes.apply(store, "output-hygiene")
    assert fixes.BLOCK_START in md.read_text()
    fixes.revert(store, "output-hygiene")
    assert md.read_text() == "# mine\nkeep me\n"
    fixes.apply(store, "output-hygiene")
    md.unlink()
    fixes.revert(store, "output-hygiene")   # file removed by the user: revert still succeeds


def test_fix_impact_interval(store):
    from savetokens.store import UsageEvent
    fixes_row = ("subagent-model", "x", T0, None, "{}")
    store.conn.execute("INSERT INTO fixes VALUES (?,?,?,?,?)", fixes_row)
    events = []
    for s in range(6):
        for i in range(10):
            events.append(UsageEvent("claude-code", f"b{s}", f"b{s}{i}", T0 - 86400 + s * 600 + i, cost_usd=1.0))
            events.append(UsageEvent("claude-code", f"a{s}", f"a{s}{i}", T0 + 600 + s * 600 + i, cost_usd=0.5))
    store.add_usage(events)
    r = fixes.impact(store, "subagent-model")
    assert r["ratio"] == 0.5 and r["lo"] <= 0.5 <= r["hi"]


def test_install_claude_code_keeps_user_settings_and_statusline(homes, transcript):
    transcript().turn(T0)
    write_settings(homes, {"model": "opus", "statusLine": {"type": "command", "command": "my-line.sh"},
                           "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command",
                                                                                    "command": "fmt"}]}]}})
    asked = []
    assert install.install_claude_code(out=lambda *_: None, ask=lambda q: asked.append(q) or "y")
    data = json.loads(settings(homes).read_text())
    assert data["model"] == "opus"
    post = data["hooks"]["PostToolUse"]
    assert post[0]["hooks"][0]["command"] == "fmt" and "savetokens hook" in post[1]["hooks"][0]["command"] \
        or "-m savetokens hook" in post[1]["hooks"][0]["command"]
    assert "PreToolUse" not in data["hooks"]
    assert data["statusLine"]["command"].endswith("statusline")
    assert load_config()["statusline_wrapped"] == "my-line.sh"
    # idempotent: a second install doesn't duplicate hooks or wrap itself
    install.install_claude_code(yes=True, out=lambda *_: None)
    data = json.loads(settings(homes).read_text())
    assert len(data["hooks"]["PostToolUse"]) == 2
    assert load_config()["statusline_wrapped"] == "my-line.sh"
    install.uninstall_claude_code(out=lambda *_: None)
    data = json.loads(settings(homes).read_text())
    assert data == {"model": "opus", "statusLine": {"type": "command", "command": "my-line.sh"},
                    "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "fmt"}]}]}}


def test_install_declined_changes_nothing(homes):
    write_settings(homes, {"model": "opus"})
    assert not install.install_claude_code(out=lambda *_: None, ask=lambda q: "n")
    assert json.loads(settings(homes).read_text()) == {"model": "opus"}
    assert "block" not in json.loads((homes / "st" / "config.json").read_text()) \
        if (homes / "st" / "config.json").exists() else True


def test_hermes_backfill_no_double_count(store, hermes_home):
    hermes.backfill(store, hermes_home)
    hermes.backfill(store, hermes_home)          # idempotent
    rows = store.usage(harness="hermes")
    by_session = {}
    for e in rows:
        by_session.setdefault(e.session_id, []).append(e)
    assert len(by_session["tracked"]) == 2       # per-call rows, not the aggregate
    assert len(by_session["old"]) == 2           # aggregates for the session without per-call rows
    qwen = next(e for e in by_session["old"] if "qwen" in e.model)
    assert qwen.cost_usd == 0.30 and qwen.cost_source == "hermes"
    sonnet = by_session["tracked"][0]
    assert sonnet.cost_source == "price_table"


def test_hermes_plugin_warns_inside_the_loop_and_never_raises(hermes_home, monkeypatch):
    from savetokens import hermes_plugin as p
    registered = {}

    class Ctx:
        def register_hook(self, name, fn):
            registered[name] = fn

    p.register(Ctx())
    assert "pre_tool_call" not in registered           # fails closed in Hermes: only with --block
    for i in range(3):
        assert registered["transform_tool_result"](tool_name="terminal", args={"command": "ls"}, result="{}",
                                                   session_id="h1", tool_call_id=f"c{i}") is None
    out = registered["transform_tool_result"](tool_name="terminal", args={"command": "ls"}, result="{}",
                                              session_id="h1", tool_call_id="c3")
    assert out.startswith("{}") and "4 times in a row" in out
    registered["post_api_request"](session_id="h1", model="anthropic/claude-sonnet-5-5", ended_at=T0,
                                   usage={"input_tokens": 10, "output_tokens": 5}, api_request_id="x1")
    # a broken store must not raise into Hermes
    monkeypatch.setattr(p, "_store", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert p.warn_in_result(tool_name="terminal", args={}, result="{}") is None
    assert p.guard_pre_tool(tool_name="terminal", args={}) is None


def test_hermes_block_registers_pre_tool_call(hermes_home):
    from savetokens import hermes_plugin as p
    cfg = load_config()
    cfg["block"] = True
    save_config(cfg)
    registered = {}

    class Ctx:
        def register_hook(self, name, fn):
            registered[name] = fn

    p.register(Ctx())
    assert "pre_tool_call" in registered
    for i in range(4):
        registered["transform_tool_result"](tool_name="terminal", args={"command": "make"}, result="{}",
                                            session_id="h2", tool_call_id=f"m{i}")
    out = registered["pre_tool_call"](tool_name="terminal", args={"command": "make"}, session_id="h2")
    assert out["action"] == "block" and out["message"]


def test_install_hermes_copies_plugin(hermes_home):
    calls = []
    assert install.install_hermes(yes=True, out=lambda *_: None, run=lambda *a, **k: calls.append(a))
    dest = hermes_home / "plugins" / "savetokens"
    assert (dest / "plugin.yaml").exists() and (dest / "package_path.txt").read_text().strip()
    install.uninstall_hermes(out=lambda *_: None, run=lambda *a, **k: None)
    assert not dest.exists()


def test_capabilities_json():
    caps = json.loads(install.capabilities_json())
    assert caps["network"].startswith("Ephemeris API") and "claude-code" in caps["harnesses"]
