"""The dashboard: one snapshot of everything, drawn by `savetokens watch` and by the Claude Code pane.

snapshot()  a plain dict (JSON-safe): each limit's outlook and stage, hourly demand for the last
            day with the forecast for the next, the model mix, machines and accounts, recent alerts
            and hits, and the forecasters' track record. On the server it covers every machine;
            `savetokens dashboard --json` returns the server's when connected.
render()    the snapshot as terminal lines (ANSI colour optional), for `watch`.
"""
from __future__ import annotations

import time

from . import alerts, forecast, meter, pools

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


def focus(looks, all_pools):
    """The pool to chart: the one whose limit runs out first, else the one most likely to, else the first."""
    looks = [o for o in looks if o["p50"] is not None]
    if looks:
        o = min(looks, key=lambda o: (o["eta"] or float("inf"), -(o["p_hit"] or 0), -(o["p50"] or 0)))
        if o["eta"] or (o["p_hit"] or 0) >= 0.2:
            return next(p for p in all_pools if p.id == o["pool"])
    return all_pools[0] if all_pools else None


def snapshot(store, now=None, hours=24, on_server=False) -> dict:
    now = now or time.time()
    acct = meter.active_account(store, now)
    looks = forecast.outlook(store, now)
    for o in looks:
        o["stage"] = alerts.stage(o, now)
    every = pools.pools(store, now)
    pool = focus(looks, every)
    hist = pools.history(store, pool, now, hours=hours)[0] if pool else []
    rate = meter.rate(store)
    source = looks[0]["source"] if looks else forecast.preferred_source(store, acct)
    week = now - 7 * 86400
    models = store.conn.execute(
        f"SELECT harness, model, SUM({meter.WEIGHT}) AS w, SUM(CASE WHEN subagent THEN {meter.WEIGHT} ELSE 0 END)"
        f" AS sub FROM usage WHERE ts >= ? GROUP BY harness, model HAVING w > 0 ORDER BY w DESC", (week,)).fetchall()
    total = {}
    for r in models:
        total[r["harness"]] = total.get(r["harness"], 0.0) + r["w"]
    machines = store.conn.execute(
        "SELECT machine, MAX(ts) AS last, SUM(cost_usd) AS usd, COUNT(*) AS n FROM usage WHERE ts >= ?"
        " GROUP BY machine ORDER BY last DESC", (week,)).fetchall()
    accounts = []
    known = [a for a in meter.accounts(store) if a]
    for a in known or meter.accounts(store):   # readings from before accounts were recorded have none
        r = meter.latest(store, "seven_day", now, a)
        last = store.conn.execute("SELECT MAX(ts) FROM meter WHERE harness = ? AND account IS ?",
                                  (meter.CLAUDE, a)).fetchone()[0]
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
    sessions = top_sessions(store, now)
    snap = {
        "now": now, "account": acct, "source": source, "forecast_made_at": store.meta("forecast_made_at"),
        "synced_at": store.meta("synced_at"), "limits": looks,
        "pools": [{"id": p.id, "tool": p.tool, "kind": p.kind, "budget": p.budget} for p in every],
        "demand": {**demand_view(hist, _hourly_forecast(store, pool.key, forecast.preferred_source(store, pool.key),
                                                          now, hours) if pool else [], now, hours),
                   "pool": pool.id if pool else None, "unit": "$" if pool and pool.kind == "api" else "%",
                   "label": (f"{pool.tool} API $" if pool.kind == "api" else f"{pool.tool} % of weekly limit")
                   if pool else None},
        "usd_per_pct": 1 / rate if rate else None,
        "models": [{"model": r["model"], "harness": r["harness"], "share": r["w"] / total[r["harness"]],
                    "subagents": (r["sub"] or 0) / r["w"]} for r in models],
        "machines": [{"machine": r["machine"], "last_seen": r["last"], "usd": r["usd"], "requests": r["n"]}
                     for r in machines],
        "sessions": sessions, "accounts": accounts, "alerts": recent, "hits": hits[:6], "track_record": tr,
        "unpriced": {h: m for h in pools.TOOLS for m in [pools.unpriced(store, h, week)] if m},
    }
    from . import spend
    try:
        snap["spend"] = spend.summary(store, now)
    except Exception as e:   # the limits view must not depend on it
        snap["spend"] = {"error": str(e)[:200]}
    from . import setup
    try:
        snap["setup"] = setup.steps(store, now, on_server=on_server)
    except Exception:   # nor on this
        snap["setup"] = []
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


def top_sessions(store, now, rate=None, since_hours=24, limit=8):
    """The sessions that used the most in the last day: project, share, % of their tool's limit, pace now.

    A session on a plan is measured in % of that plan's weekly limit; one on an API key in % of its
    budget. rate: Claude's % per dollar, when the caller has it already."""
    since = now - since_hours * 3600
    W = meter.WEIGHT
    rows = store.conn.execute(
        f"SELECT session_id, harness, MAX(billing) AS billing, MAX(project) AS project, MAX(machine) AS machine,"
        f" SUM({W}) AS w, SUM(COALESCE(cost_usd, 0)) AS usd, COUNT(*) AS n,"
        f" SUM(CASE WHEN subagent THEN {W} ELSE 0 END) AS sub, MIN(ts) AS first, MAX(ts) AS last,"
        f" SUM(CASE WHEN ts >= ? THEN {W} ELSE 0 END) AS hour_w,"
        f" SUM(CASE WHEN ts >= ? THEN COALESCE(cost_usd, 0) ELSE 0 END) AS hour_usd"
        f" FROM usage WHERE ts >= ? GROUP BY session_id, harness HAVING w > 0",
        (now - 3600, now - 3600, since)).fetchall()
    budgets = pools.budgets(store)
    rates = {}

    def pct(r, w, usd):
        """% of the session's own limit for `w` (its weight) or `usd` (on an API key)."""
        if r["billing"] == "api":
            b = budgets.get(r["harness"])
            return 100.0 * usd / b["usd"] if b and b["usd"] > 0 else None
        if r["harness"] not in rates:
            rates[r["harness"]] = rate if rate and r["harness"] == meter.CLAUDE else meter.rate(store, harness=r["harness"])
        k = rates[r["harness"]]
        return w * k if k else None

    def pool_of(r):
        return f"{r['harness']}:api" if r["billing"] == "api" else r["harness"]
    totals, hour_totals = {}, {}
    for r in rows:
        totals[pool_of(r)] = totals.get(pool_of(r), 0.0) + r["w"]
        hour_totals[pool_of(r)] = hour_totals.get(pool_of(r), 0.0) + (r["hour_w"] or 0.0)
    out = []
    for r in rows:
        model = store.conn.execute("SELECT model, project FROM usage WHERE session_id = ? AND NOT subagent"
                                   " ORDER BY ts DESC LIMIT 1", (r["session_id"],)).fetchone()
        out.append({"session": r["session_id"][:8], "project": (model[1] if model and model[1] else r["project"]),
                    "machine": r["machine"], "harness": r["harness"], "pool": pool_of(r),
                    "share": r["w"] / totals[pool_of(r)], "pct_week": pct(r, r["w"], r["usd"]),
                    "pace": pct(r, r["hour_w"] or 0.0, r["hour_usd"] or 0.0),   # % of its limit in the last hour
                    "hour_usd": r["hour_usd"] or 0.0, "hour_w": r["hour_w"] or 0.0, "requests": r["n"],
                    "subagents": (r["sub"] or 0) / r["w"], "model": model[0] if model else None,
                    "first": r["first"], "last": r["last"], "running": now - r["last"] <= LIVE_SECONDS})
    out.sort(key=lambda x: -(x["pct_week"] if x["pct_week"] is not None else x["share"]))
    rest = out[limit:]
    out = out[:limit]
    what_if(store, now, out, hour_totals)
    if rest:
        known = [x["pct_week"] for x in rest if x["pct_week"] is not None]
        out.append({"session": None, "project": f"{len(rest)} more", "share": None,
                    "pct_week": sum(known) if known else None, "running": False})
    return out


WHAT_IF_HOURS = 5   # a session is assumed to carry on at its pace for at most this long


def what_if(store, now, sessions, hour_totals):
    """For each running session: what stopping it now would change, by its pool's limit nearest to running out.

    A session's part of its pool's coming demand is its share of the pool's last hour of usage, for
    the next WHAT_IF_HOURS (sessions don't run for days). Adds `if_stopped`: {pool, limit, short, adds
    (points it would add in those hours), eta (run-out time now), eta_if_stopped (None: no longer runs
    out before the reset)}.
    """
    if not any(v > 0 for v in hour_totals.values()):
        return
    base = forecast.outlook(store, now)
    for x in sessions:
        total = hour_totals.get(x["pool"], 0.0)
        if not x.get("running") or not x.get("hour_w") or total <= 0:
            continue
        looks = [o for o in base if o["pool"] == x["pool"] and o["p50"] is not None]
        if not looks:
            continue
        # the limit that matters: the one you'd run out of first, else the one closest to full by reset
        target = min(looks, key=lambda o: (o["eta"] or float("inf"), -(o["p50"] or 0)))
        part = min(1.0, x["hour_w"] / total)
        o = {y["name"]: y for y in forecast.outlook(store, now, cut=1 - part, cut_hours=WHAT_IF_HOURS,
                                                     pool=x["pool"])}.get(target["name"])
        if not o or o["p50"] is None:
            continue
        x["if_stopped"] = {"pool": x["pool"], "limit": target["name"], "short": target["short"],
                           "adds": target["p50"] - o["p50"], "eta": target["eta"], "hours": WHAT_IF_HOURS,
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
    return f"saves {w['adds']:.1f}% of {w.get('short') or ('5-hour' if w['limit'] == 'five_hour' else 'weekly')}"


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
        text = (f"At this pace you'll run out of your {o['label']} around {alerts.when(o['eta'], now)},"
                f" {alerts.span(o['resets'] - o['eta'])} before it "
                + ("resets." if o.get("kind") != "api" else f"{o['period']} ends."))
        best = [x for x in snap.get("sessions") or [] if (x.get("if_stopped") or {}).get("eta")
                and x["if_stopped"].get("pool", o.get("pool")) == o.get("pool")]
        if best:
            x = max(best, key=lambda x: x["if_stopped"]["eta_if_stopped"] or float("inf"))
            later = x["if_stopped"]["eta_if_stopped"]
            gain = "would get you to the reset" if later is None else f"buys about {alerts.span(later - o['eta'])}"
            if later is None or later - o["eta"] >= 1800:
                text += f" Pausing {x.get('project') or x['session']} {gain}."
        return ("bad" if o.get("stage") in ("act", "last_call") else "warn"), text
    mains = [o for o in looks if o["name"] in ("seven_day", "budget")]   # each pool's long limit
    if mains:
        parts = [f"{o['label']} likely {o['p50']:.0f}% by {alerts.when(o['resets'], now)}"
                 f" ({o['p_hit']:.0%} chance of running out first)" for o in mains]
        return "ok", "On track: " + "; ".join(parts) + "."
    if snap["limits"]:
        return "ok", "Forecast on its way: the first one arrives within the hour."
    return "none", ("No limit readings yet: send a message in Claude Code or Codex with savetokens installed,"
                    " or set an API budget with `savetokens api`.")


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
        bw = max(12, min(40, w - 54))
        lines.append(title("LIMITS") + " " * (2 + 9 + 1 + bw - 6) + c("2", f"{'now':>5}   {'at reset (likely, range)':24} resets"))
        for o in snap["limits"]:
            col = {"heads_up": "33", "act": "31", "last_call": "1;31"}.get(o.get("stage"), "32")
            name = o.get("short") or ("5-hour" if o["name"] == "five_hour" else "weekly")
            at = f"{o['p50']:4.0f}%  ({o['p10']:.0f}–{o['p90']:.0f}%)" if o["p50"] is not None else "–"
            lines.append(f"  {name:9} {c(col, bar(o['used'], o['p10'], o['p50'], o['p90'], bw))} {o['used']:4.0f}%"
                         f"   {at:24} {alerts.when(o['resets'], now)}"
                         + (c("2", f"  ${o['spent_usd']:,.0f} of ${o['budget_usd']:,.0f} {o['per']}")
                            if o.get("kind") == "api" else ""))
        lines.append(c("2", f"  {'':9} █ used  ▒ likely by reset  ░ could reach  · room left"))
        for h, models in (snap.get("unpriced") or {}).items():
            lines.append(c("33", f"  {pools.TOOLS.get(h, h)} API usage on {', '.join(models[:3])} has no price, so it"
                                 f" isn't counted: add one with `savetokens price MODEL INPUT OUTPUT`"))
        lines.append("")

    d = snap["demand"]
    if any(d["past"]) or any(d["next"]):
        if d.get("unit") == "$":
            amount = f"${sum(d['past']):,.2f} spent, ~${sum(d['next']):,.2f} to come"
        else:
            amount = f"{sum(d['past']):.1f}% of the week, ~{sum(d['next']):.1f}% to come"
        tool = f"{d['label'].split(' API')[0].split(' %')[0]} · " if len(snap.get("pools") or []) > 1 else ""
        lines.append(title("USAGE PER HOUR", f"{tool}last {len(d['past'])}h ┊ next {len(d['next'])}h · used {amount}"))
        lines += chart(d, w, color=color)
        lines.append(c("2", f"  {'':2}█ used   ▒ likely   ░ could reach"))
        lines.append("")

    sessions = snap.get("sessions") or []
    if sessions:
        live = sum(1 for x in sessions if x.get("running"))
        lines.append(title("SESSIONS", f"last 24h · {live} running"))
        many = len({x.get("pool") for x in sessions if x.get("session")}) > 1
        lines.append(c("2", f"    {'project':18} {'today':>6}  {'last hour':>9}   if you pause it (next 5h)"))
        for x in sessions:
            dot = c("32", "●") if x.get("running") else c("2", "○")
            today = f"{x['pct_week']:.1f}%" if x.get("pct_week") is not None else "–"
            if x["session"] is None:
                lines.append(c("2", f"  {dot} {x['project'][:18]:18} {today:>6}"))
                continue
            pace = f"{x['pace']:.1f}%" if x.get("running") and x.get("pace") else "–"
            pause = stopping(x, now)
            name = (x["project"] or x["session"])
            if many:
                name = {"codex": "cx ", "hermes": "hm "}.get(x.get("harness"), "") + name
            lines.append(f"  {dot} {name[:18]:18} {today:>6}  {pace:>9}   "
                         + (c("2", pause) if not x.get("running") else pause))
        lines.append(c("2", "    today and last hour are % of each session's weekly limit"
                       + (" (or API budget); cx = Codex, hm = Hermes" if many else "")))
        lines.append("")

    by_tool = {}
    for m in snap["models"]:
        by_tool.setdefault(m.get("harness", meter.CLAUDE), []).append(m)
    for h, ms in by_tool.items():
        mix = " · ".join(f"{m['model'].replace('claude-', '')} {m['share']:.0%}"
                         + (f" ({m['subagents']:.0%} via subagents)" if m["subagents"] >= 0.05 else "")
                         for m in ms[:3])
        head = "MODELS" if len(by_tool) == 1 else f"MODELS {pools.TOOLS.get(h, h)}"
        lines.append(title(head) + "  " + mix + c("2", "  · this week"))
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
