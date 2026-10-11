"""Advice for the user and their agent: where the limits stand, what to change, and what a job would take.

Worked out here from your own usage, not by the LLM, so every session gets the same numbers:

  brief()     each limit's room (points, and hours at the current pace), this session's profile,
              and the options ranked by effect, each with the exact change to make
  estimate()  a job's size in points and how long it would take: the work at your pace, plus any
              wait for a limit to reset on the way

Points are % of a limit: a plan's weekly limit, or an API budget. Suggestions only: nothing here
changes a setting.
"""
from __future__ import annotations

import os
import statistics
import time
from pathlib import Path

from . import alerts, dashboard, forecast, meter, pools

ACTIVE_GAP = 600          # requests further apart than this are a pause, not work
STAT_DAYS = 28
# How to make each change, per tool (instructions only: what to change is worked out from the data)
SUBAGENT_HOW = {
    "claude-code": "set `model:` in .claude/agents/*.md, pass the model when starting a subagent, or start sessions"
                   " with CLAUDE_CODE_SUBAGENT_MODEL={model}",
    "codex": "set the subagent's model in ~/.codex/config.toml (or pass -m {model} for delegated runs)",
    "hermes": "set the delegated task's model to {model} in Hermes's config",
}
EFFORT_HOW = {"claude-code": "/effort medium for routine work", "codex": 'model_reasoning_effort = "medium" in config.toml',
              "hermes": "a lower reasoning effort in Hermes's model settings"}


# ── measuring ────────────────────────────────────────────────────────────────

def active_hours(times) -> float:
    """Hours of work in a run of request times: gaps over ACTIVE_GAP don't count."""
    times = sorted(times)
    return sum(min(b - a, ACTIVE_GAP) for a, b in zip(times, times[1:])) / 3600


class Scale:
    """Points (% of the pool's limit) for captured usage."""

    def __init__(self, store, now):
        self.store, self.now, self.rates = store, now, {}
        self.budgets = pools.budgets(store)

    def rate(self, harness):
        if harness not in self.rates:
            self.rates[harness] = meter.rate(self.store, harness=harness)
        return self.rates[harness]

    def points(self, harness, billing, weight, usd):
        if billing == "api":
            b = self.budgets.get(harness)
            return 100.0 * usd / b["usd"] if b and b["usd"] > 0 else None
        r = self.rate(harness)
        return weight * r if r else None

    def usd_per_point(self, harness, billing=None):
        if billing == "api":
            b = self.budgets.get(harness)
            return b["usd"] / 100 if b else None
        r = self.rate(harness)
        return 1 / r if r and harness == meter.CLAUDE else None


def find_session(store, now, session_id=None, cwd=None, harness=None):
    """The session asking: by id (or its first characters), else the latest one in this folder."""
    if session_id:
        r = store.conn.execute("SELECT session_id FROM usage WHERE session_id LIKE ? ORDER BY ts DESC LIMIT 1",
                               (session_id + "%",)).fetchone()
        if r:
            return r[0]
    project = Path(cwd or os.getcwd()).name
    q = "SELECT session_id FROM usage WHERE project = ? AND ts >= ? AND NOT subagent"
    p = [project, now - 3600]
    if harness:
        q += " AND harness = ?"
        p.append(harness)
    r = store.conn.execute(q + " ORDER BY ts DESC LIMIT 1", p).fetchone()
    return r[0] if r else None


def profile(store, now, sid, scale=None, hours=24):
    """One session's last day: size, pace, model, subagents, how much context each request re-reads."""
    if not sid:
        return None
    scale = scale or Scale(store, now)
    rows = store.conn.execute(
        f"SELECT ts, harness, billing, model, subagent, {meter.WEIGHT} AS w, COALESCE(cost_usd, 0) AS usd,"
        " input + cache_read + cache_write_5m + cache_write_1h AS ctx, cache_read, project"
        " FROM usage WHERE session_id = ? AND ts >= ? ORDER BY ts", (sid, now - hours * 3600)).fetchall()
    if not rows:
        return None
    h, bill = rows[0]["harness"], rows[-1]["billing"]
    w, usd = sum(r["w"] for r in rows), sum(r["usd"] for r in rows)
    last = [r for r in rows if r["ts"] >= now - 3600]
    main = [r for r in last if not r["subagent"]]
    work = active_hours([r["ts"] for r in rows])
    pts = scale.points(h, bill, w, usd)
    hour_pts = scale.points(h, bill, sum(r["w"] for r in last), sum(r["usd"] for r in last))
    model = next((r["model"] for r in reversed(rows) if not r["subagent"] and r["model"]), None)
    return {"session": sid[:8], "harness": h, "pool": f"{h}:api" if bill == "api" else h,
            "project": next((r["project"] for r in reversed(rows) if r["project"]), None), "model": model,
            "points_today": pts, "points_last_hour": hour_pts, "active_hours": work,
            "points_per_active_hour": pts / work if pts is not None and work >= 0.25 else None,
            "subagents": sum(r["w"] for r in rows if r["subagent"]) / w if w else 0.0,
            "context_tokens": statistics.fmean(r["ctx"] for r in main) if main else None,
            "running": now - rows[-1]["ts"] <= dashboard.LIVE_SECONDS}


def sizes(store, now, harness, project=None, scale=None):
    """Past sessions' sizes and paces (last STAT_DAYS): what 'typical' and 'big' mean, and work per hour."""
    scale = scale or Scale(store, now)
    q = (f"SELECT session_id, billing, ts, {meter.WEIGHT} AS w, COALESCE(cost_usd, 0) AS usd FROM usage"
         " WHERE harness = ? AND ts >= ?")
    p = [harness, now - STAT_DAYS * 86400]
    if project:
        q += " AND session_id IN (SELECT DISTINCT session_id FROM usage WHERE project = ? AND harness = ?)"
        p += [project, harness]
    by = {}
    for r in store.conn.execute(q, p):
        s = by.setdefault(r["session_id"], {"billing": r["billing"], "ts": [], "w": 0.0, "usd": 0.0})
        s["ts"].append(r["ts"])
        s["w"] += r["w"]
        s["usd"] += r["usd"]
    done = []
    for s in by.values():
        pts = scale.points(harness, s["billing"], s["w"], s["usd"])
        work = active_hours(s["ts"])
        if pts and work >= 0.25:
            done.append((pts, work))
    if len(done) < 3:
        return None
    pts = sorted(x for x, _ in done)
    pace = sorted(x / h for x, h in done)
    q_ = lambda v, f: v[min(len(v) - 1, int(f * len(v)))]
    return {"sessions": len(done), "project": project, "typical_points": q_(pts, 0.5), "big_points": q_(pts, 0.9),
            "small_points": q_(pts, 0.25), "points_per_active_hour": q_(pace, 0.5),
            "pace_range": [q_(pace, 0.25), q_(pace, 0.75)]}


# ── options ──────────────────────────────────────────────────────────────────

def levers(store, now, looks, sessions, me=None, scale=None) -> list[dict]:
    """Options, biggest measured effect first. Each: {id, title, effect, points, how}."""
    scale = scale or Scale(store, now)
    out = []
    risky = [o for o in looks if o["p50"] is not None and (o["eta"] or (o["p_hit"] or 0) >= 0.2 or o["p50"] >= 90)]
    worst = min(risky, key=lambda o: (o["eta"] or float("inf"), -(o["p_hit"] or 0))) if risky else None

    # pause the running session whose pause buys the most
    cands = [x for x in sessions if (x.get("if_stopped") or {}).get("pool") and x.get("running")]
    if cands:
        def gain(x):
            w = x["if_stopped"]
            if w["eta"]:
                return float("inf") if w["eta_if_stopped"] is None else w["eta_if_stopped"] - w["eta"]
            return w["adds"] * 3600   # rank by points when nobody runs out
        x = max(cands, key=gain)
        w, name = x["if_stopped"], x.get("project") or x["session"]
        if w["eta"]:
            effect = ("gets you to the reset" if w["eta_if_stopped"] is None
                      else f"buys about {alerts.span(w['eta_if_stopped'] - w['eta'])}")
        else:
            effect = f"saves {w['adds']:.1f} points of the {w['short']} limit"
        out.append({"id": "pause", "title": f"Pause {name} for {w['hours']} hours", "points": w["adds"],
                    "effect": f"Pausing {name} {effect}.", "how": f"Stop or park the {name} session; resume later."})

    out += _engine_levers(store, now, sessions, me, scale)

    if worst:
        # the 5-hour limit is the one in the way: wait for its reset
        if worst["name"] == "five_hour":
            out.append({"id": "after_reset", "title": f"Hold big jobs until {alerts.when(worst['resets'], now)}",
                        "points": None,
                        "effect": f"The 5-hour limit resets at {alerts.when(worst['resets'], now)}; the weekly limit has room.",
                        "how": "Queue batch work (test sweeps, wide refactors, fan-outs) to start after the reset."})
        # another tool with room
        others = [o for o in looks if o["pool"] != worst["pool"] and o["kind"] == "subscription"
                  and o["name"] == "seven_day" and o["p50"] is not None and o["p50"] < 70]
        if others:
            o = min(others, key=lambda o: o["p50"])
            tool = pools.TOOLS.get(o["harness"], o["harness"])
            out.append({"id": "move", "title": f"Move some work to {tool}", "points": None,
                        "effect": f"{o['label']} is at {o['used']:.0f}%, likely {o['p50']:.0f}% by its reset.",
                        "how": f"Run the next self-contained task in {tool}."})
    for x in out:
        x.setdefault("kind", "forecast")
    measured = sorted((x for x in out if x["points"]), key=lambda x: -x["points"])
    return measured + [x for x in out if not x["points"]]


def _pts(scale, harness, billing, usd):
    per = scale.usd_per_point(harness, billing)
    return usd / per if per and usd else None


def _engine_levers(store, now, sessions, me, scale) -> list[dict]:
    """Facts and what-ifs from the last day's records (efficiency.py): no thresholds, no tool rules."""
    from . import efficiency
    out, since = [], now - 86400

    # a running conversation whose prompt has grown: what the growth cost (an upper bound on starting fresh)
    best = None
    for x in sessions:
        if not (x.get("running") and x.get("session")):
            continue
        sid = _full(store, x["session"])
        f = efficiency.facts(store, since, now, {"session_id": sid})
        c = f["context"]
        if c and c["growth_usd"] > 0 and (best is None or c["growth_usd"] > best[1]["growth_usd"]):
            best = (x, c, f, sid)
    if best:
        x, c, f, sid = best
        p = profile(store, now, sid, scale, hours=1)
        name = (p or {}).get("project") or x["session"]
        h = (p or {}).get("harness") or x.get("harness")
        billing = "api" if (p or {}).get("pool", "").endswith(":api") else None
        ctx = f" Its prompt is now about {p['context_tokens'] / 1000:.0f}k tokens, re-sent each request." \
            if p and p.get("context_tokens") else ""
        out.append({"id": "fresh", "kind": "what-if", "title": f"Start {name} fresh at its next task",
                    "points": _pts(scale, h, billing, c["growth_usd"]), "usd": c["growth_usd"],
                    "effect": f"The part of {name}'s prompts beyond where it started cost ${c['growth_usd']:,.2f} today.{ctx}"
                              " That's the most starting fresh could save; a handover note costs a little back.",
                    "how": "At the end of the current task: write a short handover note, then clear the conversation"
                           " or start a new session."})

    # subagent work at the prices of a cheaper model you already use from the same provider
    swaps = efficiency.as_model(store, since, now, {"subagent": 1})
    if swaps:
        s = swaps[0]
        h = next((r[0] for r in store.conn.execute(
            "SELECT harness FROM usage WHERE model = ? AND subagent AND ts >= ? LIMIT 1", (s["model"], since))), None)
        out.append({"id": "subagents", "kind": "what-if", "title": f"Run subagents on {s['as']}",
                    "points": _pts(scale, h, None, s["saving_usd"]), "usd": s["saving_usd"],
                    "effect": f"Today's subagent work on {s['model']} would have cost ${s['saving_usd']:,.2f} less at"
                              f" {s['as']}'s prices, a model you already use. Price only: whether it does the job as"
                              " well isn't measured yet.",
                    "how": SUBAGENT_HOW.get(h, "set the subagents' model to {model}").format(model=s["as"])})

    # by tool: re-sent context not served from cache, and how much output is reasoning
    for h in [r[0] for r in store.conn.execute("SELECT DISTINCT harness FROM usage WHERE ts >= ?", (since,))]:
        f = efficiency.facts(store, since, now, {"harness": h})
        tool = pools.TOOLS.get(h, h)
        bill = "api" if any(b["name"] == "api" for b in f["by"].get("billing", [])) else None
        c = f["context"]
        if c and c["cache_saving_usd"] > 0:
            out.append({"id": f"cache:{h}", "kind": "what-if", "title": f"Cache more of {tool}'s re-sent context",
                        "points": _pts(scale, h, bill, c["cache_saving_usd"]), "usd": c["cache_saving_usd"],
                        "effect": f"{c['resent_uncached_tokens']:,} re-sent tokens were billed as fresh input today;"
                                  f" at the cache price that's ${c['cache_saving_usd']:,.2f} less (writing them to the"
                                  f" cache once costs up to ${c['cache_write_cost_usd']:,.2f}).",
                        "how": "Keep prompt caching on, and keep the start of the prompt (instructions, tools) unchanged"
                               " between requests so it can be reused."})
        r = f["reasoning"]
        if r and r["tokens"]:
            out.append({"id": f"reasoning:{h}", "kind": "fact", "title": f"Lower {tool}'s effort for routine work",
                        "points": None,
                        "effect": f"{r['share_of_output']:.0%} of {tool}'s output tokens today were reasoning"
                                  f" ({r['tokens']:,} tokens).",
                        "how": EFFORT_HOW.get(h, "a lower reasoning effort for routine work")})
    return out


def _full(store, short):
    r = store.conn.execute("SELECT session_id FROM usage WHERE session_id LIKE ? LIMIT 1", (short + "%",)).fetchone()
    return r[0] if r else short


# ── the brief ────────────────────────────────────────────────────────────────

def brief(store, now=None, session_id=None, cwd=None, harness=None) -> dict:
    now = now or time.time()
    scale = Scale(store, now)
    looks = forecast.outlook(store, now)
    sessions = dashboard.top_sessions(store, now)
    sid = find_session(store, now, session_id, cwd, harness)
    me = profile(store, now, sid, scale)
    if me:
        me["session_full"] = sid
    paces = {}
    for x in sessions:
        if x.get("running") and x.get("pace"):
            paces[x["pool"]] = paces.get(x["pool"], 0.0) + x["pace"]
    limits = []
    for o in looks:
        pace = paces.get(o["pool"], 0.0) * (meter.five_hour_ratio(store, harness=o["harness"])
                                            if o["name"] == "five_hour" else 1.0)
        limits.append({k: o.get(k) for k in ("pool", "name", "label", "kind", "used", "p10", "p50", "p90", "p_hit",
                                             "eta", "resets", "spent_usd", "budget_usd", "per")}
                      | {"room_points": max(0.0, 100 - o["used"]), "pace_points_per_hour": pace,
                         "hours_left_at_pace": (100 - o["used"]) / pace if pace > 0 else None,
                         "stage": alerts.stage(o, now)})
    snap = {"now": now, "limits": looks, "sessions": sessions}
    level, text = dashboard.headline(snap)
    project = me["project"] if me else Path(cwd or os.getcwd()).name
    sizing = {}
    for p in pools.pools(store, now):
        s = sizes(store, now, p.harness, project, scale) or sizes(store, now, p.harness, None, scale)
        if s:
            sizing[p.id] = s | {"usd_per_point": scale.usd_per_point(p.harness, "api" if p.kind == "api" else None)}
    options = levers(store, now, looks, sessions, me, scale)
    from . import effects
    effects.record_shown(store, options, now)   # so a week on, what happened after can be measured
    return {"now": now, "headline": {"level": level, "text": text}, "limits": limits, "this_session": me,
            "options": options, "measured": [m for m in effects.followups(store, now)][:3],
            "sessions": [{k: x.get(k) for k in ("project", "harness", "pool", "pct_week", "pace", "subagents",
                                                "running")} for x in sessions if x.get("session")][:5],
            "sizing": sizing}


def brief_text(b) -> str:
    """The brief in a few lines, for an agent's context or a terminal."""
    now = b["now"]
    lines = [b["headline"]["text"]]
    for l in b["limits"]:
        s = f"- {l['label']}: {l['used']:.0f}% used"
        if l["p50"] is not None:
            s += f", likely {l['p50']:.0f}% by {alerts.when(l['resets'], now)} ({l['p_hit']:.0%} chance of running out)"
        if l["hours_left_at_pace"] is not None and l["hours_left_at_pace"] < 48:
            s += f"; {l['hours_left_at_pace']:.1f} h of room at the last hour's pace"
        lines.append(s + ".")
    me = b["this_session"]
    if me and me["points_today"] is not None:
        lines.append(f"This session ({me['project'] or me['session']}): {me['points_today']:.1f} points today,"
                     f" {me['points_last_hour'] or 0:.1f} in the last hour, {me['subagents']:.0%} via subagents"
                     + (f", re-reading ~{me['context_tokens'] / 1000:.0f}k tokens a request" if me["context_tokens"] else "")
                     + ".")
    if b["options"]:
        lines.append("Options, biggest first (each a fact, a what-if under its stated assumption, or a forecast):")
        for o in b["options"][:4]:
            lines.append(f"- [{o.get('kind', 'forecast')}] {o['title']}: {o['effect']} How: {o['how']}")
    for m in b.get("measured") or []:
        lines.append(f"- [measured] {m['text']}")
    return "\n".join(lines)


# ── estimating a job ─────────────────────────────────────────────────────────

STEP = 0.1   # hours


def estimate(store, now=None, points=None, usd=None, like=None, hours=None, parallel=1, pool=None,
             session_id=None, cwd=None, start=None) -> dict:
    """A job's size and run time. Size it by points, API dollars, `like` ("small", "typical", "big", or a
    session id) or `hours` of work at your pace. parallel: sessions or subagents working at once."""
    now = now or time.time()
    scale = Scale(store, now)
    every = pools.pools(store, now)
    if not every:
        return {"error": "no limits known yet"}
    sid = find_session(store, now, session_id, cwd)
    me = profile(store, now, sid, scale)
    p = next((x for x in every if x.id == pool), None) or next((x for x in every if me and x.id == me["pool"]), every[0])
    project = me["project"] if me else Path(cwd or os.getcwd()).name
    stats = sizes(store, now, p.harness, project, scale) or sizes(store, now, p.harness, None, scale)
    pace = (me["points_per_active_hour"] if me and me["running"] and me["points_per_active_hour"]
            else stats["points_per_active_hour"] if stats else None)
    lo_hi = None
    if points is None and usd is not None:
        per = scale.usd_per_point(p.harness, "api" if p.kind == "api" else None)
        points = usd / per if per else None
    if points is None and like:
        key = {"small": "small_points", "typical": "typical_points", "big": "big_points"}.get(like)
        if key and stats:
            points = stats[key]
        elif not key:
            other = profile(store, now, _full(store, like), scale, hours=24 * STAT_DAYS)
            points = other["points_today"] if other else None
    if points is None and hours is not None and pace:
        points = hours * pace * parallel
    if points is None:
        return {"error": "can't size the job: give points, usd, like (small/typical/big/a session) or hours"}
    if not pace:
        return {"error": "not enough history to know your pace yet", "points": points}
    rate_h = pace * parallel
    if stats:   # the spread of paces across past sessions, around this estimate
        mid = stats["points_per_active_hour"]
        lo_hi = [points / rate_h * mid / stats["pace_range"][1], points / rate_h * mid / stats["pace_range"][0]]
    looks = {o["name"]: o for o in forecast.outlook(store, now, pool=p.id)}
    others = sum(x.get("pace") or 0 for x in dashboard.top_sessions(store, now)
                 if x.get("running") and x.get("pool") == p.id and (not me or x["session"] != me["session"]))
    run = _simulate(store, p, looks, now, start or now, points, rate_h, others)
    main = looks.get("budget") or looks.get("seven_day")
    out = {"pool": p.id, "points": points, "work_hours": points / rate_h,
           "work_hours_range": lo_hi, "pace_points_per_hour": rate_h, "parallel": parallel, **run,
           # on top of the usage already forecast (if the job is extra work, not part of it)
           "likely_at_reset_with_job": main["p50"] + points if main and main["p50"] is not None else None,
           "limit": main["label"] if main else None, "resets": main["resets"] if main else None}
    if run["waits"] and not start:   # would starting at the first reset avoid the wait?
        later = _simulate(store, p, looks, now, run["waits"][0]["until"], points, rate_h, others)
        if not later["waits"] and later["finish"] is not None:
            out["start_after"] = {"at": run["waits"][0]["until"], "finish": later["finish"]}
    out["summary"] = _summary(out, looks, now)
    return out


def _simulate(store, p, looks, now, start, points, rate_h, others):
    """Run the job from `start`, hour by hour, against the pool's limits. Other running sessions keep
    their pace for the next few hours."""
    if p.kind == "api":
        b = looks.get("budget")
        lim = {"budget": [b["used"] if b else 0.0, b["resets"] if b else None, 1.0, None]}
    else:
        ratio = meter.five_hour_ratio(store, harness=p.harness)
        lim = {n: [o["used"], o["resets"], ratio if n == "five_hour" else 1.0, 5 * 3600 if n == "five_hour" else None]
               for n, o in looks.items()}
    t, left, waits = now, points, []
    while left > 1e-9:
        if t - now > 14 * 86400:
            return {"finish": None, "waits": waits, "fits": False}
        dt = STEP * 3600
        for v in lim.values():
            if v[1] and t >= v[1]:          # a window reset
                v[0], v[1] = 0.0, (t + v[3]) if v[3] else None
        bg = others * (STEP if t - now < dashboard.WHAT_IF_HOURS * 3600 else 0.0)
        go = t >= start
        add = min(left, rate_h * STEP) if go else 0.0
        full = [n for n, v in lim.items() if v[0] + (add + bg) * v[2] > 100]
        if full and go:
            n = full[0]
            until = lim[n][1]
            if n in ("seven_day", "budget") or until is None:
                return {"finish": None, "waits": waits, "fits": False, "blocked_by": n,
                        "blocked_until": until, "points_short": left}
            waits.append({"limit": n, "from": t, "until": until})
            for v in lim.values():
                v[0] += bg
            t = until
            continue
        for v in lim.values():
            v[0] += (add + bg) * v[2]
        left -= add
        t += dt if left > 1e-9 else (add / rate_h) * 3600 if rate_h else dt
    return {"finish": t, "waits": waits, "fits": not waits}


def _summary(e, looks, now):
    lim = "budget" if e["pool"].endswith(":api") else "weekly limit"
    s = f"About {e['points']:.1f} points of your {lim} and {e['work_hours']:.1f} h of work"
    if e.get("work_hours_range"):
        lo, hi = e["work_hours_range"]
        s += f" ({lo:.1f}–{hi:.1f} h)"
    s += f" at {e['pace_points_per_hour']:.1f} points an hour."
    if e["finish"] is None:
        o = looks.get(e.get("blocked_by") or "")
        s += (f" It doesn't fit: the {o['label'] if o else 'limit'} runs out first, about {e.get('points_short', 0):.1f}"
              f" points short, until {alerts.when(e['blocked_until'], now) if e.get('blocked_until') else 'the reset'}.")
    elif e["waits"]:
        w = e["waits"][0]
        s += (f" The 5-hour limit fills at {alerts.when(w['from'], now)}; it waits until {alerts.when(w['until'], now)}"
              f" and finishes around {alerts.when(e['finish'], now)}.")
        if e.get("start_after"):
            s += (f" Starting after {alerts.when(e['start_after']['at'], now)} runs straight through, done around"
                  f" {alerts.when(e['start_after']['finish'], now)}.")
    else:
        s += f" On its own it runs straight through, done around {alerts.when(e['finish'], now)}."
    w = e.get("likely_at_reset_with_job")
    if w is not None and e["finish"] is not None:
        s += (f" On top of your usual usage, the {e['limit']} would likely reach {w:.0f}% by"
              f" {alerts.when(e['resets'], now)}" + (": that won't fit alongside it." if w > 100 else "."))
    return s


# ── the agent's note ─────────────────────────────────────────────────────────

NOTE_P_HIT = 0.2   # tell the agent only when a limit is this likely to run out (or a stage is reached)


def agent_note(store, now=None) -> str | None:
    """A few lines for the agent's context when a limit is at risk, else None. Made by upkeep, read by hooks."""
    now = now or time.time()
    looks = forecast.outlook(store, now)
    risky = [o for o in looks if o["used"] >= 100 or (o["p50"] is not None and (
        alerts.stage(o, now) or (o["p_hit"] or 0) >= NOTE_P_HIT))]
    if not risky:
        return None
    parts = []
    for o in sorted(risky, key=lambda o: o["eta"] or float("inf")):
        if o["used"] >= 100:
            parts.append(f"{o['label']} is used up until {alerts.when(o['resets'], now)}")
            continue
        s = f"{o['label']} {o['used']:.0f}% used, likely {o['p50']:.0f}% by {alerts.when(o['resets'], now)}"
        if o["eta"]:
            s += f" (runs out ~{alerts.when(o['eta'], now)} at this pace)"
        parts.append(s)
    opts = levers(store, now, looks, dashboard.top_sessions(store, now))
    text = "savetokens: " + "; ".join(parts) + "."
    if opts:
        text += " To pace this work: " + " ".join(f"{o['title']} ({o['how']})" for o in opts[:2])
    text += (" Prefer Sonnet for search/exploration subagents and avoid wide fan-outs unless the user wants them."
             " Before a big job, estimate it (savetokens estimate_job tool, or `savetokens estimate`) and tell"
             " the user what it costs. Never change settings without asking.")
    return text


# ── setup the agent can propose (never run) ───────────────────────────────────

def setup(store, now=None, cfg=None, found=None) -> list[dict]:
    """What's left to set up here, each with the exact command for the user to run (or agree to). Nothing is
    changed. The checklist itself is setup.steps."""
    from . import setup as setup_
    return [{"why": s["detail"], "command": s["command"], "ask": s["ask"], "default": s["default"]}
            for s in setup_.todo(setup_.steps(store, now, cfg, found))]
