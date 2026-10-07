"""Backtest: would forecasts have kept you under your limits? Replays your real usage.

Every request in your history is replayed through 5-hour limit windows. At each
checkpoint inside a window, a policy decides whether to economise (lean mode) for
the next half hour. Policies:

  none        never economise
  threshold   economise once 80% of the limit is used (no forecast)
  baseline@r  economise when the local baseline puts P(hit before reset) at r or more
  ephemeris@r the same with the Ephemeris forecast
  oracle      perfect foresight: economise for the whole window iff it would hit the limit

Assumptions, printed with every result:
  - The limit is set so that a given share of your past windows would hit it with no
    steering, which is what the same usage looks like on a smaller plan.
  - A window starts at the first request after the previous window ended.
  - Lean mode cuts usage by `lean` (a fraction) while it is on; the eval measures it.
  - Forecasts are remade every 6 hours from the history available then (the product now
    refreshes hourly while in use, so this understates it), and see the real (unsteered) history.
  - Usage past the limit is blocked work, or with extra usage on, billed at API rates.

Accuracy and cold start: every forecast is also scored on the next 5 and 24 hours
(pinball loss, p10–p90 coverage), and again with history cut to a new user's first days.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import random
import statistics
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import windows
from .store import Store, home

HOUR = 3600
WINDOW = 5 * HOUR
CHECK = 1800
EVERY = 6 * HOUR           # forecasts are remade this often (the product: hourly while in use)
HORIZON = 24
MIN_HISTORY = 7 * 24       # hours of history before the first steady-state origin
LEVELS = (0.1, 0.5, 0.9)
RISKS = (0.2, 0.3, 0.5)    # economise when P(hit the limit before reset) >= risk
HEADLINE_RISK = 0.3        # fixed in advance, so no forecaster is tuned on the test
LABELS = {"none": "no steering", "threshold": "warn at 80% used", "oracle": "perfect foresight",
          "baseline": "baseline", "ephemeris": "Ephemeris", "ephemeris-cal": "Ephemeris calibrated"}
COLD_AGES = (12, 24, 48, 72, 120)   # hours since a simulated new user started


def demand(store: Store, unit="sub_usd"):
    unit_of = windows.classifier(store)
    return sorted((e.ts, e.cost_usd) for e in store.usage() if unit_of(e) == unit)


def hourly(events):
    out = defaultdict(float)
    for ts, c in events:
        out[windows.hour_floor(ts)] += c
    return out


def history_at(hours: dict, origin, first, start=None):
    """Hourly (hour, $) from max(first, start, origin - 28 days) up to origin."""
    lo = max(first, start or first, origin - windows.HISTORY_HOURS * HOUR)
    return [(h, hours.get(h, 0.0)) for h in range(int(lo), int(origin), HOUR)]


def limit_windows(events):
    out, i = [], 0
    while i < len(events):
        s = events[i][0]
        j = i
        while j < len(events) and events[j][0] < s + WINDOW:
            j += 1
        out.append((s, s + WINDOW, events[i:j]))
        i = j
    return out


# ── forecasts at each origin ─────────────────────────────────────────────────

def _paths(source, hist, origin, quantiles=None, rho=windows.RHO, spread=1.0, n=windows.PATHS):
    if source == "baseline":
        data = windows.baseline_paths(hist, origin, HORIZON, n=n, seed=int(origin) % 100_000)
    else:
        data = windows.copula_paths(quantiles, n=n, rho=rho, spread=spread, seed=int(origin) % 100_000)
    return {"start": origin, "hours": HORIZON, "n": len(data) // HORIZON, "data": data} if data else None


def _cache_dir():
    d = home() / "backtest" / "ephemeris"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ephemeris_quantiles(series, origin, log=lambda *_: None):
    """Cached by the exact series sent, so reruns cost nothing."""
    from . import ephemeris
    key = hashlib.sha1(json.dumps([origin, HORIZON, series]).encode()).hexdigest()[:20]
    path = _cache_dir() / f"{key}.json"
    if path.exists():
        return [{float(k): v for k, v in q.items()} for q in json.loads(path.read_text())], 0.0
    r = ephemeris.forecast_hourly({"u": series}, HORIZON, retries=6)
    path.write_text(json.dumps(r["quantiles"]["u"]))
    return r["quantiles"]["u"], r["credits"]


def forecasts_at(hours, first, origins, sources, start=None, workers=2, log=lambda *_: None, rho=windows.RHO,
                 spread=1.0):
    """{source: {origin: paths}}, plus raw Ephemeris quantiles under "_quantiles". `start` cuts history
    to simulate a new user."""
    out = {s: {} for s in sources}
    raw = {}
    hists = {o: history_at(hours, o, first, start) for o in origins}
    if "baseline" in sources:
        for o in origins:
            if hists[o]:
                out["baseline"][o] = _paths("baseline", hists[o], o)
    if "ephemeris" in sources:
        credits, failed = [0.0], [0]

        def one(o):
            series = [round(v, 4) for _, v in hists[o]]
            if len(series) < 12 or not any(series):
                return o, None
            try:
                qs, c = ephemeris_quantiles(series, o)
            except Exception as e:   # counted and reported; the policy falls back to no decision
                failed[0] += 1
                log(f"  ephemeris forecast at {time.ctime(o)} failed: {e}")
                return o, None
            credits[0] += c
            raw[o] = qs
            return o, _paths("ephemeris", None, o, qs, rho=rho, spread=spread)

        done = 0
        with ThreadPoolExecutor(workers) as pool:
            for o, p in pool.map(one, origins):
                done += 1
                if p:
                    out["ephemeris"][o] = p
                if done % 20 == 0:
                    log(f"  ephemeris {done}/{len(origins)} forecasts ({credits[0]:g} credits so far)")
        log(f"  ephemeris: {len(out['ephemeris'])} forecasts, {failed[0]} failed, {credits[0]:g} credits"
            " (cached ones are free)")
        out["_quantiles"] = raw
    return out


def _latest(by_origin, t):
    keys = sorted(by_origin)
    i = bisect.bisect_right(keys, t) - 1
    return by_origin[keys[i]] if i >= 0 else None


# ── steering simulation ──────────────────────────────────────────────────────

def simulate(win, limit, decide, lean):
    """One window under one policy: hit, blocked $, hours economising.

    Lean time counts only half-hours up to the window's last request: economising when
    no work is happening costs nothing.
    """
    s, e, evs = win
    used, on, lean_s, hit_at, blocked = 0.0, False, 0.0, None, 0.0
    checks = [s + k * CHECK for k in range(int(WINDOW // CHECK))]
    ci = 0
    for ts, c in evs:
        while ci < len(checks) and checks[ci] <= ts:
            if hit_at is None:
                on = decide(checks[ci], used, win)
                if on:
                    lean_s += min(CHECK, e - checks[ci])
            ci += 1
        cost = c * (1 - lean) if on else c
        if hit_at is not None:
            blocked += cost
        elif used + cost >= limit:
            blocked += used + cost - limit
            used, hit_at = limit, ts
        else:
            used += cost
    return {"hit": hit_at is not None, "blocked": blocked, "lean_h": lean_s / HOUR}


def policies(fc, limit, sources):
    def forecast_rule(source, risk):
        def decide(t, used, win):
            p = _latest(fc[source], t)
            rem = windows.remaining(p, t, win[1]) if p else None
            if rem is None:
                return False
            return sum(used + r >= limit for r in rem) / len(rem) >= risk
        return decide
    out = {"none": lambda t, used, win: False,
           "threshold": lambda t, used, win: used >= 0.8 * limit}
    for source in sources:
        for risk in RISKS:
            out[f"{source}@{risk:g}"] = forecast_rule(source, risk)
    out["oracle"] = lambda t, used, win: sum(c for _, c in win[2]) > limit
    return out


def label(name):
    source, _, risk = name.partition("@")
    return f"{LABELS[source]} at {float(risk):.0%} risk" if risk else LABELS[name]


def _boot(diffs, n=2000, seed=1):
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(n))
    return statistics.fmean(diffs), means[int(0.025 * n)], means[int(0.975 * n)]


def steering(wins, fc, hit_rates, lean, sources):
    totals = sorted(sum(c for _, c in w[2]) for w in wins)
    scenarios = []
    for rate in hit_rates:
        limit = totals[min(len(totals) - 1, int((1 - rate) * len(totals)))]
        rules = policies(fc, limit, sources)
        per = {p: [simulate(w, limit, rule, lean) for w in wins] for p, rule in rules.items()}
        base_hit = [r["hit"] for r in per["none"]]
        rows = {}
        for p, rs in per.items():
            rows[p] = {"hits": sum(r["hit"] for r in rs), "blocked_usd": round(sum(r["blocked"] for r in rs), 2),
                       "lean_h": round(sum(r["lean_h"] for r in rs), 1),
                       "needless_lean_h": round(sum(r["lean_h"] for r, h in zip(rs, base_hit) if not h), 1)}
        sc = {"hit_rate": rate, "limit_usd": round(limit, 2), "windows": len(wins), "policies": rows}
        b = f"baseline@{HEADLINE_RISK:g}"
        for src in ("ephemeris", "ephemeris-cal"):
            e = f"{src}@{HEADLINE_RISK:g}"
            if e in per and b in per:
                sc[f"{src}_vs_baseline"] = {
                    "risk": HEADLINE_RISK,
                    "blocked_usd": _boot([x["blocked"] - y["blocked"] for x, y in zip(per[e], per[b])]),
                    "hits": _boot([float(x["hit"]) - y["hit"] for x, y in zip(per[e], per[b])]),
                    "lean_h": _boot([x["lean_h"] - y["lean_h"] for x, y in zip(per[e], per[b])])}
        scenarios.append(sc)
    return scenarios


# ── accuracy ─────────────────────────────────────────────────────────────────

def _pinball(samples, y):
    v = sorted(samples)
    loss = 0.0
    for q in LEVELS:
        f = v[int(q * (len(v) - 1))]
        loss += max(q * (y - f), (q - 1) * (y - f))
    lo, hi = v[int(0.1 * (len(v) - 1))], v[int(0.9 * (len(v) - 1))]
    return loss / len(LEVELS), lo <= y <= hi


def accuracy(fc, hours, origins, spans=(5, 24)):
    """{span: {source: {"n", "pinball", "coverage"}}} for the next `span` hours from each origin."""
    out = {}
    for span in spans:
        out[span] = {}
        for source, by in fc.items():
            losses, inside = [], []
            for o in origins:
                p = by.get(o)
                rem = windows.remaining(p, o, o + span * HOUR) if p else None
                if rem is None:
                    continue
                y = sum(hours.get(o + k * HOUR, 0.0) for k in range(span))
                loss, ok = _pinball(rem, y)
                losses.append(loss)
                inside.append(ok)
            if losses:
                out[span][source] = {"n": len(losses), "pinball": round(statistics.fmean(losses), 3),
                                     "coverage": round(sum(inside) / len(inside), 2)}
    return out


# ── runaway detection ────────────────────────────────────────────────────────

SURGE_FLOOR = 1.0       # $ in an hour below which nothing is flagged (as the guard's burn floor)
NAIVE_MULTIPLE = 3.0    # the fixed rule: 3x the usual for this hour of day (as the guard's burn multiple)
RUNAWAYS = (5.0, 10.0, 20.0)
# (hours in a row, forecast quantile): "1h" flags one hour above it, "2h" needs two hours in a row
SURGE_RULES = (("1h", 0.99), ("1h", 1.0), ("2h", 0.95), ("2h", 0.99))


def _naive_threshold(hours, h, days=14):
    same = [hours.get(h - d * 86400, 0.0) for d in range(1, days + 1)]
    return NAIVE_MULTIPLE * statistics.fmean(same)


def _hour_quantiles(p, i, qs):
    v = sorted(p["data"][k * p["hours"] + i] for k in range(p["n"]))
    return {q: v[min(len(v) - 1, int(q * (len(v) - 1)))] for q in qs}


def surge_name(src, kind, q):
    level = "3x usual" if src == "fixed" else ("max" if q == 1.0 else f"p{q * 100:g}")
    return f"{src} {kind}>{level}"


def surges(fc, hours, origins, sizes=RUNAWAYS, seed=7):
    """For each rule: alarms per week on your real usage, and the share of injected runaways
    ($/hour on top of real usage, lasting 2 hours) flagged within those 2 hours."""
    rng = random.Random(seed)
    qs = sorted({q for _, q in SURGE_RULES})
    th = {}
    for o in origins:
        for i in range(EVERY // HOUR):
            h = o + i * HOUR
            row = {"fixed": _naive_threshold(hours, h)}
            for source, by in fc.items():
                p = by.get(o)
                if p:
                    row[source] = _hour_quantiles(p, i, qs)
            th[h] = row

    def fires(src, kind, q, h, extra):
        def over(x):
            t = th.get(x, {}).get(src)
            if t is None:
                return None
            limit = t if src == "fixed" else t[q]
            return hours.get(x, 0.0) + extra.get(x, 0.0) > max(limit, SURGE_FLOOR)
        if kind == "1h":
            return over(h)
        a, b = over(h - HOUR), over(h)
        return None if a is None or b is None else (a and b)

    rules = [(src, kind, q) for src in fc for kind, q in SURGE_RULES] + [("fixed", "1h", None), ("fixed", "2h", None)]
    hs = sorted(th)
    starts = [h for h in hs if h + HOUR in th]     # the whole 2-hour runaway must be covered by forecasts
    sample = rng.sample(starts, min(300, len(starts)))
    out = {}
    for src, kind, q in rules:
        valid = [f for f in (fires(src, kind, q, h, {}) for h in hs) if f is not None]
        if not valid:
            continue
        caught = {}
        for size in sizes:
            n = got = 0
            for h in sample:
                extra = {h: size, h + HOUR: size}
                r = [fires(src, kind, q, x, extra) for x in (h, h + HOUR)]
                if all(x is None for x in r):
                    continue
                n += 1
                got += any(r)
            caught[f"${size:g}/h"] = round(got / n, 2) if n else None
        out[surge_name(src, kind, q)] = {"hours": len(valid), "caught": caught,
                                         "alarms_per_week": round(168 * sum(valid) / len(valid), 1)}
    return out


# ── calibration ──────────────────────────────────────────────────────────────

CAL_RHO = (0.3, 0.6, 0.8, 0.9, 0.95)
CAL_SPREAD = (0.8, 1.0, 1.25, 1.5, 2.0, 2.5)


def _span_loss(by, hours, origins, span):
    out = []
    for o in origins:
        p = by.get(o)
        rem = windows.remaining(p, o, o + span * HOUR) if p else None
        if rem is not None:
            out.append(_pinball(rem, sum(hours.get(o + k * HOUR, 0.0) for k in range(span)))[0])
    return statistics.fmean(out) if out else float("inf")


def calibrate(raw, hours, train, n=200):
    """(rho, spread) for Ephemeris paths that minimise pinball loss on 5h and 24h sums over `train` origins.
    Each span's loss is relative to the uncalibrated setting, so both count equally."""
    def paths(rho, spread):
        return {o: _paths("ephemeris", None, o, raw[o], rho=rho, spread=spread, n=n) for o in train if o in raw}
    ref = paths(windows.RHO, 1.0)
    base = {span: _span_loss(ref, hours, train, span) for span in (5, 24)}
    best = None
    for rho in CAL_RHO:
        for spread in CAL_SPREAD:
            by = paths(rho, spread)
            score = sum(_span_loss(by, hours, train, span) / base[span] for span in (5, 24)) / 2
            if best is None or score < best[0]:
                best = (score, rho, spread)
    return {"rho": best[1], "spread": best[2], "train_origins": len(train), "relative_loss": round(best[0], 3)}


# ── the whole run ────────────────────────────────────────────────────────────

def run(store: Store, hit_rates=(0.1, 0.25, 0.4), lean=0.2, use_ephemeris=True, cold_starts=20,
        log=lambda *_: None):
    events = demand(store)
    if not events:
        return None
    hours = hourly(events)
    first = windows.hour_floor(events[0][0])
    last = events[-1][0]
    sources = ("baseline", "ephemeris") if use_ephemeris else ("baseline",)
    o0 = first + MIN_HISTORY * HOUR
    o0 += (EVERY - o0 % EVERY) % EVERY
    origins = list(range(int(o0), int(last), EVERY))
    log(f"{len(origins)} forecast origins every {EVERY // HOUR}h from {time.ctime(o0)}")
    fc = forecasts_at(hours, first, origins, sources, log=log)
    raw = fc.pop("_quantiles", {})
    split = origins[len(origins) // 2]
    train, test = [o for o in origins if o < split], [o for o in origins if o >= split]
    cal = None
    if raw:
        log("calibrating Ephemeris ranges on the first half of the history")
        cal = calibrate(raw, hours, train)
        cal["fitted_at"] = time.time()
        fc["ephemeris-cal"] = {o: _paths("ephemeris", None, o, q, rho=cal["rho"], spread=cal["spread"])
                               for o, q in raw.items()}
        sources = sources + ("ephemeris-cal",)
    wins = [w for w in limit_windows(events) if w[0] >= o0]
    test_wins = [w for w in wins if w[0] >= split]
    result = {"made_at": time.time(), "from": o0, "to": last, "split": split, "lean": lean, "sources": list(sources),
              "calibration": cal,
              "steering": steering(wins, fc, hit_rates, lean, sources),
              "steering_second_half": steering(test_wins, fc, hit_rates, lean, sources),
              "accuracy": accuracy(fc, hours, origins), "accuracy_second_half": accuracy(fc, hours, test),
              "surges": surges(fc, hours, origins)}
    if cal:
        # adopted for live forecasts only if it beats the uncalibrated ranges on the held-out second half
        held = result["accuracy_second_half"]
        cal["adopted"] = all(held[span]["ephemeris-cal"]["pinball"] < held[span]["ephemeris"]["pinball"]
                             for span in (5, 24) if "ephemeris-cal" in held.get(span, {}))
        if cal["adopted"]:
            store.set_meta("ephemeris_calibration", cal)
        else:
            store.conn.execute("DELETE FROM meta WHERE key = 'ephemeris_calibration'")
            store.conn.commit()
    # cold start: a new user starting on several past days, scored in their first days
    days = sorted({windows.hour_floor(ts) // 86400 * 86400 for ts, _ in events if ts >= o0 and ts < last - 6 * 86400})
    starts = days[:: max(1, len(days) // cold_starts)][:cold_starts]
    cold = {}
    for age in COLD_AGES:
        cfc = {s: {} for s in sources if s != "ephemeris-cal"}
        corigins = []
        for d in starts:
            start = next((windows.hour_floor(ts) for ts, _ in events if ts >= d), None)
            if start is None:
                continue
            o = start + age * HOUR
            corigins.append(o)
            got = forecasts_at(hours, first, [o], tuple(cfc), start=start)
            for s in cfc:
                if o in got[s]:
                    cfc[s][o] = got[s][o]
        cold[age] = accuracy(cfc, hours, corigins)
    result["cold_start"] = {"starts": len(starts), "by_age_hours": cold}
    store.set_meta("backtest", result)
    return result


# ── report ───────────────────────────────────────────────────────────────────

def _diff_line(sc, key, name):
    d = sc.get(key)
    if not d:
        return None
    n = sc["windows"]
    b, h, l = d["blocked_usd"], d["hits"], d["lean_h"]
    return (f"  {name} − baseline at {d['risk']:.0%} risk (95% interval): {h[0] * n:+.0f} hits"
            f" [{h[1] * n:+.0f}, {h[2] * n:+.0f}], ${b[0] * n:+,.0f} past limit [{b[1] * n:+,.0f}, {b[2] * n:+,.0f}],"
            f" {l[0] * n:+.0f} lean hours [{l[1] * n:+.0f}, {l[2] * n:+.0f}]")


def _steering_block(scs, max20x_usd, only=None):
    lines = []
    for sc in scs:
        plan = f"; about 1/{max20x_usd / sc['limit_usd']:.0f} of your Max 20x limit" if max20x_usd else ""
        lines.append(f"limit that {sc['hit_rate']:.0%} of the {sc['windows']} windows would hit unsteered:"
                     f" ${sc['limit_usd']:.0f} per 5 hours{plan}")
        lines.append(f"  {'policy':32} {'limit hits':>10} {'$ past limit':>13} {'hours lean':>11} {'needless':>9}")
        for p, row in sc["policies"].items():
            if only and not only(p):
                continue
            lines.append(f"  {label(p):32} {row['hits']:>10} {row['blocked_usd']:>13,.0f} {row['lean_h']:>11.0f}"
                         f" {row['needless_lean_h']:>9.0f}")
        for key, name in (("ephemeris_vs_baseline", "Ephemeris"), ("ephemeris-cal_vs_baseline", "calibrated")):
            line = _diff_line(sc, key, name)
            if line:
                lines.append(line)
        lines.append("")
    return lines


def _accuracy_lines(acc):
    return [f"  next {span}h: " + "   ".join(f"{s} {v['pinball']:.2f} ({v['coverage']:.0%})" for s, v in by.items())
            for span, by in acc.items()]


def render(r, max20x_usd=None) -> str:
    if not r:
        return "no subscription usage to replay yet"
    day = lambda ts: time.strftime("%d %b", time.localtime(ts))
    lines = [f"backtest: your usage {day(r['from'])} – {day(r['to'])}, replayed through 5-hour limit windows",
             f"assumes lean mode cuts usage by {r['lean']:.0%} while on (try --lean)", ""]
    lines += _steering_block(r["steering"], max20x_usd)
    cal = r.get("calibration")
    if cal:
        lines.append(f"calibration: Ephemeris ranges x{cal['spread']:g} around the median, hour-to-hour correlation"
                     f" {cal['rho']:g}, fitted on {day(r['from'])} – {day(r['split'])}"
                     f" ({cal['train_origins']} forecasts); "
                     + ("adopted for live forecasts" if cal.get("adopted") else
                        "NOT adopted: it doesn't beat the uncalibrated ranges out of sample")
                     + f". Out-of-sample from {day(r['split'])}:")
        lines += _steering_block(r["steering_second_half"], max20x_usd,
                                 only=lambda p: p in ("none", "oracle") or p.endswith(f"@{HEADLINE_RISK:g}"))
        lines.append("  accuracy, second half (pinball loss, lower is better; share inside p10–p90, target 80%):")
        lines += ["  " + x for x in _accuracy_lines(r["accuracy_second_half"])]
        lines.append("")
    lines.append("forecast accuracy, whole period (pinball loss; share of actuals inside p10–p90):")
    lines += _accuracy_lines(r["accuracy"])
    sg = r.get("surges")
    if sg:
        lines.append(f"\nrunaway detection (at least ${SURGE_FLOOR:g} in the hour): alarms per week on your real usage,"
                     " and share of injected 2-hour runaways flagged within them")
        for rule, v in sg.items():
            caught = ", ".join(f"{k} {x:.0%}" for k, x in v["caught"].items() if x is not None)
            lines.append(f"  {rule:26} {v['alarms_per_week']:5.1f}/week   caught: {caught}")
    cs = r.get("cold_start")
    if cs:
        lines.append(f"\nnew user (history cut to their first hours; {cs['starts']} simulated start days), next 24h:")
        for age, acc in cs["by_age_hours"].items():
            by = acc.get(24) or acc.get("24") or {}
            if by:
                lines.append(f"  after {age:>3}h: " + "   ".join(f"{s} {v['pinball']:.2f} ({v['coverage']:.0%})"
                                                              for s, v in by.items()))
    return "\n".join(lines)
