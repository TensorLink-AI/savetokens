"""Pace alerts: when, at this pace, you'll run out, in three stages per limit window.

  heads_up   more likely than not to reach the limit before it resets
  act        80% likely, or under an hour to go at this pace
  last_call  under 10 minutes to go, or 95% used

A stage fires once per window and only climbs. The weekly projection is also
watched: a jump well above its recent average, or a burst of swings, is flagged
before any stage is reached (an intense session starting shows up there first).
"""
from __future__ import annotations

import shutil
import statistics
import subprocess
import time
from datetime import datetime

from . import forecast

STAGES = ("heads_up", "act", "last_call")
HEADS_UP, ACT = 0.5, 0.8
ACT_SECONDS, LAST_CALL_SECONDS = 3600, 600
LAST_CALL_PCT = 95.0
JUMP_SIGMAS, JUMP_MIN_PCT, MA_HOURS = 2.0, 5.0, 24
VOL_HOURS, VOL_RATIO, MIN_POINTS = 6, 2.0, 6


def when(ts, now=None):
    """'14:20' today, else 'Tue 14:20'."""
    now = now or time.time()
    d = datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if d.date() == datetime.fromtimestamp(now).date() else d.strftime("%a %H:%M")


def span(seconds):
    seconds = max(0, seconds)
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 2 * 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def stage(o, now):
    """The stage an outlook has reached, or None."""
    if o["used"] >= 100:
        return None
    left = o["eta"] - now if o.get("eta") else None
    if o["used"] >= LAST_CALL_PCT or (left is not None and left <= LAST_CALL_SECONDS):
        return "last_call"
    if (o["p_hit"] or 0) >= ACT or (left is not None and left <= ACT_SECONDS):
        return "act"
    if (o["p_hit"] or 0) >= HEADS_UP:
        return "heads_up"
    return None


def message(o, st, now):
    head = f"{o['label']}: {o['used']:.0f}% used"
    if o.get("eta"):
        pace = (f"at this pace you'll reach it around {when(o['eta'], now)} (in {span(o['eta'] - now)}),"
                f" {span(o['resets'] - o['eta'])} before it resets at {when(o['resets'], now)}")
    else:
        pace = f"likely {o['p50']:.0f}% by the reset at {when(o['resets'], now)} (up to {o['p90']:.0f}%)"
    text = f"{head}; {pace}."
    if st == "last_call":
        text += " Wrap up the current step and note where you are, so you can pick it up after the reset."
    return text


def best_pause(store, now, o) -> str:
    """' Pausing synth would ...': the running session whose pause moves the run-out time furthest."""
    from . import dashboard, meter
    try:
        rate = meter.rate(store)
        sessions = [x for x in dashboard.top_sessions(store, now, rate)
                    if (x.get("if_stopped") or {}).get("limit") == o["name"] and x["if_stopped"]["eta"]]
    except Exception:
        return ""
    if not sessions:
        return ""
    later = lambda x: x["if_stopped"]["eta_if_stopped"] or float("inf")
    x = max(sessions, key=later)
    name = x.get("project") or f"session {x['session']}"
    if x["if_stopped"]["eta_if_stopped"] is None:
        return f" Pausing {name} for a few hours would likely get you to the reset."
    gain = x["if_stopped"]["eta_if_stopped"] - o["eta"]
    return f" Pausing {name} would buy about {span(gain)}." if gain >= 1800 else ""


def projection_flags(store, now, account, name="seven_day"):
    """Jump and volatility flags on the recorded projection for the open window."""
    o = {x["name"]: x for x in forecast.outlook(store, now)}.get(name)
    if not o or not o["source"]:
        return []
    rows = [tuple(r) for r in store.conn.execute(
        "SELECT made_at, p50 FROM outlook WHERE account = ? AND name = ? AND source = ? AND window_end = ?"
        " AND made_at <= ? AND p50 IS NOT NULL ORDER BY made_at", (account or "", name, o["source"], o["resets"], now))]
    if len(rows) < MIN_POINTS:
        return []
    ts, vals = [r[0] for r in rows], [r[1] for r in rows]
    diffs = [(t, b - a) for t, a, b in zip(ts[1:], vals, vals[1:])]
    out = []
    latest = vals[-1]
    prior = [v for t, v in zip(ts[:-1], vals[:-1]) if t >= now - MA_HOURS * 3600]
    if prior:
        ma = statistics.fmean(prior)
        move = statistics.pstdev([d for _, d in diffs[:-1]]) if len(diffs) > 2 else 0.0
        if latest - ma > max(JUMP_SIGMAS * move, JUMP_MIN_PCT):
            out.append({"name": name, "window_end": o["resets"], "stage": f"jump:{int(latest // 10)}",
                        "message": f"{o['label']}: the projection jumped to {latest:.0f}% by reset, from a recent"
                                   f" average of {ma:.0f}%. Usage just sped up."})
    recent = [d for t, d in diffs if t >= now - VOL_HOURS * 3600]
    older = [d for t, d in diffs if t < now - VOL_HOURS * 3600]
    if len(recent) >= 3 and len(older) >= MIN_POINTS:
        a = statistics.median(abs(d) for d in recent)
        b = max(statistics.median(abs(d) for d in older), 1e-9)
        if a > VOL_RATIO * b and a > 0.5:
            out.append({"name": name, "window_end": o["resets"], "stage": f"swings:{int(now // 86400)}",
                        "message": f"{o['label']}: usage is erratic; the projection is swinging {a / b:.1f}x more"
                                   f" than usual (now {latest:.0f}% by reset, likely {o['p10']:.0f}–{o['p90']:.0f}%)."})
    return out


def check(store, now=None) -> list[dict]:
    """Raise any new alerts; returns them."""
    now = now or time.time()
    found = []
    looks = forecast.outlook(store, now)
    acct = looks[0]["account"] if looks else None
    for o in looks:
        st = stage(o, now)
        if not st:
            continue
        fired = {r[0] for r in store.conn.execute(
            "SELECT stage FROM alerts WHERE account = ? AND name = ? AND window_end = ?",
            (acct or "", o["name"], o["resets"]))}
        if any(STAGES.index(f) >= STAGES.index(st) for f in fired if f in STAGES):
            continue
        text = message(o, st, now)
        if st in ("act", "last_call"):
            text += best_pause(store, now, o)
        found.append({"name": o["name"], "window_end": o["resets"], "stage": st, "message": text})
    if looks:
        found += projection_flags(store, now, acct)
    new = []
    for a in found:
        cur = store.conn.execute("INSERT OR IGNORE INTO alerts (account, ts, name, window_end, stage, message)"
                                 " VALUES (?,?,?,?,?,?)", (acct or "", now, a["name"], a["window_end"], a["stage"],
                                                           a["message"]))
        if cur.rowcount:
            new.append(a)
    store.conn.commit()
    return new


def unseen(store, now=None):
    """Alerts of the last 6 hours not yet shown in a session (for the next prompt), oldest first; marks them seen."""
    since = (now or time.time()) - 6 * 3600
    rows = list(store.conn.execute("SELECT rowid, message FROM alerts WHERE seen = 0 AND ts >= ? ORDER BY ts",
                                   (since,)))
    store.conn.executemany("UPDATE alerts SET seen = 1 WHERE rowid = ?", [(r[0],) for r in rows])
    store.conn.commit()
    return [r[1] for r in rows]


def desktop(messages, run=subprocess.run):
    """Best effort: notify-send on Linux, osascript on macOS. Silent when neither exists."""
    for m in messages:
        if shutil.which("notify-send"):
            run(["notify-send", "savetokens", m], capture_output=True)
        elif shutil.which("osascript"):
            run(["osascript", "-e", f'display notification "{m.replace(chr(34), chr(39))}" with title "savetokens"'],
                capture_output=True)
