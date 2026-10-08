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
    return {
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
                    "requests": r["n"], "subagents": (r["sub"] or 0) / r["usd"], "model": model[0] if model else None,
                    "first": r["first"], "last": r["last"], "running": now - r["last"] <= LIVE_SECONDS})
    rest = rows[limit:]
    if rest:
        out.append({"session": None, "project": f"{len(rest)} more", "share": sum(r["usd"] for r in rest) / total,
                    "pct_week": sum(r["usd"] for r in rest) * rate if rate else None, "running": False})
    return out


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
    room = max(12, width - 6)
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


def render(snap, width=80, color=True) -> list[str]:
    def c(code, text):
        return f"\033[{code}m{text}\033[0m" if color else text
    now = snap["now"]
    w = max(40, min(width, 120))
    made = snap.get("forecast_made_at")
    head = (f"savetokens · {time.strftime('%a %H:%M', time.localtime(now))} · forecast:"
            f" {snap['source'] or 'none yet'}" + (f", {alerts.span(now - made)} ago" if made else ""))
    if snap.get("synced_at"):
        head += f" · synced {alerts.span(now - snap['synced_at'])} ago"
    lines = [c("1", head), ""]
    if not snap["limits"]:
        lines.append("No limit readings yet: send a message in Claude Code with savetokens installed.")
    bw = w - 30
    for o in snap["limits"]:
        stage = o.get("stage")
        col = {"heads_up": "33", "act": "31", "last_call": "1;31"}.get(stage, "32")
        label = f"{o['label']:13}"
        lines.append(f"{c('1', label)} {c(col, bar(o['used'], o['p10'], o['p50'], o['p90'], bw))}"
                     f" {o['used']:3.0f}% now")
        detail = f"{'':13} resets {alerts.when(o['resets'], now)}"
        if o["p50"] is not None:
            detail += f" · likely {o['p50']:.0f}% ({o['p10']:.0f}–{o['p90']:.0f}%) · {o['p_hit']:.0%} chance of a hit"
        lines.append(c("2", detail))
        if o.get("eta"):
            lines.append(c(col, f"{'':13} ⚠ {STAGE_TEXT.get(stage, '')}: out around {alerts.when(o['eta'], now)},"
                                f" {alerts.span(o['resets'] - o['eta'])} before the reset"))
        lines.append("")
    d = snap["demand"]
    if any(d["past"]) or any(d["next"]):
        used = sum(d["past"])
        ahead = sum(d["next"])
        lines.append(c("1", "usage per hour") + c("2", f"  last {len(d['past'])}h ┊ next {len(d['next'])}h: ▒ likely ░ could reach"
                                                        f" · used {used:.1f}% of the week, ~{ahead:.1f}% to come"))
        lines += chart(d, w, color=color)
        lines.append("")
    if snap["models"]:
        lines.append(c("1", "this week by model"))
        for m in snap["models"][:4]:
            sub = f" ({m['subagents']:.0%} subagents)" if m["subagents"] >= 0.01 else ""
            lines.append(f"  {m['model'][:26]:26} {m['share']:4.0%}{sub}")
        lines.append("")
    if snap.get("sessions"):
        live = sum(1 for x in snap["sessions"] if x.get("running"))
        lines.append(c("1", "sessions, last 24h") + c("2", f"  {live} running (●), idle (○)"))
        lines.append(c("2", f"    {'project':16} {'session':8} {'share':>6} {'of week':>8} {'last hour':>10}  model"))
        for x in snap["sessions"]:
            dot = c("32", "●") if x.get("running") else c("2", "○")
            share = f"{x['share']:.0%}"
            week = f"{x['pct_week']:.1f}%" if x.get("pct_week") is not None else "–"
            if x["session"] is None:
                lines.append(c("2", f"  {dot} {x['project'][:16]:16} {'':8} {share:>6} {week:>8}"))
                continue
            pace = f"{x['pace']:.1f}%" if x.get("pace") else "–"
            model = (x.get("model") or "").replace("claude-", "")
            sub = f", {x['subagents']:.0%} subagents" if x.get("subagents", 0) >= 0.05 else ""
            lines.append(f"  {dot} {(x['project'] or '?')[:16]:16} {x['session']:8} {share:>6} {week:>8}"
                         f" {c('33', f'{pace:>10}') if x.get('running') and x.get('pace') else f'{pace:>10}'}"
                         f"  {c('2', model + sub)}")
        lines.append(c("2", "    share = of all usage in the last 24h · of week = % of your weekly limit"
                            " · last hour = % of the weekly limit used in the past hour"))
        lines.append("")
    if len(snap["machines"]) > 1 or len(snap["accounts"]) > 1:
        lines.append(c("1", "machines and accounts"))
        for m in snap["machines"][:4]:
            lines.append(f"  machine {m['machine']}: {m['requests']:,} requests this week,"
                         f" last {alerts.span(now - m['last_seen'])} ago")
        for a in snap["accounts"][:4]:
            pct = f"{a['weekly']:.0f}% of its week" if a["weekly"] is not None else "window closed"
            lines.append(f"  account {a['account'] or '?'}{' (active)' if a['active'] else ''}: {pct}")
        lines.append("")
    if snap["alerts"]:
        lines.append(c("1", "alerts"))
        for a in snap["alerts"][:3]:
            lines.append(f"  {time.strftime('%a %H:%M', time.localtime(a['ts']))}  {a['message']}"[:w])
        lines.append("")
    if snap["hits"]:
        what = lambda h: {"session": "5-hour", "weekly": "weekly"}.get(h["kind"]) or (h["model"] or "a").title()
        lines.append(c("1", "limits hit (30 days)") + "  " + ", ".join(
            f"{what(h)} {time.strftime('%d %b', time.localtime(h['ts']))}" for h in snap["hits"][:5]))
    if snap["track_record"]:
        best = sorted(snap["track_record"].items(), key=lambda kv: kv[1]["mae"])
        lines.append(c("2", "track record: " + ", ".join(
            f"{k} off by {v['mae']:.2f}/h over {v['forecasts']}" for k, v in best)))
    return lines
