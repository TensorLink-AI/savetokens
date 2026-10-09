"""The dashboard: one snapshot of everything, drawn by `savetokens watch` and by the Claude Code pane.

snapshot()  a plain dict (JSON-safe): each limit's outlook and stage, hourly demand for the last
            day with the forecast for the next, the model mix, machines and accounts, recent alerts
            and hits, and the forecasters' track record. On the server it covers every machine;
            `savetokens dashboard --json` returns the server's when connected.
render()    the snapshot as terminal lines (ANSI colour optional), for `watch`.
"""
from __future__ import annotations

import time

from . import alerts, forecast, meter

BARS = " ▁▂▃▄▅▆▇█"
STAGE_TEXT = {"heads_up": "heads-up", "act": "act now", "last_call": "last call"}


def _hourly_forecast(store, account, source, now, hours):
    """Per coming hour: (p10, p50, p90) of demand, from the forecast paths."""
    p = forecast.load_paths(store, account, source)
    if not p:
        return []
    out = []
    for i in range(hours):
        t = meter.hour_floor(now) + i * meter.HOUR
        k = int((t - p["start"]) // meter.HOUR)
        if not 0 <= k < p["hours"]:
            break
        v = sorted(p["data"][j * p["hours"] + k] for j in range(p["n"]))
        out.append((v[len(v) // 10], v[len(v) // 2], v[(9 * len(v)) // 10]))
    return out


def snapshot(store, now=None, hours=24) -> dict:
    now = now or time.time()
    acct = meter.active_account(store, now)
    looks = forecast.outlook(store, now)
    for o in looks:
        o["stage"] = alerts.stage(o, now)
    hist, rate = meter.demand(store, now, hours=hours)
    source = looks[0]["source"] if looks else forecast.preferred_source(store, acct)
    week = now - 7 * 86400
    models = store.conn.execute(
        "SELECT model, SUM(cost_usd) AS usd, SUM(CASE WHEN subagent THEN cost_usd ELSE 0 END) AS sub"
        " FROM usage WHERE ts >= ? AND cost_usd > 0 GROUP BY model ORDER BY usd DESC", (week,)).fetchall()
    total = sum(r["usd"] for r in models) or 1.0
    machines = store.conn.execute(
        "SELECT machine, MAX(ts) AS last, SUM(cost_usd) AS usd, COUNT(*) AS n FROM usage WHERE ts >= ?"
        " GROUP BY machine ORDER BY last DESC", (week,)).fetchall()
    accounts = []
    known = [a for a in meter.accounts(store) if a]
    for a in known or meter.accounts(store):   # readings from before accounts were recorded have none
        r = meter.latest(store, "seven_day", now, a)
        last = store.conn.execute("SELECT MAX(ts) FROM meter WHERE account IS ?", (a,)).fetchone()[0]
        if r or (last and last >= week):
            accounts.append({"account": a, "weekly": r["pct"] if r else None, "resets": r["resets"] if r else None,
                             "last_seen": last, "active": a == acct})
    recent = [dict(r) for r in store.conn.execute(
        "SELECT ts, name, stage, message FROM alerts WHERE ts >= ? ORDER BY ts DESC LIMIT 5", (week,))]
    hits = []
    for h in store.conn.execute("SELECT ts, kind, model FROM hits WHERE ts >= ? ORDER BY ts DESC LIMIT 100",
                                (now - 30 * 86400,)):
        if not hits or (h["kind"], h["model"]) != (hits[-1]["kind"], hits[-1]["model"]) \
                or hits[-1]["ts"] - h["ts"] > 3600:
            hits.append(dict(h))
    tr = {k: v for k, v in (store.meta("track_record") or {}).items() if k != "at"}
    sessions = top_sessions(store, now, rate)
    snap = {
        "now": now, "account": acct, "source": source, "forecast_made_at": store.meta("forecast_made_at"),
        "synced_at": store.meta("synced_at"), "limits": looks,
        "demand": demand_view(hist, _hourly_forecast(store, acct, source, now, hours), now, hours),
        "usd_per_pct": 1 / rate if rate else None,
        "models": [{"model": r["model"], "share": r["usd"] / total, "subagents": (r["sub"] or 0) / r["usd"]}
                   for r in models],
        "machines": [{"machine": r["machine"], "last_seen": r["last"], "usd": r["usd"], "requests": r["n"]}
                     for r in machines],
        "sessions": sessions, "accounts": accounts, "alerts": recent, "hits": hits[:6], "track_record": tr,
    }
    level, text = headline(snap)
    snap["headline"] = {"level": level, "text": text}
    return snap


def demand_view(hist, fc, now, hours):
    """The last `hours` complete hours (zeros where nothing was used) and the forecast from this hour on."""
    first = meter.hour_floor(now) - hours * meter.HOUR
    got = dict(hist)
    return {"start": first, "now_hour": meter.hour_floor(now),
            "past": [round(got.get(first + i * meter.HOUR, 0.0), 3) for i in range(hours)],
            "next": [round(m, 3) for _, m, _ in fc], "next_lo": [round(lo, 3) for lo, _, _ in fc],
            "next_hi": [round(hi, 3) for _, _, hi in fc]}


LIVE_SECONDS = 600   # a session with a request in the last 10 minutes counts as running


def top_sessions(store, now, rate, since_hours=24, limit=8):
    """The sessions that used the most in the last day: project, share, % of the weekly limit, pace now."""
    since = now - since_hours * 3600
    rows = store.conn.execute(
        "SELECT session_id, MAX(project) AS project, MAX(machine) AS machine, SUM(cost_usd) AS usd, COUNT(*) AS n,"
        " SUM(CASE WHEN subagent THEN cost_usd ELSE 0 END) AS sub, MIN(ts) AS first, MAX(ts) AS last,"
        " SUM(CASE WHEN ts >= ? THEN cost_usd ELSE 0 END) AS hour_usd"
        " FROM usage WHERE ts >= ? AND cost_usd > 0 GROUP BY session_id ORDER BY usd DESC",
        (now - 3600, since)).fetchall()
    total = sum(r["usd"] for r in rows) or 1.0
    out = []
    for r in rows[:limit]:
        model = store.conn.execute("SELECT model, project FROM usage WHERE session_id = ? AND NOT subagent"
                                   " ORDER BY ts DESC LIMIT 1", (r["session_id"],)).fetchone()
        out.append({"session": r["session_id"][:8], "project": (model[1] if model and model[1] else r["project"]), "machine": r["machine"],
                    "share": r["usd"] / total, "pct_week": r["usd"] * rate if rate else None,
                    "pace": r["hour_usd"] * rate if rate else None,   # % of the weekly limit in the last hour
                    "hour_usd": r["hour_usd"] or 0.0, "requests": r["n"], "subagents": (r["sub"] or 0) / r["usd"], "model": model[0] if model else None,
                    "first": r["first"], "last": r["last"], "running": now - r["last"] <= LIVE_SECONDS})
    what_if(store, now, out, sum(r["hour_usd"] or 0 for r in rows))
    rest = rows[limit:]
    if rest:
        out.append({"session": None, "project": f"{len(rest)} more", "share": sum(r["usd"] for r in rest) / total,
                    "pct_week": sum(r["usd"] for r in rest) * rate if rate else None, "running": False})
    return out


WHAT_IF_HOURS = 5   # a session is assumed to carry on at its pace for at most this long


def what_if(store, now, sessions, hour_usd_total):
    """For each running session: what stopping it now would change, by the limit nearest to running out.

    A session's part of the coming demand is its share of the last hour's usage, for the next
    WHAT_IF_HOURS (sessions don't run for days). Adds `if_stopped`: {limit, adds (points it would add
    in those hours), eta (run-out time now), eta_if_stopped (None: no longer runs out before the reset)}.
    """
    if hour_usd_total <= 0:
        return
    base = forecast.outlook(store, now)
    looks = [o for o in base if o["p50"] is not None]
    if not looks:
        return
    # the limit that matters: the one you'd run out of first, else the one closest to full by reset
    target = min(looks, key=lambda o: (o["eta"] or float("inf"), -(o["p50"] or 0)))
    for x in sessions:
        if not x.get("running") or not x.get("hour_usd"):
            continue
        part = min(1.0, x["hour_usd"] / hour_usd_total)
        o = {y["name"]: y for y in forecast.outlook(store, now, cut=1 - part, cut_hours=WHAT_IF_HOURS)}.get(
            target["name"])
        if not o or o["p50"] is None:
            continue
        x["if_stopped"] = {"limit": target["name"], "adds": target["p50"] - o["p50"], "eta": target["eta"],
                           "hours": WHAT_IF_HOURS,
                           "eta_if_stopped": o["eta"] if (o["p_hit"] or 0) >= 0.5 else None,
                           "resets": target["resets"]}


def stopping(x, now) -> str:
    """'out Fri 01:10 → after reset' when you'd run out; else 'saves 2.1% of weekly'."""
    w = x.get("if_stopped")
    if not x.get("running"):
        return "not running"
    if not w:
        return "no forecast yet"
    if w["eta"]:
        after = alerts.when(w["eta_if_stopped"], now) if w["eta_if_stopped"] else "after reset"
        return f"out {alerts.when(w['eta'], now)} → {after}"
    label = "5-hour" if w["limit"] == "five_hour" else "weekly"
    return f"saves {w['adds']:.1f}% of {label}"


# ── drawing ──────────────────────────────────────────────────────────────────

def nice_top(v) -> float:
    """A round axis maximum at or above v: 1, 2, 2.5, 5 x 10^k."""
    import math
    if v <= 0:
        return 1.0
    k = 10 ** math.floor(math.log10(v))
    return next(m * k for m in (1, 2, 2.5, 5, 10) if m * k >= v)


def chart(d, width=80, height=6, color=True) -> list[str]:
    """Hourly usage as vertical bars in a plain frame: past solid; from the ┊ on, the forecast as one
    column, ▒ up to the likely value and ░ on to the high end. A narrow screen shows fewer hours rather than cutting the frame."""
    def c(code, text):
        return f"\033[{code}m{text}\033[0m" if color and text.strip() else text
    past, nxt, hi = d["past"], d["next"], d.get("next_hi") or d["next"]
    room = max(12, width - 4)
    if len(past) + len(nxt) > room:
        keep_next = min(len(nxt), room // 2)
        past, nxt, hi = past[-(room - keep_next):], nxt[:keep_next], hi[:keep_next]
    hours = len(past) + len(nxt)
    if not hours:
        return []
    cell = 2 if 2 * hours <= room else 1
    top = nice_top(max(past + hi + [0.01]))
    inner = hours * cell + (1 if nxt else 0)
    rows = ["  ┌" + "─" * inner + "┐"]
    for r in range(height - 1, -1, -1):
        line = "  │"
        for i in range(hours):
            fut = i >= len(past)
            if fut:   # one joined column of whole cells: ▒ up to the likely value, ░ on to the high end
                likely = nxt[i - len(past)] / top * height
                high = max(likely, hi[i - len(past)] / top * height)
                ch = ("▒" if r < round(likely) else "░" if r < round(high)
                      else "▁" if r == 0 and likely > 0 else " ")
            else:
                level = past[i] / top * height
                ch = "█" if level >= r + 1 else BARS[max(1, int((level - r) * 8))] if level > r else " "
            if fut and i == len(past):
                line += c("2", "┊")
            line += c("36" if fut else "", ch * cell)
        rows.append(line + "│")
    rows.append("  └" + "─" * inner + "┘")
    return rows


def spark(values, top=None) -> str:
    top = top or max(values or [0]) or 1.0
    return "".join(BARS[min(8, int(round(8 * v / top)))] if v > 0 else BARS[0] for v in values)


def bar(used, p10, p50, p90, width=40) -> str:
    """Filled to % used now, ▒ to the likely % at reset, ░ to the high end; | marks 100%."""
    cells = []
    for i in range(width):
        x = 100.0 * (i + 0.5) / width
        if x <= used:
            cells.append("█")
        elif p50 is not None and x <= p50:
            cells.append("▒")
        elif p90 is not None and x <= p90:
            cells.append("░")
        else:
            cells.append("·")
    return "".join(cells)


def headline(snap):
    """(level, text): the one line that says whether you're fine, for the top of every view."""
    now = snap["now"]
    looks = [o for o in snap["limits"] if o.get("p50") is not None]
    urgent = sorted((o for o in looks if o.get("eta")), key=lambda o: o["eta"])
    if urgent:
        o = urgent[0]
        name = "5-hour" if o["name"] == "five_hour" else "weekly"
        text = (f"At this pace you'll run out of your {name} limit around {alerts.when(o['eta'], now)},"
                f" {alerts.span(o['resets'] - o['eta'])} before it resets.")
        best = [x for x in snap.get("sessions") or [] if (x.get("if_stopped") or {}).get("eta")]
        if best:
            x = max(best, key=lambda x: x["if_stopped"]["eta_if_stopped"] or float("inf"))
            later = x["if_stopped"]["eta_if_stopped"]
            gain = "would get you to the reset" if later is None else f"buys about {alerts.span(later - o['eta'])}"
            if later is None or later - o["eta"] >= 1800:
                text += f" Pausing {x.get('project') or x['session']} {gain}."
        return ("bad" if o.get("stage") in ("act", "last_call") else "warn"), text
    week = next((o for o in looks if o["name"] == "seven_day"), None)
    if week:
        return "ok", (f"On track: weekly limit likely {week['p50']:.0f}% by {alerts.when(week['resets'], now)}"
                      f" ({week['p_hit']:.0%} chance of running out first).")
    if snap["limits"]:
        return "ok", "Forecast on its way: the first one arrives within the hour."
    return "none", "No limit readings yet: send a message in Claude Code with savetokens installed."


def render(snap, width=80, color=True) -> list[str]:
    """The dashboard as terminal lines: the answer first, then the detail behind it."""
    def c(code, text):
        return f"\033[{code}m{text}\033[0m" if color and text else text

    def title(text, note=""):
        return c("1", text) + (c("2", "  " + note) if note else "")
    now = snap["now"]
    w = max(50, min(width, 120))
    made = snap.get("forecast_made_at")
    src = {"ephemeris": "Ephemeris", "baseline": "local baseline"}.get(snap.get("source"), "no forecast yet")
    right = f"{time.strftime('%a %H:%M', time.localtime(now))} · {src}" + (
        f", {alerts.span(now - made)} ago" if made else "") + (" · synced" if snap.get("synced_at") else "")
    lines = [c("1", "savetokens") + " " * max(1, w - 10 - len(right)) + c("2", right), ""]
    level, text = headline(snap)
    mark = {"bad": ("1;31", "⚠"), "warn": ("33", "⚠"), "ok": ("32", "✓"), "none": ("2", "·")}[level]
    import textwrap
    for i, part in enumerate(textwrap.wrap(text, w - 2) or [""]):
        lines.append(c(mark[0], f"{mark[1] if i == 0 else ' '} {part}"))
    lines.append("")

    # limits: one row each, the bar then the numbers that matter
    if snap["limits"]:
        bw = max(12, min(40, w - 52))
        lines.append(title("LIMITS") + " " * (2 + 7 + 1 + bw - 6) + c("2", f"{'now':>5}   {'at reset (likely, range)':24} resets"))
        for o in snap["limits"]:
            col = {"heads_up": "33", "act": "31", "last_call": "1;31"}.get(o.get("stage"), "32")
            name = "5-hour" if o["name"] == "five_hour" else "weekly"
            at = f"{o['p50']:4.0f}%  ({o['p10']:.0f}–{o['p90']:.0f}%)" if o["p50"] is not None else "–"
            lines.append(f"  {name:7} {c(col, bar(o['used'], o['p10'], o['p50'], o['p90'], bw))} {o['used']:4.0f}%"
                         f"   {at:24} {alerts.when(o['resets'], now)}")
        lines.append(c("2", f"  {'':7} █ used  ▒ likely by reset  ░ could reach  · room left"))
        lines.append("")

    d = snap["demand"]
    if any(d["past"]) or any(d["next"]):
        lines.append(title("USAGE PER HOUR", f"last {len(d['past'])}h ┊ next {len(d['next'])}h · used"
                                             f" {sum(d['past']):.1f}% of the week, ~{sum(d['next']):.1f}% to come"))
        lines += chart(d, w, color=color)
        lines.append(c("2", f"  {'':2}█ used   ▒ likely   ░ could reach"))
        lines.append("")

    sessions = snap.get("sessions") or []
    if sessions:
        live = sum(1 for x in sessions if x.get("running"))
        lines.append(title("SESSIONS", f"last 24h · {live} running"))
        lines.append(c("2", f"    {'project':18} {'today':>6}  {'last hour':>9}   if you pause it (next 5h)"))
        for x in sessions:
            dot = c("32", "●") if x.get("running") else c("2", "○")
            today = f"{x['pct_week']:.1f}%" if x.get("pct_week") is not None else "–"
            if x["session"] is None:
                lines.append(c("2", f"  {dot} {x['project'][:18]:18} {today:>6}"))
                continue
            pace = f"{x['pace']:.1f}%" if x.get("running") and x.get("pace") else "–"
            pause = stopping(x, now)
            lines.append(f"  {dot} {(x['project'] or x['session'])[:18]:18} {today:>6}  {pace:>9}   "
                         + (c("2", pause) if not x.get("running") else pause))
        lines.append(c("2", "    today and last hour are % of your weekly limit"))
        lines.append("")

    if snap["models"]:
        mix = " · ".join(f"{m['model'].replace('claude-', '')} {m['share']:.0%}"
                         + (f" ({m['subagents']:.0%} via subagents)" if m["subagents"] >= 0.05 else "")
                         for m in snap["models"][:3])
        lines.append(title("MODELS") + "  " + mix + c("2", "  · this week"))
    if len(snap["machines"]) > 1 or len(snap["accounts"]) > 1:
        accts = ", ".join(f"{a['account'] or '?'}{' (in use)' if a['active'] else ''}" for a in snap["accounts"][:4])
        lines.append(title("MACHINES") + f"  {len(snap['machines'])} this week · accounts: {accts}")
    if snap["alerts"]:
        import textwrap
        a = snap["alerts"][0]
        when = time.strftime("%a %H:%M", time.localtime(a["ts"]))
        for i, part in enumerate(textwrap.wrap(a["message"], w - 26)):
            lines.append((title("LATEST ALERT") + "  " + c("2", when) if i == 0 else " " * 25) + "  " + part)
    if snap["hits"]:
        what = lambda h: {"session": "5-hour", "weekly": "weekly"}.get(h["kind"]) or (h["model"] or "a").title()
        lines.append(title("LIMITS HIT") + "  " + ", ".join(
            f"{what(h)} {time.strftime('%d %b', time.localtime(h['ts']))}" for h in snap["hits"][:5])
            + c("2", "  · last 30 days"))
    if snap.get("track_record"):
        best = sorted(snap["track_record"].items(), key=lambda kv: kv[1]["mae"])
        lines.append(c("2", "forecast accuracy: " + ", ".join(
            f"{k} off by {v['mae']:.2f} points/h over {v['forecasts']} forecasts" for k, v in best)))
    return lines
