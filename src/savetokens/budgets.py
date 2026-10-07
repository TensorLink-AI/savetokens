"""Budgets for pay-as-you-go spend, paced with the forecast.

A budget is dollars per day, week or month for a scope: one provider ("openrouter",
"api.engy.ai", ...), one harness ("hermes"), or all API-billed spend. Config:

  "budgets": [{"name": "openrouter", "usd": 200, "period": "month", "provider": "openrouter"},
              {"name": "hermes", "usd": 15, "period": "day", "harness": "hermes"}]
  "daily_budget_usd": 20          shorthand for all API spend per day

Subscription usage never counts (it has its own limits). Dollars per call come from the
harness: Hermes's own estimator for Hermes calls, which knows the providers it supports.

Pacing: spend so far, the expected total at the period's end (p10/p50/p90), the chance of
going over, and when it would run out. Days and weeks are forecast hour by hour; months
day by day (one hour-by-hour horizon doesn't reach that far). Ephemeris forecasts every
budget's series in one call when connected; the baseline replays past hours or days.
OpenRouter, when connected, reports the key's own spend per day, week and month and its
remaining limit, which replace the local totals.
"""
from __future__ import annotations

import math
import random
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta

from . import windows
from .store import Store, load_config

HOUR, DAY = 3600, 86400
PERIODS = ("day", "week", "month")


def configured(cfg=None) -> list[dict]:
    cfg = cfg or load_config()
    out = [dict(b) for b in cfg.get("budgets") or [] if b.get("usd") and b.get("period") in PERIODS]
    if cfg.get("daily_budget_usd") and not any(b.get("name") == "daily" for b in out):
        out.append({"name": "daily", "usd": cfg["daily_budget_usd"], "period": "day"})
    for b in out:
        b.setdefault("name", f"{b.get('provider') or b.get('harness') or 'all'}-{b['period']}")
    return out


def describe(b) -> str:
    scope = b.get("provider") or b.get("harness") or "all API spend"
    return f"${b['usd']:g}/{b['period']} ({scope})"


def period_bounds(period, now):
    d = datetime.fromtimestamp(now)
    start = datetime(d.year, d.month, d.day)
    if period == "week":
        start -= timedelta(days=start.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        start = start.replace(day=1)
        end = (start.replace(year=start.year + 1, month=1) if start.month == 12
               else start.replace(month=start.month + 1))
    else:
        end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def _matcher(store: Store, b):
    unit_of = windows.classifier(store)

    def match(e):
        if unit_of(e) != "api_usd":
            return False
        if b.get("provider") and (e.provider or "") != b["provider"]:
            return False
        return not b.get("harness") or e.harness == b["harness"]
    return match


def series(store: Store, b, now, step=HOUR, days=28):
    """(step start, $) oldest first for the budget's scope, complete steps before now."""
    match = _matcher(store, b)
    end = (math.floor(now / step) * step) if step == HOUR else period_bounds("day", now)[0]
    start = end - days * DAY
    totals = defaultdict(float)
    for e in store.usage(since=start, until=end):
        if match(e):
            k = (math.floor(e.ts / step) * step) if step == HOUR else period_bounds("day", e.ts)[0]
            totals[k] += e.cost_usd
    if not totals:
        return []
    first = min(totals)
    keys, t = [], first
    while t < end:
        keys.append(t)
        t = t + step if step == HOUR else period_bounds("day", t + DAY / 2 + step)[0]
    return [(k, totals.get(k, 0.0)) for k in keys]


def spent(store: Store, b, now):
    start, _ = period_bounds(b["period"], now)
    match = _matcher(store, b)
    local = sum(e.cost_usd for e in store.usage(since=start, until=now + 1) if match(e))
    reported = (store.meta("openrouter_key") or {}) if b.get("provider") == "openrouter" else {}
    field = {"day": "usage_daily", "week": "usage_weekly", "month": "usage_monthly"}[b["period"]]
    if reported.get(field) is not None and now - reported.get("fetched_at", 0) < 2 * HOUR:
        return float(reported[field]), "openrouter"
    return local, "local"


def _daily_baseline(hist, days_ahead, n=windows.PATHS, seed=0):
    rng = random.Random(seed)
    vals = [v for _, v in hist][-28:] or [0.0]
    return [[rng.choice(vals) for _ in range(days_ahead)] for _ in range(n)]


def _cached_quantiles(store, b, step, start, steps):
    """The cached Ephemeris quantiles from `start` for `steps` steps, if the cache covers them."""
    c = store.meta(f"budget_eph:{b['name']}") or {}
    if c.get("step") != step or c.get("start") is None or start < c["start"]:
        return None
    offset = int(round((start - c["start"]) / step))
    q = c.get("q", [])[offset:offset + steps]
    return [{float(k): v for k, v in x.items()} for x in q] if len(q) == steps else None


def _remaining_samples(store, b, now, source):
    """Samples of further spend from now to the period's end, or None without enough history."""
    _, end = period_bounds(b["period"], now)
    if end - now <= windows.MAX_HORIZON * HOUR:
        hist = series(store, b, now)
        if not hist:
            return None
        start = windows.hour_floor(now)
        hours = int(math.ceil((end - start) / HOUR))
        qs = _cached_quantiles(store, b, HOUR, start, hours) if source == "ephemeris" else None
        if qs:
            data = windows.copula_paths(qs)
        else:
            data = windows.baseline_paths(hist, start, hours)
        if not data:
            return None
        return windows.remaining({"start": start, "hours": hours, "n": len(data) // hours, "data": data}, now, end)
    # months: whole days, plus the rest of today as a share of a day
    hist = series(store, b, now, step=DAY)
    if not hist:
        return None
    today, _ = period_bounds("day", now)
    days = int(round((end - today) / DAY))
    qs = _cached_quantiles(store, b, DAY, today, days) if source == "ephemeris" else None
    if qs:
        flat = windows.copula_paths(qs)
        paths = [flat[i * days:(i + 1) * days] for i in range(len(flat) // days)]
    else:
        paths = _daily_baseline(hist, days, seed=int(today) % 100_000)
    frac_left = 1 - (now - today) / DAY
    return [p[0] * frac_left + sum(p[1:]) for p in paths]


def pace(store: Store, b, now=None) -> dict:
    from . import forecast
    now = now or time.time()
    start, end = period_bounds(b["period"], now)
    used, where = spent(store, b, now)
    source = "ephemeris" if forecast.preferred_source(store) == "ephemeris" and \
        store.meta(f"budget_eph:{b['name']}") else "baseline"
    rem = _remaining_samples(store, b, now, source)
    row = {"name": b["name"], "describe": describe(b), "usd": b["usd"], "period": b["period"], "start": start,
           "resets": end, "spent": round(used, 2), "spent_from": where, "source": source,
           "forecast": None, "p_over": None, "runs_out": None}
    remaining_limit = (store.meta("openrouter_key") or {}).get("limit_remaining") \
        if b.get("provider") == "openrouter" else None
    if rem:
        totals = sorted(used + r for r in rem)
        q = lambda p: totals[int(p * (len(totals) - 1))]
        row["forecast"] = [round(q(0.1), 2), round(q(0.5), 2), round(q(0.9), 2)]
        cap = b["usd"] if remaining_limit is None else min(b["usd"], used + float(remaining_limit))
        row["p_over"] = round(sum(t >= cap for t in totals) / len(totals), 2)
        expected_rate = (q(0.5) - used) / max(1.0, end - now)
        if q(0.5) >= cap and expected_rate > 0:
            row["runs_out"] = now + (cap - used) / expected_rate
    elif used >= b["usd"]:
        row["p_over"] = 1.0
    return row


def pressure_rows(store: Store, now=None, harness=None) -> list[dict]:
    """Budgets in steer.pressure's shape (percent of the budget), for the harnesses they cover."""
    out = []
    for b in configured():
        if harness and b.get("harness") and b["harness"] != harness:
            continue
        if harness == "claude-code" and (b.get("provider") or "") not in ("", "anthropic"):
            continue
        p = pace(store, b, now)
        out.append({"window": f"budget:{b['name']}", "name": f"{describe(b)} budget", "unit": "$",
                    "used": round(100 * p["spent"] / b["usd"], 1),
                    "forecast": [round(100 * v / b["usd"], 1) for v in p["forecast"]] if p["forecast"] else None,
                    "p_hit": p["p_over"], "source": p["source"], "resets": p["resets"], "usd": p["spent"],
                    "runs_out": p["runs_out"]})
    return out


def refresh(store: Store, now=None, use_ephemeris=False, log=lambda *_: None):
    """One Ephemeris call per step size for all budgets; quantiles cached per budget."""
    from . import ephemeris
    now = now or time.time()
    if not use_ephemeris:
        return
    groups = {HOUR: {}, DAY: {}}
    for b in configured():
        _, end = period_bounds(b["period"], now)
        step = HOUR if end - now <= windows.MAX_HORIZON * HOUR else DAY
        hist = series(store, b, now, step=step)
        if len(hist) >= (windows.MIN_EPHEMERIS_HOURS if step == HOUR else 7) and any(v for _, v in hist):
            start = windows.hour_floor(now) if step == HOUR else period_bounds("day", now)[0]
            steps = int(math.ceil((end - start) / step))
            groups[step][b["name"]] = (hist, start, steps)
    for step, items in groups.items():
        if not items:
            continue
        horizon = max(s for _, _, s in items.values())
        r = ephemeris.forecast_hourly({k: [round(v, 4) for _, v in h] for k, (h, _, _) in items.items()}, horizon,
                                      retries=2, freq="H" if step == HOUR else "D")
        for name, qs in r["quantiles"].items():
            store.set_meta(f"budget_eph:{name}", {"step": step, "start": items[name][1], "made_at": now,
                                                  "q": [{str(k): v for k, v in q.items()} for q in qs]})
        log(f"ephemeris: {len(items)} budget forecast(s) by {'hour' if step == HOUR else 'day'}"
            f" ({r['credits']:g} credits)")


# ── OpenRouter: the key's own spend and remaining limit ──────────────────────

def openrouter_key(cfg=None):
    import os
    from pathlib import Path
    cfg = cfg or load_config()
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    path = cfg.get("openrouter_env_file")
    if path:
        try:
            for line in Path(path).expanduser().read_text().splitlines():
                name, _, value = line.strip().removeprefix("export ").partition("=")
                if name.strip() == "OPENROUTER_API_KEY" and value.strip():
                    return value.strip().strip("'\"")
        except OSError:
            return None
    return None


def fetch_openrouter(store: Store, key=None, now=None):
    """GET /api/v1/key: usage (all time), usage_daily/weekly/monthly, limit, limit_remaining."""
    import json
    import urllib.request
    key = key or openrouter_key()
    if not key:
        return None
    req = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    d = d.get("data", d)
    keep = {k: d.get(k) for k in ("usage", "usage_daily", "usage_weekly", "usage_monthly", "limit",
                                  "limit_remaining", "is_free_tier")}
    keep["fetched_at"] = now or time.time()
    store.set_meta("openrouter_key", keep)
    return keep
