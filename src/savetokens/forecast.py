"""Forecast demand and project each limit to its reset.

Each forecaster makes sample paths of hourly demand (% of the weekly limit):
  ephemeris  hourly quantiles from an Ephemeris model (toto2-313m by default, or the
             whole ensemble: zero-shot time-series foundation models), sampled with a Gaussian copula (correlation RHO
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

from . import meter, pools as pools_
from .meter import HOUR, hour_floor

MAX_HORIZON = 7 * 24
WEEK_HOURS = 7 * 24
PATHS = 300
RHO = 0.6
QUANTILE_GRID = (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95)
NAMES = {"five_hour": "5-hour limit", "seven_day": "weekly limit"}
SHORT = {"five_hour": "5-hour", "seven_day": "weekly"}
CODEX_SHORT = {"five_hour": "Codex 5h", "seven_day": "Codex wk"}


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


def walk(paths, now, end, need, scale=1.0, cut=1.0, cut_until=None, extend=False):
    """Per path: (demand over [now, end) x scale, time it reaches `need` or None). None if paths stop short.

    cut scales demand further until cut_until (a what-if: some usage stops for a while). extend: past
    the paths' end, repeat their last week (for a monthly budget; needs a week of paths)."""
    start, hours, data, n = paths["start"], paths["hours"], paths["data"], paths["n"]
    if now < start or (end > start + hours * HOUR + 1 and not (extend and hours >= WEEK_HOURS)):
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
            k = i if i < hours else hours - WEEK_HOURS + (i - hours) % WEEK_HOURS
            add = data[base + k] * scale * (cut if cut_until is not None and t < cut_until else 1.0) * (nxt - t) / HOUR
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
    """New paths for every pool through Gnomon: Ephemeris when it's on, the baseline always.

    Each forecast is recorded in Gnomon's ledger, and each pool's hourly demand so far is appended
    as actuals, so every earlier forecast gets scored. Returns the providers that produced paths.
    """
    from .engine import Engine
    now = now or time.time()
    start = hour_floor(now)
    eng = None
    made = []
    for i, pool in enumerate(pools_.pools(store, now)):
        hist, _ = pools_.history(store, pool, now)
        if not hist:
            continue
        if pool.kind == "api":
            horizon = MAX_HORIZON          # a week, repeated to the end of a longer budget period
        else:
            week = meter.latest(store, "seven_day", now, pool.key if pool.harness == meter.CLAUDE else None,
                                pool.harness)
            until = week["resets"] if week else now + 7 * 86400
            horizon = int(min(MAX_HORIZON, max(24, math.ceil((until - start) / HOUR))))
        eng = eng or Engine(ledger_path(store), use_ephemeris=use_ephemeris)
        _series(store, eng, pool.key, pool.id, hist, now, start, horizon, pool.unit, made, log)
        try:
            tr = {"at": now, **eng.track_record(pool.key, limit=200)}
            store.set_meta(f"track_record:{pool.id}", tr)
            if i == 0:
                store.set_meta("track_record", tr)
        except Exception as e:
            log(f"{pool.id}: scoring failed: {e}")
    # each provider's hourly tokens, for spend by provider and model (a week, repeated over a month)
    from . import spend
    for prov, hist in spend.series(store, now):
        eng = eng or Engine(ledger_path(store), use_ephemeris=use_ephemeris)
        _series(store, eng, spend.key(prov), spend.key(prov), hist, now, start, MAX_HORIZON, spend.UNIT, made, log)
    if eng is None:
        return []
    store.set_meta("forecast_made_at", now)
    record(store, now)
    return made


def _series(store, eng, key, name, hist, now, start, horizon, unit, made, log):
    """One series: its actuals into the ledger, then paths from each forecaster (Ephemeris, then the baseline)."""
    eng.record_actuals(key, hist, now, unit)
    for provider in eng.providers:
        try:
            qs, ex = eng.forecast(provider, key, hist, start, horizon, unit)
        except Exception as e:   # Ephemeris unreachable or out of credits: the baseline still runs
            log(f"{name}: {provider} failed: {e}")
            store.set_meta(f"{provider}_error", {"at": now, "error": str(e)[:300]})
            continue
        save_paths(store, key, provider, now, start, horizon, copula_paths(qs))
        store.set_meta(f"{provider}_last", {"made_at": now, "horizon": horizon, "history_hours": len(hist),
                                            "execution_id": ex.execution_id, "pool": name,
                                            "credits": (ex.result.metadata or {}).get("credits")})
        log(f"{name}: {provider} {horizon}h forecast from {len(hist)}h of history")
        if provider not in made:
            made.append(provider)


# ── outlook ──────────────────────────────────────────────────────────────────

def preferred_source(store, account=None):
    return "ephemeris" if load_paths(store, account, "ephemeris") else "baseline"


def outlook(store, now=None, source=None, cut=1.0, cut_hours=None, pool=None):
    """Per limit of every pool (or just `pool`, an id): used now, reset, projected % at reset, chance
    and time of a hit. For an API pool the limit is its budget, in % of the budget.

    cut, cut_hours: a what-if, demand scaled by `cut` for the next `cut_hours` (0.8, 5: "a fifth of the
    usage stops for the next five hours")."""
    now = now or time.time()
    out = []
    for p in pools_.pools(store, now):
        if pool is None or p.id == pool:
            out += _pool_outlook(store, p, now, source, cut, cut_hours)
    return out


def _project(o, paths, now, need, scale, cut, cut_hours, extend=False):
    w = (walk(paths, now, o["resets"], need, scale, cut, now + cut_hours * HOUR if cut_hours else None, extend)
         if paths and o["used"] < 100 else None)
    if w:
        ends = [o["used"] + a for a, _ in w]
        hits = sorted(t for _, t in w if t is not None)
        o.update(p10=_q(ends, 0.1), p50=_q(ends, 0.5), p90=_q(ends, 0.9), p_hit=len(hits) / len(w))
        o["eta"] = hits[len(w) // 2] if len(hits) > len(w) // 2 else None      # the median path's time
        o["eta_early"] = hits[len(w) // 10] if len(hits) > len(w) // 10 else None  # 1 in 10 paths by then
    return o


def _pool_outlook(store, p, now, source, cut, cut_hours):
    src = source or preferred_source(store, p.key)
    paths = load_paths(store, p.key, src)
    base = {"pool": p.id, "harness": p.harness, "kind": p.kind, "account": p.key,
            "source": src if paths else None, "p10": None, "p50": None, "p90": None}
    if p.kind == "api":
        b = p.budget
        since, until = pools_.period(b, now)
        spent = pools_.spent(store, p, since, now)
        used = 100.0 * spent / b["usd"] if b["usd"] > 0 else 0.0
        per = {"day": "a day", "week": "a week", "month": "a month"}[b["period"]]
        o = {**base, "name": "budget", "label": f"{p.tool} API budget", "short": f"{p.tool.split()[0]} $",
             "used": used, "read_at": now, "resets": until, "budget_usd": b["usd"], "spent_usd": spent,
             "period": b["period"], "per": per, "p_hit": 1.0 if used >= 100 else None,
             "eta": now if used >= 100 else None}
        return [_project(o, paths, now, max(0.0, 100 - used), 100.0 / b["usd"] if b["usd"] > 0 else 0.0,
                         cut, cut_hours, extend=True)]
    acct = p.key if p.harness == meter.CLAUDE else None
    out, rate = [], None
    for name in ("five_hour", "seven_day"):
        r = meter.latest(store, name, now, acct, p.harness)
        if not r:
            continue
        scale = meter.five_hour_ratio(store, harness=p.harness) if name == "five_hour" else 1.0
        used = r["pct"]
        if now - r["ts"] > 600:   # a stale reading: add what was captured since, at the meter's rate
            rate = rate if rate is not None else (meter.rate(store, harness=p.harness) or 0.0)
            since = store.conn.execute(f"SELECT COALESCE(SUM({meter.WEIGHT}), 0) FROM usage WHERE harness = ?"
                                       f" AND {meter.SUBSCRIPTION} AND ts > ? AND ts <= ?",
                                       (p.harness, r["ts"], now)).fetchone()[0]
            used += since * rate * scale
        codex = p.harness != meter.CLAUDE
        o = {**base, "name": name, "label": (f"{p.tool} " if codex else "") + NAMES[name],
             "short": (CODEX_SHORT if codex else SHORT)[name], "used": used, "read_at": r["ts"],
             "resets": r["resets"], "p_hit": 1.0 if used >= 100 else None, "eta": now if used >= 100 else None}
        out.append(_project(o, paths, now, max(0.0, 100 - used), scale, cut, cut_hours))
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
