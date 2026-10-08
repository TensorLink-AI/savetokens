"""Forecast demand and project each limit to its reset.

Each forecaster makes sample paths of hourly demand (% of the weekly limit):
  ephemeris  hourly quantiles from the Ephemeris ensemble (zero-shot time-series
             foundation models), sampled with a Gaussian copula (correlation RHO
             between hours) so window totals get realistic widths. The default.
  baseline   past days replayed: each future day copies the hourly profile of a
             random recent day. Used when Ephemeris is off or unreachable.

A limit's outlook: % used now (the meter) + demand over the rest of its window,
scaled to that limit. Per path that gives the % at reset and when it would reach
100%; across paths, the range, the chance of a hit and the likely time of it.
"""
from __future__ import annotations

import math
import random
import time
from array import array
from collections import defaultdict
from datetime import datetime

from . import meter
from .meter import HOUR, hour_floor

MAX_HORIZON = 7 * 24
PATHS = 300
RHO = 0.6
QUANTILE_GRID = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)
NAMES = {"five_hour": "5-hour limit", "seven_day": "weekly limit"}


# ── sample paths ─────────────────────────────────────────────────────────────

def baseline_paths(history, start_hour, hours, n=PATHS, seed=0):
    rng = random.Random(seed)
    days = defaultdict(dict)
    for h, v in history:
        d = datetime.fromtimestamp(h)
        days[d.date()][d.hour] = v
    profiles = [p for p in days.values() if len(p) >= 20][-28:] or list(days.values())
    if not profiles:
        return None
    out = array("d")
    for _ in range(n):
        pick = {}
        for i in range(hours):
            t = datetime.fromtimestamp(start_hour + i * HOUR)
            out.append(pick.setdefault(t.date(), rng.choice(profiles)).get(t.hour, 0.0))
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


def copula_paths(quantiles_by_hour, n=PATHS, rho=RHO, seed=0):
    """quantiles_by_hour: [{level: value}]. A shared factor per path correlates its hours."""
    rng = random.Random(seed)
    levels = sorted(quantiles_by_hour[0])
    grid = [[max(0.0, q[l]) for l in levels] for q in quantiles_by_hour]
    for g in grid:
        for i in range(1, len(g)):
            g[i] = max(g[i], g[i - 1])
    out = array("d")
    s = math.sqrt(1 - rho * rho)
    for _ in range(n):
        c = rng.gauss(0, 1)
        for g in grid:
            out.append(_inverse(levels, g, _phi(rho * c + s * rng.gauss(0, 1))))
    return out


def save_paths(store, account, source, made_at, start_hour, hours, data):
    store.conn.execute("INSERT OR REPLACE INTO paths VALUES (?,?,?,?,?,?,?)",
                       (account or "", source, made_at, start_hour, hours, len(data) // hours, data.tobytes()))
    store.conn.commit()


def load_paths(store, account, source):
    row = store.conn.execute("SELECT * FROM paths WHERE account = ? AND source = ?",
                             (account or "", source)).fetchone()
    if not row:
        return None
    a = array("d")
    a.frombytes(row["data"])
    return {"made_at": row["made_at"], "start": row["start_hour"], "hours": row["hours"], "n": row["n"], "data": a}


def walk(paths, now, end, need, scale=1.0, cut=1.0, cut_until=None):
    """Per path: (demand over [now, end) x scale, time it reaches `need` or None). None if paths stop short.

    cut scales demand further until cut_until (a what-if: some usage stops for a while)."""
    start, hours, data, n = paths["start"], paths["hours"], paths["data"], paths["n"]
    if end > start + hours * HOUR + 1 or now < start:
        return None
    out = []
    for p in range(n):
        base, acc, when = p * hours, 0.0, None
        t = now
        while t < end:
            i = int((t - start) // HOUR)
            nxt = min(end, start + (i + 1) * HOUR)
            if cut_until is not None and t < cut_until:
                nxt = min(nxt, cut_until)
            add = data[base + i] * scale * (cut if cut_until is not None and t < cut_until else 1.0) * (nxt - t) / HOUR
            if when is None and need is not None and acc + add >= need:
                frac = (need - acc) / add if add > 0 else 0.0
                when = t + frac * (nxt - t)
            acc += add
            t = nxt
        out.append((acc, when))
    return out


def _q(values, p):
    v = sorted(values)
    i = p * (len(v) - 1)
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


# ── refresh ──────────────────────────────────────────────────────────────────

def ledger_path(store):
    """Gnomon's ledger sits beside the store: savetokens.db -> savetokens.ledger.db."""
    return store.path.with_name(store.path.stem + ".ledger.db")


def refresh(store, now=None, use_ephemeris=True, log=lambda *_: None):
    """New paths for the active account through Gnomon: Ephemeris when it's on, the baseline always.

    Each forecast is recorded in Gnomon's ledger, and the hourly demand so far is appended as
    actuals, so every earlier forecast gets scored. Returns the providers that produced paths.
    """
    from .engine import Engine
    now = now or time.time()
    acct = meter.active_account(store, now)
    hist, _ = meter.demand(store, now)
    if not hist:
        return []
    start = hour_floor(now)
    week = meter.latest(store, "seven_day", now, acct)
    until = week["resets"] if week else now + 7 * 86400
    horizon = int(min(MAX_HORIZON, max(24, math.ceil((until - start) / HOUR))))
    eng = Engine(ledger_path(store), hist, use_ephemeris=use_ephemeris)
    eng.record_actuals(acct, hist, now)
    made = []
    for provider in eng.providers:
        try:
            qs, ex = eng.forecast(provider, acct, hist, start, horizon)
        except Exception as e:   # Ephemeris unreachable or out of credits: the baseline still runs
            log(f"{provider} failed: {e}")
            store.set_meta(f"{provider}_error", {"at": now, "error": str(e)[:300]})
            continue
        save_paths(store, acct, provider, now, start, horizon, copula_paths(qs))
        store.set_meta(f"{provider}_last", {"made_at": now, "horizon": horizon, "history_hours": len(hist),
                                            "execution_id": ex.execution_id,
                                            "credits": (ex.result.metadata or {}).get("credits")})
        log(f"{provider}: {horizon}h forecast from {len(hist)}h of history")
        made.append(provider)
    store.set_meta("forecast_made_at", now)
    try:
        store.set_meta("track_record", {"at": now, **eng.track_record(acct, limit=200)})
    except Exception as e:
        log(f"scoring failed: {e}")
    record(store, now)
    return made


# ── outlook ──────────────────────────────────────────────────────────────────

def preferred_source(store, account=None):
    return "ephemeris" if load_paths(store, account, "ephemeris") else "baseline"


def outlook(store, now=None, source=None, cut=1.0, cut_hours=None):
    """Per limit of the active account: used now, reset, projected % at reset, chance and time of a hit.

    cut, cut_hours: a what-if, demand scaled by `cut` for the next `cut_hours` (0.8, 5: "a fifth of the
    usage stops for the next five hours")."""
    now = now or time.time()
    acct = meter.active_account(store, now)
    source = source or preferred_source(store, acct)
    paths = load_paths(store, acct, source)
    rate = None
    out = []
    for name in ("five_hour", "seven_day"):
        r = meter.latest(store, name, now, acct)
        if not r:
            continue
        scale = meter.five_hour_ratio(store) if name == "five_hour" else 1.0
        used = r["pct"]
        if now - r["ts"] > 600:   # a stale reading: add what was captured since, at the meter's rate
            rate = rate if rate is not None else (meter.rate(store) or 0.0)
            since = store.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE ts > ? AND ts <= ?",
                                       (r["ts"], now)).fetchone()[0]
            used += since * rate * scale
        o = {"name": name, "label": NAMES[name], "account": acct, "used": used, "read_at": r["ts"],
             "resets": r["resets"], "source": source if paths else None, "p10": None, "p50": None, "p90": None,
             "p_hit": 1.0 if used >= 100 else None, "eta": now if used >= 100 else None}
        w = (walk(paths, now, r["resets"], max(0.0, 100 - used), scale, cut,
                  now + cut_hours * HOUR if cut_hours else None) if paths and used < 100 else None)
        if w:
            ends = [used + a for a, _ in w]
            hits = sorted(t for _, t in w if t is not None)
            o.update(p10=_q(ends, 0.1), p50=_q(ends, 0.5), p90=_q(ends, 0.9), p_hit=len(hits) / len(w))
            o["eta"] = hits[len(w) // 2] if len(hits) > len(w) // 2 else None      # the median path's time
            o["eta_early"] = hits[len(w) // 10] if len(hits) > len(w) // 10 else None  # 1 in 10 paths by then
        out.append(o)
    return out


def record(store, now=None):
    """Keep every projection, for scoring and for watching how it moves."""
    now = now or time.time()
    rows = []
    for source in ("ephemeris", "baseline"):
        for o in outlook(store, now, source):
            if o["source"] == source:
                rows.append((o["account"] or "", o["name"], source, now, o["resets"], o["used"],
                             o["p10"], o["p50"], o["p90"], o["p_hit"], o["eta"]))
    store.conn.executemany("INSERT OR REPLACE INTO outlook VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    store.conn.commit()
