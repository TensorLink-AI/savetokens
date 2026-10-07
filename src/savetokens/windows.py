"""Actuals and forecasts for this hour, the 5-hour window, today and this week.

Forecasts are made hourly in API-equivalent dollars, separately for subscription
usage (sub_usd) and API-billed usage (api_usd), so they never depend on the
learned limit rates; those convert them to limit % only for display.

Each forecaster produces sample paths of the coming hours:
  baseline   past days replayed: each future calendar day copies the hourly
             profile of a random recent day (keeps within-day correlation).
  ephemeris  hourly quantiles from the Ephemeris ensemble, sampled with a
             Gaussian copula (correlation RHO between hours) so window totals
             get realistic widths.
A window's forecast is: what is already in it + the sum of the paths over the
rest of it. Every window forecast is recorded and scored when the window ends.

Three levels:
  session   this session's usage (local)
  machine   all sessions on this machine for the current account (local)
  account   Claude Code's own limit readings: every device and claude.ai
"""
from __future__ import annotations

import math
import random
import time
from array import array
from collections import defaultdict
from datetime import datetime

from . import limits
from .store import Store, load_config

HOUR = 3600
HISTORY_HOURS = 28 * 24
MAX_HORIZON = 7 * 24
MIN_EPHEMERIS_HOURS = 12   # backtest: Ephemeris beats the baseline from a new user's first 12 hours
PATHS = 300
RHO = 0.6
QUANTILE_GRID = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)
UNITS = ("sub_usd", "api_usd")


def hour_floor(ts):
    return int(ts // HOUR) * HOUR


def _local_midnight(ts):
    d = datetime.fromtimestamp(ts)
    return datetime(d.year, d.month, d.day).timestamp()


# ── usage in each unit ───────────────────────────────────────────────────────

def classifier(store: Store):
    """Function event -> unit (or None for usage of another account)."""
    bmap = limits.billing_map(store)
    defaults = {"claude-code": limits.default_billing(store, "claude-code"), "hermes": limits.API}
    others = limits.other_account_sessions(store)

    def unit(e):
        if e.session_id in others or not e.cost_usd:
            return None
        return "sub_usd" if limits.billing_of(e, bmap, defaults) == limits.SUBSCRIPTION else "api_usd"
    return unit


def usage_between(store: Store, start, end, unit, session_id=None, unit_of=None):
    unit_of = unit_of or classifier(store)
    return sum(e.cost_usd for e in store.usage(since=start, until=end, session_id=session_id) if unit_of(e) == unit)


def hourly_history(store: Store, unit, now, hours=HISTORY_HOURS, unit_of=None):
    """Complete hours before the current one: (hour_start, $) oldest first, from the first hour with usage."""
    unit_of = unit_of or classifier(store)
    end = hour_floor(now)
    totals = defaultdict(float)
    for e in store.usage(since=end - hours * HOUR, until=end):
        if unit_of(e) == unit:
            totals[hour_floor(e.ts)] += e.cost_usd
    if not totals:
        return []
    first = min(totals)
    return [(h, totals.get(h, 0.0)) for h in range(first, end, HOUR)]


# ── sample paths ─────────────────────────────────────────────────────────────

def baseline_paths(history, start_hour, hours, n=PATHS, seed=0):
    rng = random.Random(seed)
    days = defaultdict(dict)
    for h, v in history:
        d = datetime.fromtimestamp(h)
        days[d.date()][d.hour] = v
    complete = [p for d, p in days.items() if len(p) >= 20][-28:] or list(days.values())
    if not complete:
        return None
    out = array("d")
    for _ in range(n):
        pick = {}
        for i in range(hours):
            t = datetime.fromtimestamp(start_hour + i * HOUR)
            prof = pick.setdefault(t.date(), rng.choice(complete))
            out.append(prof.get(t.hour, 0.0))
    return out


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _inverse(levels, values, u):
    if u <= levels[0]:
        return values[0]
    for i in range(1, len(levels)):
        if u <= levels[i]:
            lo, hi = levels[i - 1], levels[i]
            return values[i - 1] + (values[i] - values[i - 1]) * (u - lo) / (hi - lo)
    return values[-1]


def copula_paths(quantiles_by_hour, n=PATHS, rho=RHO, seed=0, spread=1.0):
    """quantiles_by_hour: list of {level: value}. Shared factor per path gives hour-to-hour correlation.

    spread widens (>1) or narrows each hour's quantiles around its median; rho and spread are
    calibrated on scored history (`savetokens backtest`) when available.
    """
    rng = random.Random(seed)
    levels = sorted(quantiles_by_hour[0])
    mid = min(levels, key=lambda l: abs(l - 0.5))
    grid = [[max(0.0, q[mid] + spread * (q[l] - q[mid])) for l in levels] for q in quantiles_by_hour]
    for g in grid:   # enforce monotone quantiles
        for i in range(1, len(g)):
            g[i] = max(g[i], g[i - 1])
    out = array("d")
    s = math.sqrt(1 - rho * rho)
    for _ in range(n):
        c = rng.gauss(0, 1)
        for g in grid:
            out.append(_inverse(levels, g, _phi(rho * c + s * rng.gauss(0, 1))))
    return out


SURGE_Q = 0.99


def save_paths(store: Store, source, unit, made_at, start_hour, hours, paths):
    store.conn.execute("INSERT OR REPLACE INTO forecast_paths VALUES (?,?,?,?,?,?,?)",
                       (source, unit, made_at, start_hour, hours, len(paths) // hours, paths.tobytes()))
    store.conn.commit()
    _keep_surge_thresholds(store, source, unit, start_hour, hours, paths)


def _keep_surge_thresholds(store, source, unit, start_hour, hours, paths):
    """Each hour's p99 as forecast, kept for 3 days so the surge rule can look back across refreshes."""
    n = len(paths) // hours
    key = f"surge_p99:{source}:{unit}"
    kept = {int(float(h)): v for h, v in (store.meta(key) or {}).items() if float(h) >= start_hour - 72 * HOUR}
    for i in range(min(hours, 48)):
        v = sorted(paths[k * hours + i] for k in range(n))
        kept[int(start_hour) + i * HOUR] = v[int(SURGE_Q * (n - 1))]
    store.set_meta(key, {str(h): round(v, 4) for h, v in sorted(kept.items())})


def surge_thresholds(store: Store, source, unit) -> dict:
    return {int(float(h)): v for h, v in (store.meta(f"surge_p99:{source}:{unit}") or {}).items()}


def load_paths(store: Store, source, unit):
    row = store.conn.execute("SELECT * FROM forecast_paths WHERE source = ? AND unit = ?", (source, unit)).fetchone()
    if not row:
        return None
    a = array("d")
    a.frombytes(row["data"])
    return {"made_at": row["made_at"], "start": row["start_hour"], "hours": row["hours"], "n": row["n"], "data": a}


def _prefix(paths):
    """Per-path cumulative sums, computed once per loaded paths object."""
    if "cum" not in paths:
        hours, data = paths["hours"], paths["data"]
        cum = []
        for p in range(paths["n"]):
            c, acc, base = array("d", [0.0]), 0.0, p * hours
            for i in range(hours):
                acc += data[base + i]
                c.append(acc)
            cum.append(c)
        paths["cum"] = cum
    return paths["cum"]


def remaining(paths, now, end):
    """Per-path sum over [now, end), counting partial hours by overlap. None if the paths don't reach `end`."""
    if paths is None:
        return None
    if end <= now:
        return [0.0] * paths["n"]
    start, hours, data = paths["start"], paths["hours"], paths["data"]
    if end > start + hours * HOUR + 1:
        return None
    cum = _prefix(paths)

    def F(c, base, x):   # integral of the hourly step function from 0 to x hours
        x = min(max(x, 0.0), hours)
        i = int(x)
        return c[i] + ((x - i) * data[base + i] if i < hours else 0.0)

    a, b = (now - start) / HOUR, (end - start) / HOUR
    return [F(c, p * hours, b) - F(c, p * hours, a) for p, c in enumerate(cum)]


def _q(values, p):
    v = sorted(values)
    i = p * (len(v) - 1)
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def band(values):
    return tuple(_q(values, p) for p in (0.1, 0.5, 0.9))


# ── windows ──────────────────────────────────────────────────────────────────

def latest_reading(store: Store, window, now):
    acc, params = limits.account_filter(store)
    return store.conn.execute(f"SELECT ts, {window}_pct AS pct, {window}_resets AS resets FROM limits"
                              f" WHERE harness = 'claude-code' AND {window}_pct IS NOT NULL AND {window}_resets > ?"
                              f" AND {acc}"
                              f" ORDER BY ts DESC LIMIT 1", (now, *params)).fetchone()


def current_windows(store: Store, now):
    """[(kind, start, end, reading-or-None)] for the windows open now."""
    h = hour_floor(now)
    midnight = _local_midnight(now)
    out = [("hour", h, h + HOUR, None), ("day", midnight, _local_midnight(midnight + 86400 + 7200), None)]
    for kind, window, span in (("five_hour", "five_hour", 5 * HOUR), ("week", "seven_day", 7 * 86400)):
        r = latest_reading(store, window, now)
        if r:
            out.append((kind, r["resets"] - span, r["resets"], r))
        elif kind == "week":   # no reading: a rolling 7 days, local only
            out.append((kind, now - 7 * 86400, now + 7 * 86400 - (now - midnight), None))
    order = {"hour": 0, "five_hour": 1, "day": 2, "week": 3}
    return sorted(out, key=lambda w: order[w[0]])


def forecasts(store: Store, now=None, session_id=None, sources=("ephemeris", "baseline"), day_budget_usd=None):
    """Rows for display: per window, actuals at three levels and forecasts at the end, in $ and limit %."""
    now = now or time.time()
    unit_of = classifier(store)
    wins = current_windows(store, now)
    events = [(e.ts, e.session_id, unit_of(e), e.cost_usd) for e in store.usage(since=min(w[1] for w in wins),
                                                                               until=now + 1)]

    def used(start, unit, sid=None):
        return sum(c for ts, s_, u, c in events if u == unit and ts >= start and (sid is None or s_ == sid))

    rate = {"five_hour": limits.effective_rate(store, "five_hour", now),
            "week": limits.effective_rate(store, "seven_day", now)}
    rate["hour"] = rate["day"] = rate["week"]
    paths = {(s, u): load_paths(store, s, u) for s in sources for u in UNITS}
    rows = []
    for kind, start, end, reading in wins:
        row = {"kind": kind, "start": start, "end": end, "units": {}}
        for unit in UNITS:
            machine = used(start, unit)
            session = used(start, unit, session_id) if session_id else None
            fc, rems = {}, {}
            for source in sources:
                rem = remaining(paths[(source, unit)], now, end)
                if rem is not None and paths[(source, unit)] is not None:
                    fc[source] = tuple(machine + v for v in band(rem))
                    rems[source] = rem
            row["units"][unit] = {"machine": machine, "session": session, "forecast": fc, "rem": rems}
        sub = row["units"]["sub_usd"]
        r = rate.get(kind)
        row["pct"] = None
        if r and (sub["machine"] or reading):
            account = reading["pct"] if reading else None
            # account-level forecast: Anthropic's reading plus the machine's expected further use
            fc, p_hit = {}, {}
            now_pct = account if account is not None else sub["machine"] * r
            for source, b in sub["forecast"].items():
                extra = tuple(v - sub["machine"] for v in b)
                fc[source] = tuple(now_pct + x * r for x in extra)
                rem = sub["rem"][source]
                p_hit[source] = sum(now_pct + x * r >= 100 for x in rem) / len(rem) if rem else 0.0
            row["pct"] = {"rate": r, "account": account, "machine": sub["machine"] * r, "p_hit": p_hit,
                          "session": sub["session"] * r if sub["session"] is not None else None, "forecast": fc,
                          "limit_window": kind in ("five_hour", "week"),
                          "unit_of": "5-hour limit" if kind == "five_hour" else "weekly limit"}
        api = row["units"]["api_usd"]
        if kind == "day" and day_budget_usd:
            api["p_over"] = {src: sum(api["machine"] + x >= day_budget_usd for x in rem) / len(rem) if rem else 0.0
                             for src, rem in api["rem"].items()}
        for u in row["units"].values():
            u.pop("rem", None)
        rows.append(row)
    return rows


# ── refresh, record and score ────────────────────────────────────────────────

def refresh(store: Store, now=None, use_ephemeris=None, log=lambda *_: None):
    """Rebuild baseline paths (always) and Ephemeris paths (when connected); record window forecasts."""
    from . import ephemeris
    now = now or time.time()
    start = hour_floor(now)                       # the current hour is the first forecast step
    week = latest_reading(store, "seven_day", now)
    horizon = int(min(MAX_HORIZON, max(24, math.ceil(((week["resets"] if week else now + 86400 * 7) - start) / HOUR))))
    unit_of = classifier(store)
    histories = {u: hourly_history(store, u, now, unit_of=unit_of) for u in UNITS}
    for unit, hist in histories.items():
        p = baseline_paths(hist, start, horizon) if hist else None
        if p:
            save_paths(store, "baseline", unit, now, start, horizon, p)
    if use_ephemeris is None:
        use_ephemeris = ephemeris.enabled()
    if use_ephemeris:
        series = {u: [round(v, 4) for _, v in h] for u, h in histories.items()
                  if len(h) >= MIN_EPHEMERIS_HOURS and any(h)}
        if series:
            result = ephemeris.forecast_hourly(series, horizon)
            cal = store.meta("ephemeris_calibration") or {}
            for unit, qs in result["quantiles"].items():
                save_paths(store, "ephemeris", unit, now, start, horizon,
                           copula_paths(qs, rho=cal.get("rho", RHO), spread=cal.get("spread", 1.0)))
            store.set_meta("ephemeris_hourly", {"made_at": now, "credits": result["credits"],
                                                "models": result["models"], "horizon": horizon})
            log(f"ephemeris: {horizon}h forecast for {', '.join(series)} ({result['credits']:g} credits)")
    record(store, now)


def record(store: Store, now=None):
    """Store every window forecast so it can be scored when the window ends."""
    now = now or time.time()
    rows = []
    for row in forecasts(store, now):
        for unit, u in row["units"].items():
            for source, b in u["forecast"].items():
                rows.append((source, unit, row["kind"], row["start"], row["end"], now, u["machine"], *b))
    store.conn.executemany("INSERT OR REPLACE INTO window_forecasts VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    store.conn.commit()


def score(store: Store, now=None):
    """Per (kind, unit, source): matured forecasts, interval hit rate, mean pinball loss on the remainder."""
    now = now or time.time()
    unit_of = classifier(store)
    cache = {}
    out = {}
    for r in store.conn.execute("SELECT * FROM window_forecasts WHERE window_end <= ?", (now,)):
        key = (r["window_start"], r["window_end"], r["unit"])
        if key not in cache:
            cache[key] = usage_between(store, r["window_start"], r["window_end"], r["unit"], unit_of=unit_of)
        y = cache[key]
        if y == 0 and r["p90"] == 0:
            continue
        loss = sum(max(p * (y - v), (p - 1) * (y - v)) for p, v in zip((0.1, 0.5, 0.9), (r["p10"], r["p50"], r["p90"])))
        s = out.setdefault((r["kind"], r["unit"], r["source"]), {"n": 0, "hits": 0, "pinball": 0.0})
        s["n"] += 1
        s["hits"] += r["p10"] <= y <= r["p90"]
        s["pinball"] += loss / 3
    for s in out.values():
        s["hit_rate"] = s["hits"] / s["n"]
        s["pinball"] /= s["n"]
    return out


def per_day(store: Store, source, unit, now=None):
    """Forecast (p10, p50, p90) $ for each coming local day, from the paths."""
    now = now or time.time()
    p = load_paths(store, source, unit)
    if not p:
        return []
    out = []
    day = _local_midnight(now)
    while True:
        end = _local_midnight(day + 86400 + 7200)
        rem = remaining(p, max(now, day), end)
        if rem is None:
            break
        out.append((datetime.fromtimestamp(day).date().isoformat(), band(rem)))
        day = end
    return out

