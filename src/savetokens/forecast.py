"""Statusline segment: actual → expected at each limit window's end.

The windows, paths and scoring live in windows.py; this module only renders.
"""
from __future__ import annotations

import time

from .store import Store, load_config


def preferred_source(store: Store):
    connected = load_config().get("forecaster", "ephemeris") == "ephemeris"
    return "ephemeris" if connected and store.meta("ephemeris_hourly") else "baseline"


def segment(store: Store, payload: dict | None = None, now=None) -> str:
    """Compact: actual → expected at the window's end, for the 5-hour and weekly limits (or $ for API)."""
    from . import windows
    now = now or time.time()
    src = preferred_source(store)
    sid = (payload or {}).get("session_id")
    rows = {r["kind"]: r for r in windows.forecasts(store, now, sid, sources=(src,))}
    parts = []
    for kind, label in (("five_hour", "5h"), ("week", "wk")):
        r = rows.get(kind)
        pct = r and r["pct"]
        if not pct or pct["account"] is None:
            continue
        fc = pct["forecast"].get(src)
        text = f"{label} {pct['account']:.0f}%"
        if fc:
            text += f"→{fc[1]:.0f}%"
            if fc[2] >= 100 and kind in ("five_hour", "week"):
                text += " ⚠"
        parts.append(text)
    week = rows.get("week")
    if sid and week and week["pct"] and week["pct"]["session"]:
        parts.append(f"session {week['pct']['session']:.1f}% wk")
    day = rows.get("day")
    if day:
        api = day["units"]["api_usd"]
        if api["machine"] or (limits_billing(store) != "subscription"):
            fc = api["forecast"].get(src)
            parts.append(f"${api['machine']:.2f} today" + (f"→${fc[1]:.0f}" if fc else ""))
    if parts and limits_billing(store) == "subscription":
        from . import limits
        ls = limits.learning_state(store)
        if not ls["settled"]:
            parts.append(f"learning limits {ls['readings']}/{limits.SETTLE_READINGS}")
    from . import ephemeris, steer
    cfg = load_config()
    if cfg.get("forecaster", "ephemeris") == "ephemeris" and not ephemeris.api_key(cfg):
        parts.append("no Ephemeris key")
    from . import levers
    on = levers.applied(store).get("claude-code") or {}
    if on:
        parts.append("economising: " + ",".join(sorted(on)))
    mode = steer.configured_mode()
    if mode == "auto":
        parts.append(f"auto→{steer.guard_mode(store, {'mode': 'auto'})}")
    elif mode != "balanced":
        parts.append(mode)
    if payload:
        ctx = (payload.get("context_window") or {}).get("total_input_tokens")
        if ctx and ctx >= 150_000:
            parts.append(f"ctx {ctx // 1000}k")
    return " · ".join(parts)


def limits_billing(store: Store):
    from . import limits
    return limits.default_billing(store, "claude-code")
