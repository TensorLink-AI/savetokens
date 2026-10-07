"""Codex capture and forecasting, and levers: applied at risk, undone exactly, never without consent."""
from __future__ import annotations

import json

import pytest

from conftest import T0
from savetokens import guard, levers, steer
from savetokens.adapters import codex
from savetokens.store import ToolEvent, load_config, save_config


def rollout(path, sid, events):
    """events: (ts, pct, resets, total_tokens) for one weekly window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"timestamp": "2026-10-01T10:00:00Z", "type": "session_meta", "payload": {"id": sid}},
             {"timestamp": "2026-10-01T10:00:00Z", "type": "turn_context", "payload": {"model": "gpt-6-astra",
                                                                                       "effort": "medium"}}]
    from datetime import datetime, timezone
    for ts, pct, resets, total in events:
        lines.append({"timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z"),
                      "type": "event_msg", "payload": {
                          "type": "token_count",
                          "info": {"total_token_usage": {"total_tokens": total},
                                   "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 800,
                                                        "output_tokens": 50}},
                          "rate_limits": {"limit_id": "codex", "plan_type": "pro",
                                          "primary": {"used_percent": pct, "window_minutes": 10080,
                                                      "resets_at": resets}}}})
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


def test_codex_backfill_records_usage_and_weekly_readings(store, homes):
    root = homes / "codex" / "sessions" / "2026" / "10" / "01"
    rollout(root / "rollout-a.jsonl", "a", [(T0 + i * 600, 10 + i, T0 + 86400, 100 * (i + 1)) for i in range(5)])
    assert codex.backfill(store) == 5
    assert codex.backfill(store) == 0                       # incremental
    u = store.usage(harness="codex")[0]
    assert (u.model, u.input, u.cache_read, u.output) == ("gpt-6-astra", 200, 800, 50)
    assert codex.latest(store)["seven_day"]["pct"] == 14
    # Claude Code's limit queries never see Codex readings
    from savetokens import windows
    assert windows.latest_reading(store, "seven_day", T0) is None


def test_codex_increments_follow_each_window(store, homes):
    root = homes / "codex" / "sessions"
    old, new = T0 + 3 * 86400, T0 + 10 * 86400
    rollout(root / "rollout-a.jsonl", "a", [(T0 + h * 3600, 20 + 2 * h, old, h + 1) for h in range(5)])
    # a long-running session still reports the old window's snapshot while a new window starts
    rollout(root / "rollout-b.jsonl", "b", [(T0 + 5 * 3600 + 60, 1, new, 1), (T0 + 6 * 3600 + 60, 4, new, 2)])
    rollout(root / "rollout-c.jsonl", "c", [(T0 + 6 * 3600 + 120, 28, old, 1)])
    codex.backfill(store)
    inc = dict(codex.hourly_increments(store, "seven_day", T0 + 8 * 3600))
    assert sum(inc.values()) == 20 + 8 + 1 + 3                 # first reading, +2 x4, new window 1, +3; stale 28 adds 0


def test_codex_pressure_gives_the_chance_of_a_hit(store, homes):
    from array import array
    from savetokens import windows
    store.set_meta("codex_last_reading", {"seven_day": {"ts": T0, "pct": 80.0, "resets": T0 + 5 * 3600,
                                                         "plan": "pro"}})
    data = array("d")
    for p in range(10):
        data.extend([10.0 if p < 3 else 0.0] * 24)             # 3 of 10 paths add 10 points an hour
    windows.save_paths(store, "baseline", "codex_week", T0, T0, 24, data)
    w = codex.pressure(store, T0)[0]
    assert w["name"] == "Codex weekly limit" and w["p_hit"] == 0.3 and w["used"] == 80.0


@pytest.fixture
def consent():
    cfg = load_config()
    cfg["levers_consent"] = True
    cfg["levers"] = list(levers.DEFAULT_LEVERS) + ["compaction"]      # compaction is opt-in
    save_config(cfg)
    return cfg


@pytest.fixture
def risk(monkeypatch):
    state = {"claude-code": (0.0, T0 + 3600, "5-hour limit"), "codex": (0.0, T0 + 3600, "Codex weekly limit")}
    monkeypatch.setattr(levers, "_risk", lambda store, h, now: state[h])
    return state


def settings(homes):
    p = homes / "claude" / "settings.json"
    return json.loads(p.read_text()) if p.exists() else {}


def test_no_levers_without_consent(store, homes, risk):
    risk["claude-code"] = (0.9, T0 + 3600, "5-hour limit")
    assert levers.update(store, T0) == {}
    assert settings(homes) == {}


def test_levers_apply_at_risk_and_restore_exactly(store, homes, risk, consent):
    (homes / "claude").mkdir(parents=True, exist_ok=True)
    (homes / "claude" / "settings.json").write_text(json.dumps({"model": "opus", "effortLevel": "high",
                                                                "env": {"FOO": "1"}}))
    codex_cfg = homes / "codex" / "config.toml"
    codex_cfg.parent.mkdir(parents=True, exist_ok=True)
    codex_cfg.write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "medium"\n\n[projects."/x"]\ntrust = 1\n')
    risk["claude-code"] = (0.2, T0 + 3600, "5-hour limit")
    assert levers.update(store, T0) == {}                       # below 30%: nothing
    risk["claude-code"] = (0.45, T0 + 3600, "5-hour limit")
    risk["codex"] = (0.5, T0 + 7200, "Codex weekly limit")
    out = levers.update(store, T0)
    assert set(out) == {"claude-code", "codex"}
    s = settings(homes)
    assert s["env"] == {"FOO": "1", "CLAUDE_CODE_SUBAGENT_MODEL": "claude-sonnet-5-5"}
    assert s["effortLevel"] == "low" and s["autoCompactWindow"] == 300_000 and s["model"] == "opus"   # never main
    text = codex_cfg.read_text()
    assert 'model_reasoning_effort = "low"' in text and "model_auto_compact_token_limit = 150000" in text
    assert text.index("model_auto_compact_token_limit") < text.index("[projects")    # top level, not in a table
    # hysteresis: 20% is below the apply level but not low enough to undo
    risk["claude-code"] = (0.2, T0 + 3600, "5-hour limit")
    assert "claude-code" not in levers.update(store, T0 + 40 * 60)
    assert levers.active(store, "claude-code")
    # the windows reset: everything back exactly
    out = levers.update(store, T0 + 3 * 3600)
    assert "reverted" in out["claude-code"] and "reverted" in out["codex"]
    assert settings(homes) == {"model": "opus", "effortLevel": "high", "env": {"FOO": "1"}}
    assert codex_cfg.read_text() == 'model = "gpt-6-astra"\nmodel_reasoning_effort = "medium"\n\n[projects."/x"]\ntrust = 1\n'


def test_a_value_the_user_changed_is_left_alone(store, homes, risk, consent):
    risk["claude-code"] = (0.5, T0 + 3600, "5-hour limit")
    levers.update(store, T0, harnesses=("claude-code",))
    s = settings(homes)
    s["effortLevel"] = "xhigh"                                   # the user picks something else meanwhile
    (homes / "claude" / "settings.json").write_text(json.dumps(s))
    levers.revert(store, "claude-code", now=T0 + 60)
    s = settings(homes)
    assert s["effortLevel"] == "xhigh" and "autoCompactWindow" not in s and "env" not in s


def test_failing_tests_switch_back_and_cool_down(store, homes, risk, consent):
    risk["claude-code"] = (0.6, T0 + 3600, "5-hour limit")
    levers.update(store, T0, harnesses=("claude-code",))
    assert levers.active(store, "claude-code")
    cfg = load_config()
    for i in range(cfg["failing_tests"]):
        store.add_tools([ToolEvent("claude-code", "s1", T0 + 10 + i, "Bash", "test", tool_use_id=f"t{i}",
                                   args_hash="h", ok=False)])
    alerts = guard.evaluate(store, "claude-code", "s1", cfg=cfg, now=T0 + 20)
    assert any(a.rule == "levers" and "switched back" in a.message for a in alerts)
    assert not levers.active(store) and settings(homes) == {}
    assert levers.update(store, T0 + 60, harnesses=("claude-code",)) == {}        # cooling down
    assert levers.update(store, T0 + 31 * 60, harnesses=("claude-code",))["claude-code"]["applied"]


def test_quality_mode_never_economises(store, homes, risk, consent):
    risk["claude-code"] = (0.9, T0 + 3600, "5-hour limit")
    cfg = load_config()
    cfg["mode"] = "quality"
    save_config(cfg)
    assert levers.update(store, T0, harnesses=("claude-code",)) == {}
    assert settings(homes) == {}


def test_economy_text_offers_cheaper_subagents_only_with_consent(consent):
    assert steer.SUBAGENT_HINT in steer.economy("claude-code")
    assert steer.SUBAGENT_HINT not in steer.economy("hermes")
    cfg = load_config()
    cfg["levers"] = ["effort"]
    save_config(cfg)
    assert steer.SUBAGENT_HINT not in steer.economy("claude-code")


def test_compaction_is_opt_in_by_default():
    assert "compaction" not in levers.DEFAULT_LEVERS and "main" not in levers.DEFAULT_LEVERS


def test_harness_scoped_permission(store, homes, risk):
    cfg = load_config()
    cfg["levers_consent"] = True
    cfg["levers"] = ["codex:effort"]
    save_config(cfg)
    risk["claude-code"] = (0.9, T0 + 3600, "5-hour limit")
    risk["codex"] = (0.9, T0 + 3600, "Codex weekly limit")
    out = levers.update(store, T0, harnesses=("claude-code", "codex"))
    assert set(out) == {"codex"} and settings(homes) == {}


def test_hermes_install_does_not_consent_for_claude(homes, hermes_home):
    from savetokens import install
    install.install_hermes(yes=True, restart=False, out=lambda *_: None, no_ephemeris=True)
    cfg = load_config()
    assert levers.consented("hermes", cfg) and not levers.consented("claude-code", cfg)
    install.install_claude_code(yes=True, out=lambda *_: None, no_ephemeris=True)
    cfg = load_config()
    assert all(levers.consented(h, cfg) for h in ("hermes", "claude-code", "codex"))
