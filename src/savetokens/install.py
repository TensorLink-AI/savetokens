"""Install into Claude Code and remove again. Every change is shown before it is made."""
from __future__ import annotations

import json
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import capture, ephemeris, schedule
from .store import Store, home, load_config, save_config

HOOK_EVENTS = ("UserPromptSubmit", "Stop", "SubagentStop", "SessionEnd")
MARK = "savetokens hook"


def executable() -> str:
    found = shutil.which("savetokens")
    return found or f"{shlex.quote(sys.executable)} -m savetokens"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".savetokens.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def _ours(entry) -> bool:
    return any(MARK in h.get("command", "") for h in entry.get("hooks", []))


def _our_statusline(sl) -> bool:
    cmd = (sl or {}).get("command", "")
    return "savetokens" in cmd and cmd.rstrip().endswith(" statusline")


SKILL_SRC = Path(__file__).with_name("skill") / "SKILL.md"


def skill_path() -> Path:
    return capture.claude_home() / "skills" / "savetokens" / "SKILL.md"


def settings_path() -> Path:
    return capture.claude_home() / "settings.json"


def _without_ours(hooks: dict) -> dict:
    """Hooks minus savetokens' own, from any version (older ones hooked more events)."""
    out = {}
    for event, entries in hooks.items():
        kept = [e for e in entries if not _ours(e)]
        if kept:
            out[event] = kept
    return out


def plan(exe=None):
    """(new settings, config updates, human-readable changes)."""
    exe = exe or executable()
    data = _read_json(settings_path())
    hooks = _without_ours(data.get("hooks", {}))
    for event in HOOK_EVENTS:
        hooks.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": f"{exe} hook claude-code", "timeout": 3}]})
    data["hooks"] = hooks
    changes = [f"hooks ({', '.join(HOOK_EVENTS)}): read usage as sessions run; show pace alerts on your next"
               " prompt (fail open, 3 s timeout)"]
    current = data.get("statusLine") or {}
    cfg = {}
    if not _our_statusline(current):
        if current.get("command"):
            cfg["statusline_wrapped"] = current["command"]
            changes.append(f"statusLine: keep your `{current['command']}` and add the savetokens segment after it")
        else:
            changes.append("statusLine: show each limit's % used and where it is heading")
        cfg["statusline_previous"] = current or None
        data["statusLine"] = {"type": "command", "command": f"{exe} statusline", "padding": 0}
    return data, cfg, changes


def import_old_meter(store: Store, old: Path | None = None) -> int:
    """Limit readings recorded by savetokens 0.1 (they can't be rebuilt from transcripts)."""
    old = old or home() / "events.db"
    if not old.exists() or store.meta("imported_old_meter"):
        return 0
    con = sqlite3.connect(old)
    cols = {r[1] for r in con.execute("PRAGMA table_info(limits)")}
    acct = "account" if "account" in cols else "NULL"
    rows = []
    for ts, a, p5, r5, p7, r7 in con.execute(f"SELECT ts, {acct}, five_hour_pct, five_hour_resets, seven_day_pct,"
                                             " seven_day_resets FROM limits WHERE harness = 'claude-code'"):
        for name, pct, resets in (("five_hour", p5, r5), ("seven_day", p7, r7)):
            if pct is not None:
                rows.append((store.machine, "claude-code", a, ts, name, pct, resets))
    con.close()
    n = store.insert("meter", ["machine", "harness", "account", "ts", "name", "pct", "resets"], rows)
    store.set_meta("imported_old_meter", True)
    return n


def install(yes=False, key=None, no_ephemeris=False, cron=True, server=None, token=None, out=print, ask=input,
            run=subprocess.run) -> bool:
    """server/token: connect to a savetokens server, which then makes the forecasts (no key needed here)."""
    data, cfg_updates, changes = plan()
    out(f"savetokens will change {settings_path()}:")
    for c in changes:
        out(f"  - {c}")
    out("  - read usage from ~/.claude/projects: token counts and limit errors only, kept in ~/.savetokens")
    out(f"  - add the savetokens skill at {skill_path()}: ask your agent about your limits and how to pace them")
    if server:
        out(f"  - sync with {server}: token counts per request, limit readings and limit hits go there;"
            " forecasts come back. Never prompts, replies, code or file names")
    elif no_ephemeris:
        out("  - forecasts: local baseline only (--no-ephemeris); nothing leaves this machine")
    else:
        out(f"  - forecasts: Ephemeris ({ephemeris.SITE}). It is sent one number per hour: the % of your weekly"
            " limit used. Nothing else. Hourly while you work, every 3 hours otherwise.")
    cron = cron and schedule.available()
    if cron:
        out("  - one crontab line: hourly upkeep while Claude Code is closed (skip with --no-schedule)")
    if not yes and ask("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        out("Nothing changed.")
        return False
    path = settings_path()
    if path.exists():
        backup = home() / "backups" / "claude-settings.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(path, backup)
    _write_json(path, data)
    skill_path().parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL_SRC, skill_path())
    cfg = load_config()
    cfg.update(cfg_updates)
    cfg["forecaster"] = "baseline" if no_ephemeris else "ephemeris"
    if server:
        cfg.update(server_url=server, server_token=token)
    elif key:
        ephemeris.save_key(key, cfg)
    elif not no_ephemeris and not ephemeris.api_key(cfg) and not yes:
        pasted = ask(f"Ephemeris API key (from {ephemeris.SITE}; Enter to skip): ").strip()
        if pasted:
            ephemeris.save_key(pasted, cfg)
    save_config(cfg)
    if cron and not schedule.add(executable(), run):
        out("Could not update the crontab; upkeep still runs whenever Claude Code is open.")
    from . import maintain
    with Store() as s:
        n = capture.backfill(s)
        m = import_old_meter(s)
        maintain.run(s)
        err = s.meta("sync_error") if server else None
    out(f"Installed. Read {n:,} requests" + (f" and {m:,} earlier limit readings" if m else "") + ".")
    if server:
        out(f"Couldn't reach {server} yet ({err['error']}); it retries in the background." if err
            else f"Synced with {server}.")
    elif not no_ephemeris and not ephemeris.api_key():
        out(f"No Ephemeris key yet: forecasts use the local baseline. Add one with"
            f" `savetokens ephemeris --key KEY` ({ephemeris.SITE}).")
    out("The statusline updates on your next message. `savetokens status` shows the full picture.")
    return True


def uninstall(out=print, run=subprocess.run):
    path = settings_path()
    data = _read_json(path)
    hooks = _without_ours(data.get("hooks", {}))
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    cfg = load_config()
    if _our_statusline(data.get("statusLine")):
        prev = cfg.get("statusline_previous")
        if prev:
            data["statusLine"] = prev
        else:
            data.pop("statusLine", None)
    for k in ("statusline_wrapped", "statusline_previous"):
        cfg.pop(k, None)
    save_config(cfg)
    _write_json(path, data)
    schedule.remove(run)
    if skill_path().exists():
        shutil.rmtree(skill_path().parent, ignore_errors=True)
    out(f"Removed savetokens' hooks, statusline, skill and crontab line. Your data stays in {home()}.")
