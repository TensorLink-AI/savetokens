import json

from savetokens import compaction, fixes, guard, install, notify, schedule, windows
from savetokens.adapters import claude_code as cc
from savetokens.store import UsageEvent, load_config

from conftest import T0


class FakeCrontab:
    def __init__(self, text=""):
        self.text = text

    def __call__(self, cmd, input=None, **_):
        class R:
            pass
        r = R()
        r.returncode, r.stdout = 0, ""
        if cmd == ["crontab", "-l"]:
            r.stdout = self.text
        elif cmd == ["crontab", "-"]:
            self.text = input
        return r


def test_crontab_line_added_once_and_removed_exactly(monkeypatch):
    monkeypatch.setattr(schedule, "available", lambda: True)
    fake = FakeCrontab("*/15 * * * * other-job\n")
    assert schedule.add("/x/savetokens", fake) and schedule.add("/x/savetokens", fake)
    assert fake.text.count(schedule.MARK) == 1 and "other-job" in fake.text
    schedule.remove(fake)
    assert fake.text == "*/15 * * * * other-job\n"


def write_compactions(t, pre_tokens_manual=(380_000, 410_000, 450_000), auto=(960_000,)):
    ts = T0
    for trig, values in (("manual", pre_tokens_manual), ("auto", auto)):
        for v in values:
            t.turn(ts, read=v)
            ts += 10
            with open(t.path, "a") as f:
                f.write(json.dumps({"type": "system", "subtype": "compact_boundary", "sessionId": t.session,
                                    "timestamp": __import__("conftest").iso(ts),
                                    "compactMetadata": {"trigger": trig, "preTokens": v}}) + "\n")
            ts += 10
            t.turn(ts, read=0, write5=30_000)
            ts += 10


def test_compactions_captured_and_window_suggested_from_manual_use(store, transcript):
    write_compactions(transcript())
    cc.backfill(store)
    by, cost, n = compaction.stats(store)
    assert n == 4 and by["manual"]["n"] == 3 and by["auto"]["median"] == 960_000 and cost > 0
    window, n_manual, med = compaction.suggested_window(store)
    assert (window, n_manual, med) == (400_000, 3, 410_000)


def test_autocompact_fix_sets_and_reverts_setting(store, transcript, homes):
    write_compactions(transcript())
    cc.backfill(store)
    settings = homes / "claude" / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}))
    plan = fixes.plan("autocompact-window", store)
    assert "400000" in plan and "median of 410k" in plan
    fixes.apply(store, "autocompact-window")
    assert json.loads(settings.read_text())["autoCompactWindow"] == 400_000
    assert compaction.configured_window() == 400_000
    fixes.revert(store, "autocompact-window")
    assert json.loads(settings.read_text()) == {"model": "opus"}


def test_context_alert_respects_window_and_idle_gap(store, homes):
    cfg = load_config()
    store.add_usage([UsageEvent("claude-code", "s1", "a", T0, model="claude-opus-5-5", cache_read=250_000),
                     UsageEvent("claude-code", "s1", "b", T0 + 30, model="claude-opus-5-5", cache_read=260_000)])
    a = guard._context(store, "s1", cfg)
    assert a and "/clear if the task changed" in a.message and "autocompact-window" in a.message
    (homes / "claude").mkdir(parents=True, exist_ok=True)
    (homes / "claude" / "settings.json").write_text(json.dumps({"autoCompactWindow": 280_000}))
    assert guard._context(store, "s1", cfg) is None          # auto-compact is about to fire anyway
    (homes / "claude" / "settings.json").write_text(json.dumps({}))
    store.add_usage([UsageEvent("claude-code", "s1", "c", T0 + 30 + 7200, model="claude-opus-5-5",
                                cache_read=270_000)])
    a = guard._context(store, "s1", cfg)
    assert "resumed after a break" in a.message and a.audience == guard.USER


def test_notify_is_silent_then_reports_new_alerts_once(store):
    assert notify.alerts(store, now=T0) == []
    store.add_alert("claude-code", "sess1234", "loop", "k", "warn", "savetokens: Bash was called 4 times", ts=T0 + 5)
    out = notify.alerts(store, now=T0 + 10)
    assert len(out) == 1 and "Bash was called 4 times" in out[0]
    assert notify.alerts(store, now=T0 + 20) == []


def test_learning_note_until_settled(store):
    from savetokens import limits
    store.set_meta("claude_code_billing", "subscription")
    for i in range(3):
        store.add_limits("claude-code", "s", ts=T0 + i * 600, seven_day_pct=10 + i, seven_day_resets=T0 + 86400)
    ls = limits.learning_state(store)
    assert not ls["settled"] and ls["readings"] == 3
    for i in range(3, 15):
        store.add_limits("claude-code", "s", ts=T0 + i * 1800, seven_day_pct=10 + i, seven_day_resets=T0 + 86400)
    assert limits.learning_state(store)["settled"]


def test_install_hermes_ships_desktop_half_and_cron_scripts(hermes_home, monkeypatch):
    calls = []
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: f"/usr/bin/{name}")
    ok = install.install_hermes(yes=True, notify="telegram", out=lambda *_: None,
                                run=lambda cmd, **k: calls.append(cmd) or type("R", (), {"returncode": 0})())
    assert ok
    dest = hermes_home / "plugins" / "savetokens"
    assert (dest / "desktop" / "plugin.js").exists() and (dest / "dashboard" / "plugin_api.py").exists()
    assert json.loads((dest / "dashboard" / "manifest.json").read_text())["api"] == "plugin_api.py"
    script = (hermes_home / "scripts" / "savetokens-alerts.sh").read_text()
    assert "notify --harness hermes" in script and script.startswith("#!/usr/bin/env bash")
    creates = [c for c in calls if c[:3] == ["hermes", "cron", "create"]]
    assert len(creates) == 2 and all("--no-agent" in c and "telegram" in c for c in creates)
    assert ["hermes", "gateway", "restart"] in calls
    install.uninstall_hermes(out=lambda *_: None, run=lambda cmd, **k: calls.append(cmd))
    assert not dest.exists() and not (hermes_home / "scripts" / "savetokens-alerts.sh").exists()
    assert ["hermes", "cron", "remove", "savetokens-daily"] in calls


def test_desktop_backend_route_returns_status(hermes_home, monkeypatch):
    import importlib.util
    import sys
    import types
    fake = types.ModuleType("fastapi")

    class APIRouter:
        def get(self, path):
            return lambda fn: fn
    fake.APIRouter = APIRouter
    monkeypatch.setitem(sys.modules, "fastapi", fake)
    from pathlib import Path
    import savetokens
    path = Path(savetokens.__file__).parent / "hermes_plugin" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("st_plugin_api", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.status()
    assert set(out) == {"text", "detail"} and out["text"]
