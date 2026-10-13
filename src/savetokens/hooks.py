"""Claude Code entry points: the statusline and hooks. Both must be fast and must never break a session.

statusline  records Claude's limit readings, reads the session's new transcript lines,
            and shows each limit: % used now → projected at reset, or when you'd run out
hooks       read transcripts as sessions run and end; on your next prompt, show any
            new pace alert (to you; and to the agent too, with a pacing note, if you turned
            that on: `agent_context`), and at session start when a limit is at risk
budget agent  per budget (`savetokens budget --mode`): advise tells the agent when a budget its session counts
            toward is at risk; guard blocks new prompts once that budget is used up
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


NOTE_MAX_AGE = 3 * 3600


def note(store, now=None):
    """The agent's note, when telling the agent is on and the note is recent."""
    now = now or time.time()
    if not load_config().get("agent_context"):
        return None
    n = store.meta("agent_note") or {}
    return n.get("text") if now - n.get("at", 0) <= NOTE_MAX_AGE else None


def session(store, payload, who=None) -> dict:
    """What a session counts toward: its tool, provider, project id and billing."""
    from . import projects
    pid = projects.resolve(store, payload.get("cwd"))[0] if payload.get("cwd") else None
    return {"tool": capture.HARNESS, "provider": who or "anthropic", "project": pid,
            "billing": "api" if who or capture.billing(store) == "api" else "plan"}


def guard(store, here, now=None) -> str | None:
    """Why this prompt is stopped: a guarded budget it counts toward is used up. None: go ahead."""
    from . import pools
    for gid, g, used in pools.used_up(store, now, "guard"):
        if pools.covers(g, **here):
            return (f"savetokens: the {g['label']} budget is used up ({used:.0f}%) and set to guard, so new work"
                    f" stops here until its period ends. To go on: raise it, or switch it to advise"
                    f" (dashboard, or `savetokens budget --name {gid} --mode advise`).")
    return None


ADVISE_EVERY = 3600


def advise(store, here, session_id, now=None) -> str | None:
    """A pacing note for the agent, at most hourly per session, when a budget in advise (or guard) that this
    session counts toward is at risk."""
    from . import pools
    now = now or time.time()
    wanted = {gid for gid, g in pools.goals_(store).items() if pools.mode(g) in ("advise", "guard")
              and pools.covers(g, **here)}
    if not wanted:
        return None
    told = store.meta("advised") or {}
    if now - told.get(session_id or "", 0) < ADVISE_EVERY:
        return None
    risky = [o for o in forecast.outlook(store, now, goals=True) if o["kind"] == "goal"
             and o["pool"][5:] in wanted and (alerts.stage(o, now) or o["used"] >= 100)]
    if not risky:
        return None
    told[session_id or ""] = now
    store.set_meta("advised", dict(list(told.items())[-200:]))
    return ("savetokens (budget agent): " + " ".join(alerts.message(o, alerts.stage(o, now), now) for o in risky)
            + " Pace yourself: use cheaper models for subagents and exploration, avoid wide fan-outs and re-reading"
              " large files, and at a task boundary propose starting fresh. Propose bigger changes (a cheaper model"
              " or provider for the main work) to the user; don't make them yourself.")


def handle(event: str, payload: dict, store: Store):
    """Hook output as a dict (printed as JSON), or None."""
    acct = _account(store)
    who = capture.note_endpoint(store, payload.get("session_id"))   # hooks run in Claude Code's environment
    capture.ingest_session(store, payload.get("transcript_path"), acct)
    if event == "UserPromptSubmit":
        here = session(store, payload, who)
        why = guard(store, here)
        if why:
            return {"decision": "block", "reason": why}
        told = advise(store, here, payload.get("session_id"))
        if told:
            shown = alerts.unseen(store)
            return {"systemMessage": " ".join(shown) or "savetokens: a budget is at risk; the agent was told.",
                    "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": told}}
    if event == "SessionStart":
        text = note(store)
        if text:
            return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    if event == "UserPromptSubmit":
        shown = alerts.unseen(store)
        if shown:
            out = {"systemMessage": " ".join(shown)}
            text = note(store)
            if text:   # the agent hears it too, so it can pace itself
                out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": " ".join(shown) + " " + text}
            return out
    if event in ("Stop", "SessionEnd"):   # headless runs (claude -p) have no statusline to drive upkeep
        maintain.kick(store)
    return None
