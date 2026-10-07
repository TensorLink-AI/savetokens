"""Warning score: replay your real usage and score how well each forecaster warns before a limit hit.

  python3 evals/pacing/warn_score.py [--hit-rates 0.1,0.25] [--max-credits 25000] [--json out.json]

Every week (from your real weekly reset) and every 5-hour window in your history is replayed.
Forecasts are remade as the product now does: hourly while you were active, every 3 hours
otherwise, and not while nothing changed. At each check, each warning rule decides whether to
warn. A window "hits" when its real usage reaches the limit. Scores per rule:

  caught       share of hits warned about at least LEAD_MIN minutes ahead
  lead         median time from the first warning to the hit
  false/week   warnings, per week of history, in windows that never hit
  quiet        share of non-hitting windows with no warning

Rules:
  ephemeris@p, baseline@p   chance of reaching the limit before the reset is at least p
  burn                      usage so far, projected at the window's average rate so far
  burn-1h                   the last hour's rate continued to the reset ("if this pace continues")
  used@80%                  80% of the limit already used (what most monitors do)
  jump                      (weeks) the projection watch's jump flag, on the replayed projections

Limits: by default, set so that a given share of windows would hit (what your usage would
look like on a smaller plan); with a learned limit rate, also your real limit.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim  # noqa: E402

HOUR = 3600
WEEK = 7 * 86400
FIVE = 5 * HOUR
LEAD_MIN = 30
RISKS = (0.5, 0.8)
PATHS = 200


def origins(hours, start, end):
    """Forecast times inside [start, end), as the product makes them: hourly after an active hour,
    3-hourly otherwise, none while nothing changed (up to 12 hours)."""
    out, last_made, last_use = [], None, None
    for h in range(int(start), int(end), HOUR):
        if hours.get(h - HOUR, 0.0) > 0:
            last_use = h - HOUR
        if last_made is None:
            out.append(h)
            last_made = h
            continue
        age = h - last_made
        changed = last_use is not None and last_use >= last_made     # an hour of usage the forecast hasn't seen
        active = hours.get(h - HOUR, 0.0) > 0
        if (changed and (age >= 3 * HOUR or (active and age >= HOUR))) or age >= 12 * HOUR:
            out.append(h)
            last_made = h
    return out


def forecasts(hours, os_, horizon_end, sources, workers=4, max_credits=None, log=lambda *_: None):
    """{source: {origin: paths}} out to horizon_end(origin)."""
    from savetokens import backtest, windows
    first = min(hours)
    out = {s: {} for s in sources}
    spent = [0.0]

    def one(o):
        hist = backtest.history_at(hours, o, first)
        horizon = max(1, int(math.ceil((horizon_end(o) - o) / HOUR)))
        res = {}
        if "baseline" in sources and hist:
            data = windows.baseline_paths(hist, o, horizon, n=PATHS, seed=int(o) % 100_000)
            if data:
                res["baseline"] = {"start": o, "hours": horizon, "n": PATHS, "data": data}
        if "ephemeris" in sources and hist:
            series = [round(v, 4) for _, v in hist]
            if len(series) >= windows.MIN_EPHEMERIS_HOURS and any(series):
                if max_credits is not None and spent[0] >= max_credits:
                    return o, res
                try:
                    qs, c = sim.ephemeris_quantiles(series, o, horizon)
                    spent[0] += c
                    res["ephemeris"] = {"start": o, "hours": horizon, "n": PATHS,
                                        "data": windows.copula_paths(qs, n=PATHS, seed=int(o) % 100_000)}
                except Exception as e:
                    log(f"  ephemeris at {time.ctime(o)} failed: {e}")
        return o, res

    with ThreadPoolExecutor(workers) as pool:
        for k, (o, res) in enumerate(pool.map(one, os_), 1):
            for s, p in res.items():
                out[s][o] = p
            if k % 50 == 0:
                log(f"  {k}/{len(os_)} forecasts, {spent[0]:,.0f} credits so far")
    return out, spent[0]


def used_at(hours_fine, start, t):
    return sum(c for ts, c in hours_fine if start <= ts < t)


def score_windows(wins, events, fc, limit, checks_every, sources, log=lambda *_: None):
    """wins: [(start, end)]. Returns per rule: caught, lead, false alarms, quiet."""
    from savetokens import projection, windows
    import bisect
    ts_list = [ts for ts, _ in events]
    cum = [0.0]
    for _, c in events:
        cum.append(cum[-1] + c)

    def used(a, b):
        return cum[bisect.bisect_left(ts_list, b)] - cum[bisect.bisect_left(ts_list, a)]

    keys = {s: sorted(fc.get(s, {})) for s in sources}

    def latest(s, t):
        k = keys[s]
        i = bisect.bisect_right(k, t) - 1
        return fc[s][k[i]] if i >= 0 else None

    rules = [f"{s}@{r:g}" for s in sources for r in RISKS] + ["burn", "burn-1h", "used@80%"]
    if "ephemeris" in sources and checks_every >= HOUR:
        rules.append("jump")
    res = {r: {"hits": 0, "caught": 0, "leads": [], "false": 0, "quiet": 0, "nohit": 0} for r in rules}
    for start, end in wins:
        total = used(start, end)
        hit_t = None
        if total >= limit:
            # first event that takes usage to the limit
            i0 = bisect.bisect_left(ts_list, start)
            for j in range(i0, len(events)):
                if cum[j + 1] - cum[i0] >= limit:
                    hit_t = ts_list[j]
                    break
        first_warn = {r: None for r in rules}
        proj = []   # (t, p10, p50, p90) of the Ephemeris projection, for the jump rule
        t = start + checks_every
        while t < (hit_t or end):
            u = used(start, t)
            for s in sources:
                p = latest(s, t)
                rem = windows.remaining(p, t, end) if p else None
                if rem:
                    p_hit = sum(u + x >= limit for x in rem) / len(rem)
                    for r in RISKS:
                        if p_hit >= r and first_warn[f"{s}@{r:g}"] is None:
                            first_warn[f"{s}@{r:g}"] = t
                    if s == "ephemeris":
                        v = sorted(u + x for x in rem)
                        proj.append((t, v[len(v) // 10], v[len(v) // 2], v[9 * len(v) // 10]))
            elapsed = t - start
            if elapsed > 0 and first_warn["burn"] is None and u + u / elapsed * (end - t) >= limit:
                first_warn["burn"] = t
            last_h = used(max(start, t - HOUR), t)
            if first_warn["burn-1h"] is None and u + last_h * (end - t) / HOUR >= limit:
                first_warn["burn-1h"] = t
            if first_warn["used@80%"] is None and u >= 0.8 * limit:
                first_warn["used@80%"] = t
            if "jump" in first_warn and first_warn["jump"] is None and len(proj) >= projection.MIN_POINTS:
                if _jump(proj, t, limit):
                    first_warn["jump"] = t
            t += checks_every
        for r in rules:
            w = first_warn[r]
            if hit_t is not None:
                res[r]["hits"] += 1
                if w is not None and hit_t - w >= LEAD_MIN * 60:
                    res[r]["caught"] += 1
                if w is not None:
                    res[r]["leads"].append((hit_t - w) / 60)
            else:
                res[r]["nohit"] += 1
                if w is None:
                    res[r]["quiet"] += 1
                else:
                    res[r]["false"] += 1
    return res


def _jump(proj, t, limit):
    """The projection watch's jump rule (projection.flags), applied to a replayed projection series."""
    from savetokens import projection
    pts = [p for p in proj if p[0] >= t - projection.NORM_HOURS * HOUR]
    vals = [p[2] / limit * 100 for p in pts]
    if len(vals) < projection.MIN_POINTS:
        return False
    prior = [v for p, v in zip(pts[:-1], vals[:-1]) if p[0] >= t - projection.MA_HOURS * HOUR]
    diffs = [b - a for a, b in zip(vals, vals[1:])]
    if not prior:
        return False
    move = statistics.pstdev(diffs[:-1]) if len(diffs) > 2 else 0.0
    return vals[-1] - statistics.fmean(prior) > max(projection.JUMP_SIGMAS * move, projection.JUMP_MIN_PCT)


def summarise(res, weeks_of_history):
    out = {}
    for r, d in res.items():
        out[r] = {"hits": d["hits"], "caught": d["caught"] / d["hits"] if d["hits"] else None,
                  "lead_min_median": statistics.median(d["leads"]) if d["leads"] else None,
                  "false_per_week": d["false"] / weeks_of_history if weeks_of_history else None,
                  "quiet": d["quiet"] / d["nohit"] if d["nohit"] else None, "windows_without_hit": d["nohit"]}
    return out


def week_starts(first, last, weekday=4, hour=3):
    d = datetime.fromtimestamp(first + 86400).replace(hour=hour, minute=0, second=0, microsecond=0)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    out = []
    while (d + timedelta(days=7)).timestamp() <= last:
        out.append(d.timestamp())
        d += timedelta(days=7)
    return out


def run(hit_rates=(0.1, 0.25), sources=("baseline", "ephemeris"), max_credits=25000, weekday=4, hour=3,
        log=lambda *_: None):
    from savetokens import backtest, limits
    from savetokens.store import Store
    store = Store()
    events = backtest.demand(store)
    hours = backtest.hourly(events)
    first, last = events[0][0], events[-1][0]
    weeks = [(s, s + WEEK) for s in week_starts(first, last, weekday, hour)]
    fives = [(s, e) for s, e, _ in backtest.limit_windows(events) if s >= weeks[0][0] and e <= last]
    span_weeks = (last - weeks[0][0]) / WEEK
    os_ = origins(hours, weeks[0][0], weeks[-1][1])
    log(f"{len(weeks)} weeks, {len(fives)} 5-hour windows, {len(os_)} forecast times")

    def week_end(o):
        return next(e for s, e in weeks if s <= o < e) if any(s <= o < e for s, e in weeks) else o + 24 * HOUR

    fc, credits = forecasts(hours, os_, week_end, sources, max_credits=max_credits, log=log)
    log(f"forecasts done: {credits:,.0f} credits")
    rate = limits.effective_rate(store, "seven_day")
    rate5 = limits.effective_rate(store, "five_hour")
    out = {"weeks": len(weeks), "five_hour_windows": len(fives), "forecast_times": len(os_), "credits": credits,
           "history_weeks": span_weeks, "scenarios": []}
    for kind, wins, every, real in (("week", weeks, HOUR, rate), ("five_hour", fives, 15 * 60, rate5)):
        totals = sorted(sum(c for ts, c in events if s <= ts < e) for s, e in wins)
        limits_ = [(f"{r:.0%} of windows hit", totals[min(len(totals) - 1, int((1 - r) * len(totals)))])
                   for r in hit_rates]
        if real:
            limits_.append(("your real limit", 100 / real))
        for name, lim in limits_:
            res = score_windows(wins, events, fc, lim, every, sources, log)
            out["scenarios"].append({"kind": kind, "limit": name, "limit_usd": lim,
                                     "rules": summarise(res, span_weeks)})
    return out


def render(r):
    lines = [f"{r['weeks']} weeks and {r['five_hour_windows']} 5-hour windows of your usage;"
             f" {r['forecast_times']} forecast times (hourly while active, 3-hourly otherwise);"
             f" {r['credits']:,.0f} Ephemeris credits (cached ones free)",
             f"caught = hits warned about {LEAD_MIN}+ min ahead; lead = median minutes from first warning to the hit;"
             " false/wk = warnings in windows that never hit, per week of history", ""]
    for sc in r["scenarios"]:
        kind = "week" if sc["kind"] == "week" else "5-hour window"
        any_rule = next(iter(sc["rules"].values()))
        lines.append(f"{kind}, limit = {sc['limit']} (${sc['limit_usd']:,.0f}): {any_rule['hits']} hits,"
                     f" {any_rule['windows_without_hit']} windows without one")
        lines.append(f"  {'rule':16} {'caught':>7} {'lead':>9} {'false/wk':>9} {'quiet':>6}")
        for name, d in sc["rules"].items():
            lead = d["lead_min_median"]
            lead_s = "" if lead is None else (f"{lead / 60:.1f} h" if lead >= 120 else f"{lead:.0f} min")
            lines.append(f"  {name:16} {'' if d['caught'] is None else format(d['caught'], '.0%'):>7} {lead_s:>9}"
                         f" {'' if d['false_per_week'] is None else format(d['false_per_week'], '.1f'):>9}"
                         f" {'' if d['quiet'] is None else format(d['quiet'], '.0%'):>6}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hit-rates", default="0.1,0.25")
    ap.add_argument("--max-credits", type=float, default=25000)
    ap.add_argument("--no-ephemeris", action="store_true")
    ap.add_argument("--weekday", type=int, default=4, help="weekly reset day, 0 = Monday (yours: Friday)")
    ap.add_argument("--hour", type=int, default=3, help="weekly reset hour, local time")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    r = run([float(x) for x in a.hit_rates.split(",")], ("baseline",) if a.no_ephemeris else ("baseline", "ephemeris"),
            a.max_credits, a.weekday, a.hour, log=lambda m: print(m, file=sys.stderr, flush=True))
    print(render(r))
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
