"""The quality knob: modes, briefing, nudges, lessons and their delivery to each harness."""
from __future__ import annotations

import json

import pytest

from conftest import T0
from savetokens import guard, install, steer
from savetokens.adapters import claude_code, hermes
from savetokens.store import ToolEvent, UsageEvent, load_config, save_config


def window(name="five_hour", used=40.0, p50=70.0, resets=T0 + 3600, p_hit=None):
    if p_hit is None:   # a rough stand-in: forecasts near or over the limit carry risk
        p_hit = 0.0 if p50 < 80 else min(1.0, round((p50 - 70) / 40, 2))
    return {"window": name, "name": steer.WINDOW_NAMES.get(name, name), "unit": "%", "used": used,
            "forecast": [p50 - 10, p50, p50 + 10], "p_hit": p_hit, "resets": resets, "session": None}


@pytest.fixture
def pressure(monkeypatch):
    """Set the limit forecasts the steering code sees."""
    state = {"windows": []}
    monkeypatch.setattr(steer, "pressure", lambda *a, **k: list(state["windows"]))
    return state


def set_mode(mode):
    cfg = load_config()
    cfg["mode"] = mode
    save_config(cfg)


def test_mode_comes_from_config_and_env_overrides(monkeypatch):
    assert steer.configured_mode() == "auto"          # the default: steer only when a limit is at risk
    set_mode("lean")
    assert steer.configured_mode() == "lean"
    monkeypatch.setenv("SAVETOKENS_MODE", "quality")
    assert steer.configured_mode() == "quality"
    monkeypatch.setenv("SAVETOKENS_MODE", "nonsense")
    assert steer.configured_mode() == "balanced"      # unknown values fall back to the middle setting


@pytest.mark.parametrize("p50s,risk,expected", [([], 0, "balanced"), ([90, 30], 0.35, "lean"),
                                                ([90, 30], 0.25, "balanced"), ([40, 20], 0, "quality"),
                                                ([70, 20], 0, "balanced")])
def test_auto_follows_the_chance_of_a_hit(store, p50s, risk, expected):
    set_mode("auto")
    ws = [window("five_hour", p50=p, p_hit=risk) if i == 0 else window("week", p50=p, p_hit=0)
          for i, p in enumerate(p50s)]
    mode, why = steer.effective_mode(store, T0, windows_=ws)
    assert mode == expected and why.startswith("auto")
    assert store.meta("steer_mode")["mode"] == expected   # cached for the guard


def test_guard_thresholds_follow_the_mode():
    base = load_config()
    q, lean = steer.adjust_guard(base, "quality"), steer.adjust_guard(base, "lean")
    assert q["reread_limit"] > base["reread_limit"] > lean["reread_limit"] >= 2
    assert q["loop_repeats"] > base["loop_repeats"] > lean["loop_repeats"] >= 2
    assert steer.adjust_guard(base, "balanced") == base


def test_lean_guard_fires_earlier(store):
    set_mode("lean")
    cfg = load_config()
    for i in range(3):   # balanced needs 4 reads; lean needs 3
        store.add_tools([ToolEvent("claude-code", "s", T0 + i, "Read", "read", tool_use_id=f"t{i}",
                                   args_hash=f"h{i}", target="/w/a.py")])
    assert [a.rule for a in guard.evaluate(store, "claude-code", "s", cfg=cfg, now=T0 + 5)] == ["reread"]


def test_quality_mode_adds_nothing(store, pressure):
    set_mode("quality")
    pressure["windows"] = [window(p50=150)]
    assert steer.briefing(store, "/w/proj", now=T0) is None
    assert steer.nudge(store, "s1", now=T0) is None


def test_balanced_is_silent_without_risk_or_lessons(store, pressure):
    pressure["windows"] = [window(p50=40)]
    assert steer.briefing(store, "/w/proj", now=T0) is None


def test_balanced_shows_budget_only_when_at_risk(store, pressure):
    pressure["windows"] = [window(p50=90, p_hit=0.1)]
    assert steer.briefing(store, "/w/proj", now=T0) is None
    pressure["windows"] = [window(p50=90, p_hit=0.25)]
    text = steer.briefing(store, "/w/proj", now=T0)
    assert text.startswith("savetokens (balanced mode): Budget: 5-hour limit 40% used, ~90% expected")
    assert "25% chance of hitting it" in text
    assert steer.ECONOMY not in text


def test_lean_briefing_asks_for_economy(store, pressure):
    set_mode("lean")
    pressure["windows"] = [window(p50=20)]
    text = steer.briefing(store, "/w/proj", now=T0)
    assert "Budget:" in text and steer.ECONOMY in text
    assert len(text) < 900   # it is paid for on every later turn


def test_nudge_once_per_step_and_only_when_at_risk(store, pressure):
    set_mode("balanced")
    pressure["windows"] = [window(p50=95, p_hit=0.4)]
    assert steer.nudge(store, "s1", now=T0) is None          # balanced nudges from a 50% chance
    pressure["windows"] = [window(p50=104, p_hit=0.55)]
    agent, user = steer.nudge(store, "s1", now=T0)
    assert "~104% expected" in user and "55% chance" in user and steer.ECONOMY in agent
    pressure["windows"] = [window(p50=106, p_hit=0.6)]
    assert steer.nudge(store, "s1", now=T0 + 60) is None     # same step: quiet
    pressure["windows"] = [window(p50=115, p_hit=0.85)]
    assert steer.nudge(store, "s1", now=T0 + 120)            # worse: one more
    assert steer.nudge(store, "s2", now=T0 + 120)            # another session hears it too


def _history(store, project="proj", sessions=("a", "b"), big_chars=20_000):
    for i, sid in enumerate(sessions):
        store.add_usage([UsageEvent("claude-code", sid, f"r{sid}", T0 - 86400 + i, model="claude-sonnet-5-5",
                                    project=project, input=1, output=1)])
        events = [ToolEvent("claude-code", sid, T0 - 86400 + i, "Bash", "test", tool_use_id=f"{sid}t",
                            args_hash="x", output_chars=big_chars)]
        events += [ToolEvent("claude-code", sid, T0 - 86400 + i + k, "Read", "read", tool_use_id=f"{sid}r{k}",
                             args_hash=f"r{k}", target=f"/w/{project}/core.py") for k in range(4)]
        store.add_tools(events)


def test_lessons_need_evidence_from_two_sessions(store):
    _history(store, sessions=("a",))
    assert steer.lessons(store, "/w/proj", now=T0) == []
    _history(store, sessions=("b",))
    out = steer.lessons(store, "/w/proj", now=T0)
    assert out[0].startswith("Test runs here have printed up to 20k characters")
    assert out[1].startswith("core.py tend to get re-read")
    assert steer.lessons(store, "/w/other", now=T0) == []    # other projects don't inherit them


def test_session_start_and_prompt_hooks(store, pressure):
    set_mode("lean")
    pressure["windows"] = [window(p50=104)]
    out = claude_code.handle_hook("SessionStart", {"session_id": "s1", "cwd": "/w/proj", "source": "startup"}, store)
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert steer.ECONOMY in out["hookSpecificOutput"]["additionalContext"]
    out = claude_code.handle_hook("UserPromptSubmit", {"session_id": "s1", "prompt": "go"}, store)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert out["systemMessage"].startswith("savetokens: 5-hour limit")
    assert claude_code.handle_hook("UserPromptSubmit", {"session_id": "s1", "prompt": "again"}, store) is None


def test_hermes_first_turn_briefing_and_later_nudge(store, pressure):
    set_mode("lean")
    pressure["windows"] = [window(p50=104)]
    first = hermes.on_pre_llm(store, session_id="h1", is_first_turn=True)
    assert steer.ECONOMY in first["context"]
    assert "chance of hitting it" in hermes.on_pre_llm(store, session_id="h1", is_first_turn=False)["context"]
    assert hermes.on_pre_llm(store, session_id="h1", is_first_turn=False) is None


def test_hermes_plugin_registers_pre_llm_call(hermes_home):
    from savetokens import hermes_plugin
    hooks = {}

    class Ctx:
        def register_hook(self, name, fn):
            hooks[name] = fn
    hermes_plugin.register(Ctx())
    assert "pre_llm_call" in hooks
    assert hooks["pre_llm_call"](session_id="x", is_first_turn=True) is None   # balanced, no data: silent


def test_install_adds_steering_hooks_and_skill(homes):
    assert install.install_claude_code(yes=True, out=lambda *_: None)
    data = json.loads((homes / "claude" / "settings.json").read_text())
    assert {"SessionStart", "UserPromptSubmit"} <= set(data["hooks"])
    skill = install.skill_path()
    assert skill.exists() and "savetokens budget --json" in skill.read_text()
    install.uninstall_claude_code(out=lambda *_: None)
    assert not skill.exists()
    assert "hooks" not in json.loads((homes / "claude" / "settings.json").read_text())


def test_budget_json_shape(store, pressure):
    pressure["windows"] = [window(p50=104, resets=T0 + 1800)]
    b = steer.budget(store, now=T0)
    assert b["mode"] == "lean" and b["configured_mode"] == "auto" and b["advice"] == steer.ECONOMY
    assert b["mode_reason"].startswith("auto: 85% chance of hitting the 5-hour limit")
    assert b["limits"][0]["resets_in_min"] == 30


def test_install_connects_ephemeris_by_default_or_stays_local(homes, monkeypatch):
    from savetokens import ephemeris
    monkeypatch.setattr(ephemeris, "balance", lambda key: 1234.0)
    lines = []
    assert install.install_claude_code(out=lines.append, ask=lambda q: "" if "key" in q else "y")
    assert any("Ephemeris (https://ephemeris" in x for x in lines)           # disclosed before consent
    assert any("No Ephemeris key yet" in x for x in lines)
    assert load_config()["forecaster"] == "ephemeris" and not ephemeris.enabled()
    lines.clear()
    install.install_claude_code(yes=True, out=lines.append, ephemeris_key="k-123")
    assert ephemeris.api_key() == "k-123" and ephemeris.enabled()
    assert oct((homes / "st" / "ephemeris.env").stat().st_mode & 0o777) == "0o600"
    assert any("1,234 credits" in x for x in lines)
    install.install_claude_code(yes=True, out=lambda *_: None, no_ephemeris=True)
    assert load_config()["forecaster"] == "baseline" and not ephemeris.enabled()
