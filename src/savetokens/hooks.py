"""Claude Code entry points: the statusline and hooks. Both must be fast and must never break a session.

statusline  records Claude's limit readings, reads the session's new transcript lines,
            and shows each limit: % used now → projected at reset, or when you'd run out
hooks       read transcripts as sessions run and end; on your next prompt, show any
            new pace alert (to you, not to the agent)
"""
from __future__ import annotations

import subprocess
import time

from . import alerts, capture, forecast, maintain
from .store import Store, load_config

SHORT = {"five_hour": "5h", "seven_day": "wk", "budget": "$"}


def _account(store):
    """The signed-in account, cached for a minute (the statusline runs often)."""
    cached = store.meta("account_cache") or {}
    if time.time() - cached.get("at", 0) > 60:
        cached = {"at": time.time(), **capture.account()}
        store.set_meta("account_cache", cached)
    return cached.get("account")


def segment(store, now=None) -> str:
    now = now or time.time()
    parts = []
    for o in forecast.outlook(store, now):
        if o["harness"] != capture.HARNESS:   # Claude Code's statusline shows Claude Code's limits
            continue
        text = f"{SHORT[o['name']]} {o['used']:.0f}%"
        st = alerts.stage(o, now)
        if st and o.get("eta"):
            text += f" ⚠ out ~{alerts.when(o['eta'], now)}"
        elif st:
            text += f" ⚠→{o['p50']:.0f}%"
        elif o["p50"] is not None:
            text += f"→{o['p50']:.0f}%"
        parts.append(text)
    return " · ".join(parts)


def statusline(payload: dict, raw: str, store: Store) -> str:
    """Our segment, after the user's own statusline command if they had one."""
    acct = _account(store)
    capture.record_statusline(store, payload, acct)
    capture.ingest_session(store, payload.get("transcript_path"), acct)
    ours = segment(store)
    try:
        maintain.kick(store)
    except Exception:
        pass
    wrapped = load_config().get("statusline_wrapped")
    if wrapped:
        try:
            theirs = subprocess.run(wrapped, shell=True, input=raw, capture_output=True, text=True,
                                    timeout=1.5).stdout.rstrip("\n")
        except (subprocess.SubprocessError, OSError):
            theirs = ""
        if theirs:
            return f"{theirs} │ {ours}" if ours else theirs
    return ours


def handle(event: str, payload: dict, store: Store):
    """Hook output as a dict (printed as JSON), or None."""
    acct = _account(store)
    capture.ingest_session(store, payload.get("transcript_path"), acct)
    if event == "UserPromptSubmit":
        shown = alerts.unseen(store)
        if shown:
            return {"systemMessage": " ".join(shown)}
    if event in ("Stop", "SessionEnd"):   # headless runs (claude -p) have no statusline to drive upkeep
        maintain.kick(store)
    return None
