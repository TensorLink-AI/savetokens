"""Install into Claude Code (and Codex and Hermes, when they are here) and remove again.
Every change is shown before it is made. Codex and Hermes need no hooks: upkeep reads their sessions."""
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

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SubagentStop", "SessionEnd")
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


def codex_skill_path() -> Path:
    from . import codex
    return codex.codex_home() / "skills" / "savetokens" / "SKILL.md"


def hermes_skill_path() -> Path:
    from . import hermes
    return hermes.hermes_home() / "skills" / "savetokens" / "SKILL.md"


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


CODEX_MCP_START, CODEX_MCP_END = "# savetokens MCP (added by savetokens install)", "# end savetokens MCP"


def claude_cli():
    return shutil.which("claude")


def add_claude_mcp(exe, run=subprocess.run) -> bool:
    """Register `savetokens mcp` with Claude Code for every project (user scope), through its own CLI."""
    cli = claude_cli()
    if not cli:
        return False
    run([cli, "mcp", "remove", "--scope", "user", "savetokens"], capture_output=True, text=True)
    return run([cli, "mcp", "add", "--scope", "user", "savetokens", "--", *shlex.split(exe), "mcp"],
               capture_output=True, text=True).returncode == 0


def remove_claude_mcp(run=subprocess.run):
    cli = claude_cli()
    if cli:
        run([cli, "mcp", "remove", "--scope", "user", "savetokens"], capture_output=True, text=True)


def _codex_config():
    from . import codex
    return codex.codex_home() / "config.toml"


def _without_codex_mcp(text):
    out, skip = [], False
    for line in text.splitlines():
        if line.strip() == CODEX_MCP_START:
            skip = True
        elif skip and line.strip() == CODEX_MCP_END:
            skip = False
        elif not skip:
            out.append(line)
    return "\n".join(out).rstrip() + ("\n" if out else "")


def add_codex_mcp(exe) -> bool:
    """A marked [mcp_servers.savetokens] block at the end of Codex's config.toml (one of yours is kept)."""
    path = _codex_config()
    text = path.read_text() if path.exists() else ""
    text = _without_codex_mcp(text)
    if "[mcp_servers.savetokens]" in text:
        return True
    parts = shlex.split(exe)
    block = (f"\n{CODEX_MCP_START}\n[mcp_servers.savetokens]\ncommand = {json.dumps(parts[0])}\n"
             f"args = {json.dumps(parts[1:] + ['mcp'])}\n{CODEX_MCP_END}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = home() / "backups" / "codex-config.toml"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(path, backup)
    path.write_text(text + block)
    return True


def remove_codex_mcp():
    path = _codex_config()
    if path.exists() and CODEX_MCP_START in path.read_text():
        path.write_text(_without_codex_mcp(path.read_text()))


def _hermes_config():
    from . import hermes
    return hermes.hermes_home() / "config.yaml"


def _without_hermes_mcp(text):
    """Hermes's config.yaml without our server: the marked block, or (once Hermes has rewritten the file and
    dropped the comments) a `savetokens:` server under mcp_servers that runs `savetokens mcp`."""
    text = _without_codex_mcp(text)
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        line = lines[i]
        indent = len(line) - len(line.lstrip())
        if indent and line.strip() == "savetokens:":
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent):
                j += 1
            body = "\n".join(lines[i + 1:j])
            if "savetokens" in body and "mcp" in body:
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out).rstrip() + ("\n" if out else "")


def add_hermes_mcp(exe) -> bool:
    """A savetokens server under mcp_servers in Hermes's config.yaml, between marker comments (one of yours is
    kept). False when mcp_servers is written in a form this can't safely add to."""
    path = _hermes_config()
    text = _without_hermes_mcp(path.read_text()) if path.exists() else ""
    parts = shlex.split(exe)
    lines = text.splitlines()
    at = next((i for i, l in enumerate(lines) if l.split("#")[0].rstrip() in ("mcp_servers:", "mcp_servers: {}")),
              None)
    if at is None and any(l.startswith("mcp_servers:") for l in lines):
        return False                                   # a flow-style map: leave it to the user
    if at is not None and any(l.strip() == "savetokens:" for l in lines[at + 1:]):
        return True
    pad = "  "
    if at is not None:
        nxt = next((l for l in lines[at + 1:] if l.strip() and not l.lstrip().startswith("#")), "")
        pad = " " * ((len(nxt) - len(nxt.lstrip())) or 2)     # the file's own indent
    server = ["savetokens:", f"{pad}command: {json.dumps(parts[0])}", f"{pad}args: {json.dumps(parts[1:] + ['mcp'])}"]
    if at is None:
        lines += ["", CODEX_MCP_START, "mcp_servers:"] + [pad + l for l in server] + [CODEX_MCP_END]
    else:
        lines[at] = "mcp_servers:"
        lines[at + 1:at + 1] = [pad + CODEX_MCP_START] + [pad + l for l in server] + [pad + CODEX_MCP_END]
    if path.exists():
        backup = home() / "backups" / "hermes-config.yaml"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).lstrip("\n") + "\n")
    return True


def remove_hermes_mcp():
    path = _hermes_config()
    if path.exists():
        text = path.read_text()
        new = _without_hermes_mcp(text)
        if new != text:
            path.write_text(new)


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
            run=subprocess.run, code=None, tell_agent=None, mcp=True, secret=None) -> bool:
    """server with token or code: connect to a savetokens server, which then makes the forecasts (no key
    needed here). tell_agent: also give the agent a pacing note when a limit is at risk (None: ask)."""
    data, cfg_updates, changes = plan()
    out(f"savetokens will change {settings_path()}:")
    for c in changes:
        out(f"  - {c}")
    from . import codex, hermes
    has_codex, has_hermes = codex.codex_home().is_dir(), hermes.hermes_home().is_dir()
    others = [s for s, has in (("~/.codex/sessions", has_codex), ("~/.hermes/state.db", has_hermes)) if has]
    out("  - read usage from " + " and ".join(["~/.claude/projects"] + others)
        + ": token counts, costs, limit readings and limit errors only, kept in ~/.savetokens")
    skills = [skill_path()] + [p for p, has in ((codex_skill_path(), has_codex), (hermes_skill_path(), has_hermes))
                               if has]
    out(f"  - add the savetokens skill at {' and '.join(map(str, skills))}"
        ": ask your agent about your limits and how to pace them")
    if mcp:
        out("  - the savetokens MCP server (pacing_brief, estimate_job) for Claude Code"
            + (", Codex (a marked block in ~/.codex/config.toml)" if has_codex else "")
            + (", Hermes (a marked block under mcp_servers in ~/.hermes/config.yaml)" if has_hermes else "")
            + ": your agent can check limits and size jobs without a shell command")
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
        out("  - one crontab line: upkeep every 10 minutes (reads Codex and Hermes sessions, keeps forecasts current while"
            " Claude Code is closed; skip with --no-schedule)")
    if not yes and ask("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        out("Nothing changed.")
        return False
    if tell_agent is None:
        tell_agent = (not yes and ask("Also tell your agent when a limit is at risk, so it can pace itself? It adds"
                                      " a short note (about 100 tokens) only then. [y/N] ").strip().lower()
                      in ("y", "yes"))
    if server and code and not token:
        import urllib.request
        req = urllib.request.Request(server.rstrip("/") + "/v1/join", data=json.dumps({"code": code}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                token = json.load(r)["token"]
        except Exception as e:
            out(f"Couldn't join {server} with that code ({e}). Nothing changed.")
            return False
    path = settings_path()
    if path.exists():
        backup = home() / "backups" / "claude-settings.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(path, backup)
    _write_json(path, data)
    for dest in skills:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SKILL_SRC, dest)
    cfg = load_config()
    cfg.update(cfg_updates)
    cfg["forecaster"] = "baseline" if no_ephemeris else "ephemeris"
    cfg["agent_context"] = bool(tell_agent)
    if server:
        cfg.update(server_url=server, server_token=token)
    elif key:
        ephemeris.save_key(key, cfg)
    save_config(cfg)
    if cron and not schedule.add(executable(), run):
        out("Could not update the crontab; upkeep still runs whenever Claude Code is open.")
    if mcp:
        if not add_claude_mcp(executable(), run):
            out("Claude Code's CLI wasn't found, so the MCP server isn't registered there; the skill still works."
                " Later: claude mcp add --scope user savetokens -- savetokens mcp")
        if has_codex:
            add_codex_mcp(executable())
        if has_hermes and not add_hermes_mcp(executable()):
            out("Hermes's mcp_servers is written inline, so the MCP server isn't added there; the skill still"
                " works. Later: hermes mcp add savetokens --command savetokens --args mcp")
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
    from . import setup
    with Store() as s:   # plans (their price detected), budgets, the Ephemeris key
        setup.run(s, load_config(), interactive=not yes, accept=yes, out=out, ask=ask, secret=secret)
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
    remove_claude_mcp(run)
    remove_codex_mcp()
    remove_hermes_mcp()
    for dest in (skill_path(), codex_skill_path(), hermes_skill_path()):
        if dest.exists():
            shutil.rmtree(dest.parent, ignore_errors=True)
    out(f"Removed savetokens' hooks, statusline, skill, MCP server and crontab line. Your data stays in {home()}.")
