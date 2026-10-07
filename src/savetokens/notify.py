"""Short messages for channels without a statusline (Hermes cron delivery, Telegram).

`alerts()` returns only what is new since the last call, so a frequent cron job
stays silent unless something happened: guard alerts, or a limit or budget
window whose forecast now reaches 100%.
"""
from __future__ import annotations

import time
from datetime import datetime

from . import forecast, windows
from .store import Store


def alerts(store: Store, now=None) -> list[str]:
    now = now or time.time()
    last = store.meta("notify_last", now - 3600)
    out = [f"⚠ {a['message'].removeprefix('savetokens: ')} ({a['harness']}, session {a['session_id'][:8]})"
           for a in store.alerts(last) if a["ts"] > last]
    from . import guard
    from .store import load_config
    a = guard._surge_safe(store, now, load_config(), None)   # machine-wide, even with no tool calls (cron jobs)
    if a and store.add_alert("machine", "machine", a.rule, a.key, "warn", a.message, a.burn, ts=now):
        out.append(f"⚠ {a.message.removeprefix('savetokens: ')}")
    notified = set(store.meta("notified_windows", []))
    src = forecast.preferred_source(store)
    for r in windows.forecasts(store, now, sources=(src,)):
        p = r["pct"]
        if not p or not p["limit_window"]:
            continue
        fc = p["forecast"].get(src)
        key = f"{r['kind']}:{int(r['end'])}"
        if fc and fc[2] >= 100 and key not in notified:
            when = datetime.fromtimestamp(r["end"]).strftime("%a %H:%M")
            likely = "likely" if fc[1] >= 100 else "possible (p90)"
            out.append(f"⚠ {p['unit_of']}: {p['account'] or p['machine']:.0f}% used, reaching 100% is {likely}"
                       f" before it resets {when}.")
            notified.add(key)
    store.set_meta("notify_last", now)
    store.set_meta("notified_windows", sorted(notified)[-50:])
    return out


def daily(store: Store, now=None) -> str:
    """One compact summary: the status windows plus yesterday's biggest waste."""
    from . import report
    now = now or time.time()
    src = forecast.preferred_source(store)
    lines = ["savetokens daily"]
    names = {"hour": "this hour", "five_hour": "5-hour", "day": "today", "week": "this week"}
    for r in windows.forecasts(store, now, sources=(src,)):
        p = r["pct"]
        if p:
            fc = p["forecast"].get(src)
            now_pct = p["account"] if p["account"] is not None else p["machine"]
            lines.append(f"{names[r['kind']]}: {now_pct:.1f}% of {p['unit_of']}"
                         + (f" → {fc[1]:.0f}% expected ({fc[0]:.0f}–{fc[2]:.0f}%)" if fc else ""))
        api = r["units"]["api_usd"]
        if r["kind"] in ("day", "week") and api["machine"]:
            fc = api["forecast"].get(src)
            lines.append(f"{names[r['kind']]} API spend: ${api['machine']:,.2f}"
                         + (f" → ${fc[1]:,.0f} expected (${fc[0]:,.0f}–{fc[2]:,.0f})" if fc else ""))
    rep = report.build(store, days=1, now=now)
    top = rep["findings"][:2]
    if top:
        lines.append("biggest waste yesterday: " + "; ".join(
            f"{f.title} (~${f.usd:,.0f})" + (f", fix: savetokens fixes apply {f.fix}" if f.fix and f.fix != "context-guard"
                                              else "") for f in top))
    return "\n".join(lines)
