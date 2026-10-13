"""Make this machine the hub: `savetokens server --setup`.

Runs the sync server here, starting on boot (a systemd user service, a launchd agent on macOS, or
an @reboot crontab line), with the Ephemeris key from the shared credentials file, connects this
machine to it, and prints the one line that adds every other machine:

    curl -fsSL http://<hub>:8787/join | sh -s XXXX-XXXX

The address it prints prefers Tailscale (private and encrypted between your machines, no domain
needed), then this machine's address on the local network.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .store import home

SERVICE = "savetokens-server"
LAUNCHD_LABEL = "ai.tensorlink.savetokens-server"
CRON_MARK = "# savetokens hub"


def _ok(run, cmd) -> bool:
    try:
        return run(cmd, capture_output=True, text=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def tailscale(run=subprocess.run) -> str | None:
    """This machine's Tailscale name (or address), when Tailscale is up."""
    if not shutil.which("tailscale"):
        return None
    try:
        r = run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
        me = json.loads(r.stdout or "{}").get("Self") or {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if r.returncode != 0 or not me.get("Online", True):
        return None
    name = (me.get("DNSName") or "").rstrip(".")
    ips = [ip for ip in me.get("TailscaleIPs") or [] if ":" not in ip]
    return name or (ips[0] if ips else None)


def lan_address() -> str | None:
    """This machine's address on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))   # TEST-NET: only picks the outgoing interface
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def https_name(port, run=subprocess.run) -> str | None:
    """https://NAME when Tailscale serves this hub over HTTPS (`tailscale serve` or `tailscale funnel`)."""
    if not shutil.which("tailscale"):
        return None
    try:
        r = run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=5)
        web = json.loads(r.stdout or "{}").get("Web") or {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for hostport, conf in web.items():
        proxies = [h.get("Proxy") or "" for h in ((conf or {}).get("Handlers") or {}).values()]
        if hostport.endswith(":443") and any(p.rstrip("/").endswith(f":{port}") for p in proxies):
            return "https://" + hostport[:-4]
    return None


def serve_https(port, run=subprocess.run, out=print) -> str | None:
    """Have Tailscale serve the hub over HTTPS, on your tailnet only (not the internet): `tailscale serve`.
    Returns https://NAME, or None when Tailscale can't (HTTPS off for the tailnet, no permission)."""
    try:
        run(["tailscale", "serve", "--bg", "--yes", str(port)], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        pass
    url = https_name(port, run)
    if url:
        out(f"- HTTPS on your tailnet (tailscale serve): {url}. Only your Tailscale devices can reach it.")
    else:
        out(f"- For HTTPS, turn on HTTPS certificates for your tailnet (Tailscale admin, DNS), then run"
            f" `tailscale serve --bg {port}` and `savetokens server --setup` again. Plain Tailscale works meanwhile.")
    return url


def address(port, run=subprocess.run) -> tuple[str, str]:
    """(URL other machines use, how they reach it: tailscale, lan or local)."""
    ts = tailscale(run)
    if ts:
        return f"http://{ts}:{port}", "tailscale"
    ip = lan_address()
    return (f"http://{ip}:{port}", "lan") if ip else (f"http://127.0.0.1:{port}", "local")


def _systemd_unit(cmd) -> str:
    return (f"[Unit]\nDescription=savetokens hub: every machine's usage and forecasts\nAfter=network-online.target\n\n"
            f"[Service]\nExecStart={shlex.join(cmd)}\nRestart=on-failure\nRestartSec=5\n\n"
            f"[Install]\nWantedBy=default.target\n")


def _launchd_plist(cmd) -> str:
    args = "".join(f"<string>{a}</string>" for a in cmd)
    log = home() / "server.log"
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
            f' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
            f"<key>Label</key><string>{LAUNCHD_LABEL}</string><key>ProgramArguments</key><array>{args}</array>"
            f"<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
            f"<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>"
            f"</dict></plist>\n")


def systemd_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / f"{SERVICE}.service"


def launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def start_on_boot(cmd, run=subprocess.run, popen=subprocess.Popen, platform=sys.platform) -> str | None:
    """Run cmd now and at every boot. Returns how: systemd, launchd or cron (None: couldn't)."""
    if platform == "darwin":
        path = launchd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_launchd_plist(cmd))
        _ok(run, ["launchctl", "unload", str(path)])
        return "launchd" if _ok(run, ["launchctl", "load", "-w", str(path)]) else None
    if shutil.which("systemctl") and _ok(run, ["systemctl", "--user", "show-environment"]):
        path = systemd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_systemd_unit(cmd))
        if (_ok(run, ["systemctl", "--user", "daemon-reload"])
                and _ok(run, ["systemctl", "--user", "enable", SERVICE])
                and _ok(run, ["systemctl", "--user", "restart", SERVICE])):
            # keep it running when you're logged out (may need admin rights; harmless if refused)
            _ok(run, ["loginctl", "enable-linger", os.environ.get("USER", "")])
            return "systemd"
    from . import schedule
    if schedule.available():
        current = [l for l in schedule._read(run).splitlines() if CRON_MARK not in l]
        if schedule._write("\n".join(current + [f"@reboot {shlex.join(cmd)} >/dev/null 2>&1 {CRON_MARK}"]) + "\n", run):
            log = open(home() / "server.log", "ab")
            popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
            return "cron"
    return None


def stop(run=subprocess.run, platform=sys.platform) -> bool:
    """Stop the hub and don't start it at boot any more. The data stays."""
    done = False
    if platform == "darwin" and launchd_path().exists():
        _ok(run, ["launchctl", "unload", "-w", str(launchd_path())])
        launchd_path().unlink()
        done = True
    if systemd_path().exists():
        _ok(run, ["systemctl", "--user", "disable", "--now", SERVICE])
        systemd_path().unlink()
        _ok(run, ["systemctl", "--user", "daemon-reload"])
        done = True
    from . import schedule
    if schedule.available():
        text = schedule._read(run)
        if CRON_MARK in text:
            kept = [l for l in text.splitlines() if CRON_MARK not in l]
            schedule._write(("\n".join(kept) + "\n") if kept else "", run)
            _ok(run, ["pkill", "-f", "savetokens server --host"])
            done = True
    return done


def healthy(url, wait=10.0, sleep=time.sleep) -> bool:
    end = time.time() + wait
    while True:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/healthz", timeout=2) as r:
                return bool(json.load(r).get("ok"))
        except Exception:
            if time.time() >= end:
                return False
            sleep(0.5)


def setup(port=8787, data=None, interactive=True, out=print, ask=input, open_url=None, run=subprocess.run,
          popen=subprocess.Popen, platform=sys.platform, wait=10.0, public=None) -> dict | None:
    """Make this machine the hub. Returns {"url", "how", "command", "code", "started"}."""
    from . import ephemeris, install, server
    from .store import load_config, save_config
    data = Path(data) if data else home() / "server"
    cfg = load_config()
    out("Making this machine your savetokens hub: every machine sends its usage here, and it makes the forecasts.")

    # 1. Ephemeris, for the forecasts (the hub's key covers every machine)
    if ephemeris.api_key(cfg):
        out("- Forecasts: Ephemeris (your key is set).")
    elif interactive:
        a = ask("- Forecasts: sign in to Ephemeris in your browser for a key? [Y/n] ").strip().lower()
        if a in ("", "y", "yes"):
            try:
                key = ephemeris.device_login(out=out, open_url=open_url or __import__("webbrowser").open)
                ephemeris.save_key(key, cfg)
                out("  Ephemeris key saved; the hub forecasts with it.")
            except Exception as e:
                out(f"  Sign-in didn't finish ({str(e)[:120]}); the hub uses its local baseline until"
                    " `savetokens setup` adds a key.")
    else:
        out("- Forecasts: local baseline (no Ephemeris key; `savetokens setup` adds one).")

    # 2. the server, now and at every boot, at the address every join command will use: one you gave, one
    #    saved before, HTTPS from Tailscale, then Tailscale's name, then this network's
    url, how = address(port, run)
    host = "0.0.0.0" if how != "local" else "127.0.0.1"
    fixed = public or server.public_url(data) or https_name(port, run)
    if not fixed and how == "tailscale":
        fixed = serve_https(port, run, out)
    if fixed:
        url, how = fixed.rstrip("/"), "public"
    if how != "local":
        server.set_public_url(data, url)
    users = server.Users(data)
    if not users.names():
        server.first_user(users, log=lambda *_: None)
    cmd = shlex.split(install.executable()) + ["server", "--host", host, "--port", str(port), "--data", str(data)]
    started = start_on_boot(cmd, run, popen, platform)
    if not started:
        out("- Couldn't set it to start on boot here (no systemd, launchd or crontab). Run it yourself:")
        out(f"    {shlex.join(cmd)}")
    else:
        out(f"- The hub starts on boot ({started}).")
    up = healthy(f"http://127.0.0.1:{port}", wait)
    if started and not up:
        out(f"- It didn't answer on port {port} yet; see {home() / 'server.log'}"
            + (" or `journalctl --user -u savetokens-server`." if started == "systemd" else "."))

    # 3. this machine joins its own hub
    if not (cfg.get("server_url") and cfg.get("server_token")):
        cfg.update(server_url=f"http://127.0.0.1:{port}", server_token=users.add("me"))
        if "share_names" not in cfg:
            cfg["share_names"] = interactive and install.ask_names(ask)
        save_config(cfg)
        out("- This machine sends its usage to the hub.")

    # 4. the line for every other machine
    code = users.pair("me", server.INVITE_SECONDS, server.INVITE_MACHINES)
    command = server.join_command(url, code)
    out("")
    out(f"Your hub: {url}")
    out(f"Add a machine: paste this on it (works for 24 hours, up to {server.INVITE_MACHINES} machines):")
    out(f"  {command}")
    out(f"See every machine in a browser: {url}/#code={code}")
    if how == "public":
        out(f"Every join command uses {url}; change it with `savetokens server --public-url URL`.")
    elif how == "tailscale":
        out("Machines reach it over Tailscale, so each needs Tailscale signed in to the same account.")
    elif how == "lan":
        out("Machines on this network can reach it. For machines elsewhere, install Tailscale (free) on each,"
            " or deploy a hub with HTTPS (README, 'Run a hub').")
        if wsl():
            out("This is WSL: other machines usually can't reach it. Install Tailscale inside WSL, or turn on"
                " WSL's mirrored networking.")
    else:
        out("No network address found: only this machine can reach the hub. Install Tailscale to add others.")
    out("More codes later: `savetokens pair`, or 'Add a machine' in the browser view. Stop: savetokens server --stop")
    return {"url": url, "how": how, "command": command, "code": code, "started": started, "up": up}
