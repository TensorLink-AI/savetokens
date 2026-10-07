"""Install and uninstall per harness. Every change is shown before it is made."""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import savetokens

from .adapters import claude_code, hermes
from .store import home, load_config, save_config

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "SubagentStop")
MARK = "savetokens hook"


def executable() -> str:
    found = shutil.which("savetokens")
    return found or f"{shlex.quote(sys.executable)} -m savetokens"


def _hook_entry(event, exe):
    entry = {"hooks": [{"type": "command", "command": f"{exe} hook claude-code", "timeout": 3}]}
    if event.startswith(("PreToolUse", "PostToolUse")):
        entry["matcher"] = "*"
    return entry


def _ours(entry) -> bool:
    return any(MARK in h.get("command", "") for h in entry.get("hooks", []))


def _our_statusline(sl) -> bool:
    cmd = (sl or {}).get("command", "")
    return "savetokens" in cmd and cmd.rstrip().endswith(" statusline")


def claude_code_changes(block=False):
    """(new settings, list of human-readable changes)."""
    from .fixes import _read_json
    path = claude_code.claude_home() / "settings.json"
    data = _read_json(path)
    exe = executable()
    changes = []
    hooks = data.setdefault("hooks", {})
    events = HOOK_EVENTS + (("PreToolUse",) if block else ())
    for event in events:
        entries = [e for e in hooks.get(event, []) if not _ours(e)]
        entries.append(_hook_entry(event, exe))
        hooks[event] = entries
        changes.append(f"hooks.{event}: add `{exe} hook claude-code` (fails open, 3 s timeout)")
    if not block:
        rest = [e for e in hooks.get("PreToolUse", []) if not _ours(e)]
        if rest:
            hooks["PreToolUse"] = rest
        else:
            hooks.pop("PreToolUse", None)
    current = data.get("statusLine") or {}
    cfg_updates = {}
    if not _our_statusline(current):
        if current.get("command"):
            cfg_updates["statusline_wrapped"] = current["command"]
            changes.append(f"statusLine: keep your `{current['command']}` and append the savetokens segment")
        else:
            changes.append("statusLine: show the savetokens segment")
        cfg_updates["statusline_previous"] = current or None
        data["statusLine"] = {"type": "command", "command": f"{exe} statusline", "padding": 0}
    return path, data, changes, cfg_updates


SKILL_SRC = Path(__file__).with_name("skills") / "claude-code" / "SKILL.md"


def _ephemeris_change(out, no_ephemeris):
    from . import ephemeris
    if no_ephemeris:
        out("  - forecasts: local baseline only (--no-ephemeris); nothing leaves this machine")
    else:
        out(f"  - forecasts: Ephemeris ({ephemeris.SITE}), the default forecaster. Every 6 hours it sends"
            " hourly API-equivalent dollar totals (no prompts, tokens, project or session names);"
            " --no-ephemeris keeps everything local")


def _ephemeris_setup(cfg, key, no_ephemeris, yes, out, ask):
    """Connect Ephemeris (asking for a key when none is found), or keep forecasts local."""
    from . import ephemeris
    if no_ephemeris:
        cfg["forecaster"] = "baseline"
        return
    cfg["forecaster"] = "ephemeris"
    if key:
        ephemeris.save_key(key, cfg)
    elif not ephemeris.api_key(cfg) and not yes:
        pasted = ask(f"Ephemeris API key (from {ephemeris.SITE}; Enter to skip for now): ").strip()
        if pasted:
            ephemeris.save_key(pasted, cfg)
    found = ephemeris.api_key(cfg)
    if not found:
        out(f"No Ephemeris key yet, so forecasts use the local baseline for now. Get a key at {ephemeris.SITE},"
            " then run `savetokens ephemeris connect --key <key>`.")
        return
    try:
        out(f"Ephemeris connected: {ephemeris.balance(found):,.0f} credits available.")
    except Exception as e:
        out(f"Ephemeris didn't accept the key ({e}); forecasts use the local baseline until"
            " `savetokens ephemeris connect --key <key>` succeeds.")


def skill_path() -> Path:
    return claude_code.claude_home() / "skills" / "savetokens" / "SKILL.md"


def install_claude_code(yes=False, block=False, schedule_upkeep=True, out=print, ask=input,
                        run=subprocess.run, ephemeris_key=None, no_ephemeris=False, levers=True) -> bool:
    from . import schedule
    path, data, changes, cfg_updates = claude_code_changes(block)
    out(f"savetokens will change {path}:")
    for c in changes:
        out(f"  - {c}")
    out(f"  - add the savetokens skill at {skill_path()} (lets the agent check your budget before big jobs)")
    out("  - backfill usage from ~/.claude/projects (counts only, kept on this machine)")
    _ephemeris_change(out, no_ephemeris)
    if levers:
        out("  - when a limit is forecast to be hit (auto mode), temporarily set cheaper options for new sessions"
            " and subagents: subagents on Sonnet and effort low in ~/.claude/settings.json; Codex effort low in"
            " ~/.codex/config.toml (earlier compaction is opt-in: `savetokens levers on --allow ...`). Undone when the risk passes, at reset,"
            " or when tests start failing; never the main model unless you allow it. Skip with --no-levers")
    cron = schedule_upkeep and schedule.available()
    if cron:
        out("  - add one line to your crontab: hourly upkeep (learning limits, forecasts, scoring) while"
            " Claude Code is closed; skip with --no-schedule")
    if not yes and ask("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        out("Nothing changed.")
        return False
    if path.exists():
        backup = home() / "backups" / "claude-settings.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(path, backup)
    from .fixes import _write_json
    _write_json(path, data)
    skill_path().parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL_SRC, skill_path())
    cfg = load_config()
    cfg.update(cfg_updates, block=block)
    given = cfg.get("levers_consent")
    given = ["claude-code", "codex"] if given is True else list(given or [])
    if levers:
        cfg["levers_consent"] = sorted(set(given) | {"claude-code", "codex"})
    else:
        cfg["levers_consent"] = [h for h in given if h not in ("claude-code", "codex")]
    _ephemeris_setup(cfg, ephemeris_key, no_ephemeris, yes, out, ask)
    save_config(cfg)
    if cron and not schedule.add(executable(), run):
        out("Could not update the crontab; upkeep still runs whenever Claude Code is open.")
    from . import maintain
    from .store import Store
    with Store() as s:
        n = claude_code.backfill(s)
        maintain.run(s)
    out(f"Installed. Backfilled {n:,} requests. It is live now: the statusline updates on your next message,"
        " and the guard checks every tool call. Try `savetokens status` and `savetokens report`.")
    out("Limit forecasts sharpen over the first day as readings arrive; the statusline says 'learning' until then.")
    return True


def uninstall_claude_code(out=print, run=subprocess.run):
    from . import schedule
    from .fixes import _read_json, _write_json
    path = claude_code.claude_home() / "settings.json"
    data = _read_json(path)
    hooks = data.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not _ours(e)]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
    cfg = load_config()
    if _our_statusline(data.get("statusLine")):
        prev = cfg.get("statusline_previous")
        if prev:
            data["statusLine"] = prev
        else:
            data.pop("statusLine", None)
    cfg.pop("statusline_wrapped", None)
    cfg.pop("statusline_previous", None)
    save_config(cfg)
    _write_json(path, data)
    schedule.remove(run)
    if skill_path().exists():
        shutil.rmtree(skill_path().parent, ignore_errors=True)
    out(f"Removed savetokens hooks, skill, statusline and crontab line from {path}. Your data stays in {home()}.")


HERMES_JOBS = (("savetokens-alerts", "every 15m", "savetokens-alerts.sh", "notify --harness hermes"),
               ("savetokens-daily", "0 9 * * *", "savetokens-daily.sh", "notify --daily"))


def _copy_plugin(dest: Path):
    src = Path(__file__).with_name("hermes_plugin")
    shutil.copytree(src, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    (dest / "package_path.txt").write_text(str(Path(savetokens.__file__).resolve().parent.parent) + "\n")


def install_hermes(yes=False, block=False, notify="local", restart=True, out=print, ask=input,
                   run=subprocess.run, ephemeris_key=None, no_ephemeris=False, levers=True) -> bool:
    """notify: where the cron jobs deliver ("local" keeps output in Hermes; "telegram" etc. sends it)."""
    h = hermes.hermes_home()
    dest = h / "plugins" / "savetokens"
    has_cli = shutil.which("hermes") is not None
    out("savetokens will:")
    out(f"  - copy its Hermes plugin to {dest} (agent hooks, plus a Desktop status-bar item)")
    out("  - run `hermes plugins enable savetokens`")
    out(f"  - add Hermes cron jobs: alerts every 15 minutes (silent unless something is new) and a daily"
        f" summary at 09:00, delivered to {notify}; they also keep forecasts and scoring current")
    if restart:
        out("  - restart the Hermes gateway so the plugin loads now (CLI sessions pick it up when they next start)")
    out(f"  - backfill usage from {h / 'state.db'} and the token-tracker database if present")
    if block:
        out("  - enable blocking: repeated identical tool calls and re-runs of failing tests are refused")
    _ephemeris_change(out, no_ephemeris)
    if levers:
        out("  - when a budget you set (`savetokens budgets add`) is forecast to be overspent, temporarily set"
            " cheaper options through `hermes config`: side tasks on the cheapest model you already configured"
            " and a lower OpenRouter pareto-code score if you use that router."
            " Undone when the risk passes, at the period's end, or when tests start failing; never your main"
            " model. Skip with --no-levers")
    if not yes and ask("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        out("Nothing changed.")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    _copy_plugin(dest)
    cfg = load_config()
    cfg["block"] = block
    if shutil.which("hermes"):
        cfg["hermes_exe"] = shutil.which("hermes")      # cron and upkeep may run without Hermes on PATH
    given = cfg.get("levers_consent")
    given = ["claude-code", "codex"] if given is True else list(given or [])
    cfg["levers_consent"] = sorted(set(given) | {"hermes"}) if levers else [h for h in given if h != "hermes"]
    _ephemeris_setup(cfg, ephemeris_key, no_ephemeris, yes, out, ask)
    save_config(cfg)
    scripts = h / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    exe = executable()
    for _, _, script, args in HERMES_JOBS:
        p = scripts / script
        # cron scripts get a sanitised environment, so use the absolute path to savetokens
        p.write_text(f"#!/usr/bin/env bash\n# savetokens: prints only when there is something to say\n"
                     f"exec {exe} {args}\n")
        p.chmod(0o755)
    if has_cli:
        r = run(["hermes", "plugins", "enable", "savetokens"], capture_output=True, text=True)
        out("Enabled the plugin." if r.returncode == 0 else "Enable it yourself: hermes plugins enable savetokens")
        for name, when, script, _ in HERMES_JOBS:
            run(["hermes", "cron", "remove", name], capture_output=True, text=True)
            r = run(["hermes", "cron", "create", when, "--no-agent", "--script", script, "--deliver", notify,
                     "--name", name], capture_output=True, text=True)
            if r.returncode != 0:
                out(f"Could not create cron job {name}: hermes cron create \"{when}\" --no-agent --script {script}"
                    f" --deliver {notify} --name {name}")
        if restart:
            r = run(["hermes", "gateway", "restart"], capture_output=True, text=True)
            out("Restarted the gateway." if r.returncode == 0
                else "Restart the gateway yourself: hermes gateway restart")
    else:
        out("hermes is not on PATH. Finish with: hermes plugins enable savetokens; then create the cron jobs"
            " (see `savetokens capabilities --json`) and run hermes gateway restart")
    from . import maintain
    from .store import Store
    with Store() as s:
        n = hermes.backfill(s, h)
        maintain.run(s)
    out(f"Installed. Backfilled {n:,} usage rows. Try `savetokens status`.")
    out("Hermes Desktop: turn on the savetokens status-bar item in Capabilities → Plugins (desktop halves start off).")
    return True


def uninstall_hermes(out=print, run=subprocess.run):
    h = hermes.hermes_home()
    dest = h / "plugins" / "savetokens"
    if shutil.which("hermes"):
        run(["hermes", "plugins", "disable", "savetokens"], capture_output=True, text=True)
        for name, _, _, _ in HERMES_JOBS:
            run(["hermes", "cron", "remove", name], capture_output=True, text=True)
    for _, _, script, _ in HERMES_JOBS:
        (h / "scripts" / script).unlink(missing_ok=True)
    shutil.rmtree(dest, ignore_errors=True)
    out(f"Removed {dest}, its cron jobs and scripts. Restart Hermes. Your data stays in {home()}.")


def capabilities() -> dict:
    return {
        "name": "savetokens",
        "version": savetokens.__version__,
        "harnesses": ["claude-code", "hermes"],
        "commands": {
            "install <harness> [--yes] [--block]": "add hooks/plugin and backfill; shows changes first",
            "status": "rate-limit and spend forecast",
            "budget --json": "mode, limits used and forecast at reset, and advice; check before large jobs",
            "mode [quality|balanced|lean|auto]": "the quality knob: how much savetokens steers the agent",
            "levers [status|on|off|revert]": "cheaper models, effort and compaction while a limit is at risk",
            "report [--days N]": "where tokens went and the biggest waste, in dollars",
            "fixes list|plan|apply|revert|impact <id>": "canned fixes, applied only with consent",
            "guard --block on|off": "opt in or out of blocking runaway calls",
            "uninstall <harness>": "remove hooks/plugin; keeps local data",
        },
        "data": str(home()),
        "network": "Ephemeris API (hourly usage totals, every 6 hours) unless forecaster is 'baseline'",
    }


def capabilities_json() -> str:
    return json.dumps(capabilities(), indent=2)
