"""Hourly upkeep outside Claude Code: a user crontab line, added and removed with consent.

The statusline and hooks already trigger upkeep while Claude Code runs; this keeps
scoring and forecasts current when it doesn't (and covers headless `claude -p` runs,
which have no statusline).
"""
from __future__ import annotations

import shutil
import subprocess

MARK = "# savetokens upkeep"


def available() -> bool:
    return shutil.which("crontab") is not None


def line(exe: str) -> str:
    return f"17 * * * * {exe} maintain --quiet >/dev/null 2>&1 {MARK}"


def _read(run=subprocess.run) -> str:
    r = run(["crontab", "-l"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _write(text: str, run=subprocess.run) -> bool:
    return run(["crontab", "-"], input=text, capture_output=True, text=True).returncode == 0


def installed(run=subprocess.run) -> bool:
    return available() and MARK in _read(run)


def add(exe: str, run=subprocess.run) -> bool:
    if not available():
        return False
    current = [l for l in _read(run).splitlines() if MARK not in l]
    return _write("\n".join(current + [line(exe)]) + "\n", run)


def remove(run=subprocess.run) -> bool:
    if not available():
        return False
    text = _read(run)
    if MARK not in text:
        return True
    kept = [l for l in text.splitlines() if MARK not in l]
    return _write(("\n".join(kept) + "\n") if kept else "", run)
