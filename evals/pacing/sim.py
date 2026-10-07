"""Pacing replay: under a weekly budget, does pacing solve more tasks than the alternatives?

  python3 evals/pacing/sim.py --results evals/pacing/results.jsonl [--ladder glm-5.3-flash,deepseek-v4-flash]
      [--mix mini=0.7,pro=0.3] [--budgets 0.5,0.7,0.9] [--tasks-per-week 100] [--seeds 20]
      [--no-ephemeris] [--json out.json]
  python3 evals/pacing/sim.py --synthetic            # the mechanics on made-up task results

Two measured inputs, nothing modelled:
  - per-task results for each model (one JSONL row per attempt: task, model, passed, cost_usd,
    and optionally set, e.g. "mini" or "pro"), from real benchmark runs (evals/swebench);
  - when work arrives: your real hourly usage (API-equivalent $, savetokens' own database),
    turned into a stream of tasks, each one drawn from the benchmark.

Each week is replayed under each policy. A policy picks a model when a task arrives; the
task's outcome and cost are that model's recorded result on that task. Policies:

  best           always the top of the ladder
  cheapest       always the bottom
  switch@f       top until f of the budget is spent, then the bottom (what gateways do)
  pace:SOURCE    at each task (or every --every hours), the best rung for which the chance of running out before the
                 week ends is at most RISK, using the forecast of the rest of the week's work
                 from SOURCE: burn (this week's rate so far, as usage monitors extrapolate),
                 baseline (savetokens' local seasonal forecast), ephemeris, or perfect
                 (the real remaining work: an upper bound on what forecasting can give)

Assumptions (printed with every result):
  - The budget is a hard cap: once it is spent, the week's remaining tasks are blocked. With
    --soft, work continues on the policy's choice and the overspend is reported instead.
  - The budget is a fixed fraction of what "best" would spend in an average week.
  - Pacing never looks at a task's outcome before choosing; it knows each rung's mean cost and
    pass rate from the benchmark (the same for every task).
  - With --retry, a task a lower rung fails is redone once on the top rung, if budget remains,
    and both attempts are paid for.
  - Heavy users work in long sessions: interactive tasks share one session, so changing its model
    while the prompt cache is warm re-reads the context at full price (--switch-cost); pacers hold
    the model until the cache is cold. Background tasks (--background share) start fresh and move
    down the ladder before interactive ones do.
  - Forecasts are remade every 6 hours from the history up to then (Ephemeris is zero-shot),
    out to the end of the week, and are cached, so reruns cost nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src"))
from analyze import boot  # noqa: E402

HOUR = 3600
WEEK = 7 * 86400
EVERY = 6 * HOUR
RISK = 0.2              # fixed in advance: pace so the chance of running out is at most 20%
PATHS = 200
REFERENCES = ("switch@0.8", "pace:burn")    # the paired comparisons that make the claim


# ── inputs ───────────────────────────────────────────────────────────────────

def load_results(path):
    """{set: {task: {model: [(passed, cost), ...]}}}"""
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for line in open(path):
        if line.strip():
            r = json.loads(line)
            out[r.get("set", "all")][r["task"]][r["model"]].append((float(r["passed"]), float(r["cost_usd"])))
    return out


def synthetic_results(seed=0, n=50):
    """Made-up results for testing: three models, harder tasks fail more and cost more."""
    rng = random.Random(seed)
    models = {"strong": (0.85, 1.0), "mid": (0.65, 0.35), "cheap": (0.45, 0.12)}   # skill, cost scale
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for i in range(n):
        d = rng.random()
        for m, (skill, scale) in models.items():
            for _ in range(2):
                passed = float(rng.random() < max(0.02, skill - 0.6 * d + 0.2))
                out["all"][f"task-{i:02}"][m].append((passed, scale * (0.3 + 1.4 * d) * rng.uniform(0.7, 1.3)))
    return out


def common_tasks(results, ladder):
    """Tasks that have results for every model on the ladder, per set."""
    return {s: sorted(t for t, by in tasks.items() if all(by.get(m) for m in ladder)) for s, tasks in results.items()}


def default_ladder(results):
    """Models by pass rate, best first."""
    rates = defaultdict(list)
    for tasks in results.values():
        for by in tasks.values():
            for m, rs in by.items():
                rates[m] += [p for p, _ in rs]
    return sorted(rates, key=lambda m: -statistics.fmean(rates[m]))


def rung_stats(results, ladder, tasks, mix):
    """Each rung's expected (pass, cost) per arriving task under the task mix; with retry, the cost
    and pass rate of 'this rung, then the top rung if it fails'."""
    out = []
    for m in ladder:
        p = c = pt = ct = 0.0
        for s, w in mix.items():
            rows = [results[s][t] for t in tasks[s]]
            p += w * statistics.fmean(statistics.fmean(x for x, _ in r[m]) for r in rows)
            c += w * statistics.fmean(statistics.fmean(x for _, x in r[m]) for r in rows)
            pt += w * statistics.fmean(statistics.fmean(x for x, _ in r[ladder[0]]) for r in rows)
            ct += w * statistics.fmean(statistics.fmean(x for _, x in r[ladder[0]]) for r in rows)
        out.append({"model": m, "pass": p, "cost": c, "retry_pass": p + (1 - p) * pt,
                    "retry_cost": c + (1 - p) * ct if m != ladder[0] else c})
    return out


def demand_hours(store=None, unit="sub_usd"):
    """{hour: $} of your real usage, API-equivalent."""
    from savetokens import backtest
    from savetokens.store import Store
    store = store or Store()
    return backtest.hourly(backtest.demand(store, unit))


def weeks_of(hours, weekday=0, hour=0, min_history_h=24):
    """Week starts (local time) that are complete and have min_history_h hours of history before them."""
    if not hours:
        return []
    first, last = min(hours), max(hours)
    d = datetime.fromtimestamp(first + min_history_h * HOUR).replace(hour=hour, minute=0, second=0, microsecond=0)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    if d.timestamp() < first + min_history_h * HOUR:
        d += timedelta(days=7)
    out = []
    while (d + timedelta(days=7)).timestamp() <= last + HOUR:
        out.append(d.timestamp())
        d += timedelta(days=7)
    return out


def arrivals(hours, start, end, per_dollar):
    """Task arrival times in [start, end): each hour's $ of real usage becomes per_dollar tasks per $,
    carried over between hours, spread evenly through the hour."""
    out, mass = [], 0.0
    for h in range(int(start), int(end), HOUR):
        mass += hours.get(h, 0.0) * per_dollar
        n = int(mass)
        mass -= n
        out += [h + (i + 0.5) * HOUR / n for i in range(n)]
    return out


# ── forecasts of the rest of the week ────────────────────────────────────────

def _cache_dir():
    from savetokens.store import home
    d = home() / "pacing" / "ephemeris"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ephemeris_quantiles(series, origin, horizon):
    from savetokens import ephemeris
    key = hashlib.sha1(json.dumps([origin, horizon, series]).encode()).hexdigest()[:20]
    path = _cache_dir() / f"{key}.json"
    if path.exists():
        return [{float(k): v for k, v in q.items()} for q in json.loads(path.read_text())], 0.0
    r = ephemeris.forecast_hourly({"u": series}, horizon, retries=6)
    path.write_text(json.dumps(r["quantiles"]["u"]))
    return r["quantiles"]["u"], r["credits"]


def week_forecasts(hours, week_start, sources, log=lambda *_: None):
    """{source: {origin: paths}} for origins every 6 hours through the week, each out to the week's end."""
    from savetokens import backtest, windows
    first = min(hours)
    out = {s: {} for s in sources}
    credits = 0.0
    for o in range(int(week_start), int(week_start + WEEK), EVERY):
        hist = backtest.history_at(hours, o, first)
        if not hist:
            continue
        horizon = int(math.ceil((week_start + WEEK - o) / HOUR))
        seed = int(o) % 100_000
        if "baseline" in sources:
            data = windows.baseline_paths(hist, o, horizon, n=PATHS, seed=seed)
            if data:
                out["baseline"][o] = {"start": o, "hours": horizon, "n": PATHS, "data": data}
        if "ephemeris" in sources:
            series = [round(v, 4) for _, v in hist]
            if len(series) >= windows.MIN_EPHEMERIS_HOURS and any(series):
                try:
                    qs, c = ephemeris_quantiles(series, o, horizon)
                except Exception as e:   # counted: the pacer falls back to the top rung for that stretch
                    log(f"  ephemeris forecast at {time.ctime(o)} failed: {e}")
                    continue
                credits += c
                data = windows.copula_paths(qs, n=PATHS, seed=seed)
                out["ephemeris"][o] = {"start": o, "hours": horizon, "n": PATHS, "data": data}
    return out, credits


def _latest(by_origin, t):
    keys = [o for o in by_origin if o <= t]
    return by_origin[max(keys)] if keys else None


# ── policies ─────────────────────────────────────────────────────────────────

def configs(rungs, background):
    """The pacing ladder as (background rung, interactive rung) steps, best first: background
    work moves all the way down before interactive work moves at all. Each step's expected cost
    per task blends the two by the share of background work."""
    n = len(rungs)
    steps = [(i, 0) for i in range(n)] + [(n - 1, j) for j in range(1, n)]
    out = []
    for bg, ia in steps:
        c = {k: background * rungs[bg][k] + (1 - background) * rungs[ia][k] for k in ("cost", "retry_cost")}
        if out and abs(c["cost"] - out[-1]["cost"]) < 1e-12:
            continue
        out.append({**c, "bg": bg, "ia": ia})
    return out


def pace_plan(spent, budget, remaining_tasks, rungs, retry):
    """A mix of two adjacent rungs, (i, x): a share x of the coming tasks on rung i, the rest on
    rung i + 1, so the rest of the week's expected cost fits the budget left in at least 1 - RISK
    of the forecast's outcomes. remaining_tasks: samples of tasks still to come, this one included."""
    key = "retry_cost" if retry else "cost"
    n = sorted(remaining_tasks)[max(0, math.ceil((1 - RISK) * len(remaining_tasks) - 1e-9) - 1)]
    per_task = (budget - spent) / max(n, 1.0)
    costs = [r[key] for r in rungs]
    if per_task >= costs[0]:
        return 0, 1.0
    for i in range(len(costs) - 1):
        if per_task >= costs[i + 1]:
            return i, (per_task - costs[i + 1]) / (costs[i] - costs[i + 1])
    return len(costs) - 1, 1.0


def make_policies(steps, retry, fc, per_dollar, every_h=0):
    """every_h: pacers re-decide at most this often (0: at every task), as when a person applies
    each suggestion, or the autopilot holds a choice to avoid flip-flopping."""
    bottom = len(steps) - 1

    def held(plan):
        """Turns a plan (i, x) into a rung per task, spreading the share x evenly; the plan is
        remade at most every every_h hours."""
        last, acc = {}, {}

        def decide(t, spent, budget, week):
            k = week["start"]
            if not (every_h and k in last and t - last[k][0] < every_h * HOUR):
                last[k] = (t, plan(t, spent, budget, week))
            i, x = last[k][1]
            acc[k] = acc.get(k, 0.0) + x
            if acc[k] >= 1.0 - 1e-9 or i == bottom:
                acc[k] -= 1.0
                return i
            return i + 1
        return decide

    def pace(source):
        def decide(t, spent, budget, week):
            if source == "perfect":
                rem = [sum(1 for a in week["arrivals"] if a >= t)]
            elif source == "burn":
                done = sum(1 for a in week["arrivals"] if a < t) + 1
                elapsed = max(t - week["start"], HOUR)
                rem = [done / elapsed * (week["start"] + WEEK - t) + 1]
            else:
                from savetokens import windows
                p = _latest(fc.get(source, {}), t)
                dollars = windows.remaining(p, t, week["start"] + WEEK) if p else None
                if dollars is None:
                    return 0, 1.0
                rem = [d * per_dollar + 1 for d in dollars]
            return pace_plan(spent, budget, rem, steps, retry)
        return decide

    out = {"best": lambda t, spent, budget, week: 0,
           "cheapest": lambda t, spent, budget, week: bottom}
    for f in (0.5, 0.8):
        out[f"switch@{f:g}"] = (lambda f: lambda t, spent, budget, week: 0 if spent < f * budget else bottom)(f)
    for source in ("burn", "baseline", "ephemeris", "perfect"):
        if source in ("burn", "perfect") or source in fc:
            out[f"pace:{source}"] = held(pace(source))
    return out


def replay(week, draws, decide, budget, results, ladder, retry, soft, steps=None, switch_cost=0.0,
           cache_ttl_h=1.0, safe=False):
    """One week under one policy. draws: per arrival, (set, task, u1, u2, background) common to every
    policy. decide returns a step of `steps` (see configs); background tasks take its background
    rung, interactive ones its interactive rung.

    Interactive work is one long session, as heavy users work: changing its model while the prompt
    cache is warm (the last interactive task less than cache_ttl_h ago) re-reads the context at the
    new model's full price, switch_cost x that model's mean task cost. With safe, the policy holds
    the interactive model until the cache has gone cold. Background tasks (subagents, exec runs)
    start fresh, so switching them costs nothing."""
    steps = steps or [{"bg": i, "ia": i} for i in range(len(ladder))]
    spent = solved = blocked = switches = penalty = 0.0
    on = [0] * len(ladder)
    last_ia = last_t = None
    mean_cost = {m: statistics.fmean(c for tasks in results.values() for by in tasks.values() if m in by
                                     for _, c in by[m]) for m in ladder}

    def outcome(s, task, model, u):
        rs = results[s][task][model]
        return rs[min(len(rs) - 1, int(u * len(rs)))]

    for t, (s, task, u1, u2, bg) in zip(week["arrivals"], draws):
        if spent >= budget and not soft:
            blocked += 1
            continue
        step = steps[decide(t, spent, budget, week)]
        i = step["bg"] if bg else step["ia"]
        if not bg:
            warm = last_t is not None and t - last_t < cache_ttl_h * HOUR
            if warm and last_ia is not None and i != last_ia:
                if safe:
                    i = last_ia
                else:
                    switches += 1
                    penalty += switch_cost * mean_cost[ladder[i]]
                    spent += switch_cost * mean_cost[ladder[i]]
            last_ia, last_t = i, t
        on[i] += 1
        passed, cost = outcome(s, task, ladder[i], u1)
        spent += cost
        if retry and not passed and i > 0 and (soft or spent < budget):
            passed, cost = outcome(s, task, ladder[0], u2)
            spent += cost
        solved += passed
    n = len(week["arrivals"])
    return {"tasks": n, "solved": solved, "blocked": blocked, "spent": spent, "hit": spent >= budget,
            "over": max(0.0, spent - budget), "switches": switches, "switch_usd": penalty,
            "share": [x / max(1, n - blocked) for x in on]}


# ── the run ──────────────────────────────────────────────────────────────────

def run(results, hours, ladder=None, mix=None, budgets=(0.5, 0.7, 0.9), tasks_per_week=100, seeds=20,
        sources=("baseline", "ephemeris"), retry=False, soft=False, weekday=0, every_h=0, background=0.3,
        switch_cost=0.5, cache_ttl_h=1.0, safe_switch=True, log=lambda *_: None):
    ladder = ladder or default_ladder(results)
    tasks = common_tasks(results, ladder)
    mix = mix or {s: 1.0 for s in tasks if tasks[s]}
    total = sum(mix.values())
    mix = {s: w / total for s, w in mix.items() if tasks.get(s)}
    if not mix:
        raise SystemExit("no task has results for every model on the ladder")
    rungs = rung_stats(results, ladder, tasks, mix)
    steps = configs(rungs, background)
    starts = weeks_of(hours, weekday)
    if not starts:
        raise SystemExit("not enough history for one complete week")
    weekly = [sum(hours.get(h, 0.0) for h in range(int(s), int(s + WEEK), HOUR)) for s in starts]
    per_dollar = tasks_per_week / statistics.fmean(weekly)
    weeks = [{"start": s, "arrivals": arrivals(hours, s, s + WEEK, per_dollar)} for s in starts]
    fc, credits = {}, 0.0
    for w in weeks:
        f, c = week_forecasts(hours, w["start"], sources, log)
        credits += c
        for src, by in f.items():
            fc.setdefault(src, {}).update(by)
    fc = {s: by for s, by in fc.items() if by}
    sets = list(mix)
    draws = []   # common random numbers: seed x week -> per-arrival task and attempt draws
    for seed in range(seeds):
        rng = random.Random(seed)
        draws.append([[(s := rng.choices(sets, [mix[x] for x in sets])[0], rng.choice(tasks[s]),
                        rng.random(), rng.random(), rng.random() < background) for _ in w["arrivals"]] for w in weeks])
    best_cost = statistics.fmean(len(w["arrivals"]) for w in weeks) * (rungs[0]["cost"])
    scenarios = []
    for frac in budgets:
        budget = frac * best_cost
        policies = make_policies(steps, retry, fc, per_dollar, every_h)
        per = {}
        for name, decide in policies.items():
            per[name] = [[replay(w, draws[s][k], decide, budget, results, ladder, retry, soft, steps, switch_cost,
                                 cache_ttl_h, safe=safe_switch and name.startswith("pace:"))
                          for s in range(seeds)] for k, w in enumerate(weeks)]
        rows = {}
        for name, wk in per.items():
            flat = [r for rs in wk for r in rs]
            rows[name] = {"solved_per_week": statistics.fmean(r["solved"] for r in flat),
                          "blocked_per_week": statistics.fmean(r["blocked"] for r in flat),
                          "spent_per_week": statistics.fmean(r["spent"] for r in flat),
                          "weeks_hit": statistics.fmean(r["hit"] for r in flat),
                          "over_per_week": statistics.fmean(r["over"] for r in flat),
                          "switch_usd_per_week": statistics.fmean(r["switch_usd"] for r in flat),
                          "share": [statistics.fmean(r["share"][i] for r in flat) for i in range(len(ladder))]}
        diffs = {}
        for ref in REFERENCES:
            for name in per:
                if name == ref or not name.startswith("pace:"):
                    continue
                d = [statistics.fmean(a["solved"] - b["solved"] for a, b in zip(per[name][k], per[ref][k]))
                     for k in range(len(weeks))]
                h = [statistics.fmean(float(a["hit"]) - b["hit"] for a, b in zip(per[name][k], per[ref][k]))
                     for k in range(len(weeks))]
                diffs[f"{name} vs {ref}"] = {"solved": boot(d), "weeks_hit": boot(h)}
        scenarios.append({"budget_frac": frac, "budget_usd": budget, "policies": rows, "paired": diffs})
    return {"ladder": ladder, "rungs": rungs, "mix": mix, "weeks": len(weeks), "seeds": seeds,
            "tasks_per_week": tasks_per_week, "tasks": {s: len(tasks[s]) for s in mix},
            "best_cost_per_week": best_cost, "every_h": every_h, "background": background,
            "switch_cost": switch_cost, "cache_ttl_h": cache_ttl_h, "safe_switch": safe_switch, "retry": retry, "soft": soft, "risk": RISK,
            "forecasts": {s: len(by) for s, by in fc.items()}, "ephemeris_credits": credits,
            "scenarios": scenarios}


def render(r):
    lines = [f"ladder (best first): " + " → ".join(
                 f"{x['model']} ({x['pass']:.0%} pass, ${x['cost']:.3f}/task)" for x in r["rungs"]),
             f"tasks: " + ", ".join(f"{s} {n} (weight {r['mix'][s]:.0%})" for s, n in r["tasks"].items())
             + f"; {r['tasks_per_week']} tasks in an average week, shaped by your real usage",
             f"{r['weeks']} weeks x {r['seeds']} task draws; pace risk {r['risk']:.0%};"
             f" pacers re-decide {'every ' + format(r['every_h'], 'g') + 'h' if r['every_h'] else 'at every task'};"
             f" {'soft' if r['soft'] else 'hard'} cap; retry on the top rung: {'yes' if r['retry'] else 'no'}",
             f"background work {r['background']:.0%} (moves down first); interactive work is one long session:"
             f" a switch while its cache is warm (< {r['cache_ttl_h']:g}h) costs {r['switch_cost']:g}x a task on the new"
             f" model" + ("; pacers wait for a cold cache" if r["safe_switch"] else ""),
             f"forecasts: " + (", ".join(f"{s} {n}" for s, n in r["forecasts"].items()) or "none")
             + (f" ({r['ephemeris_credits']:g} Ephemeris credits, cached ones free)" if "ephemeris" in r["forecasts"] else ""),
             ""]
    for sc in r["scenarios"]:
        lines.append(f"budget {sc['budget_frac']:.0%} of what 'best' spends in an average week"
                     f" (${sc['budget_usd']:.2f}/week)")
        lines.append(f"  {'policy':16} {'solved/wk':>9} {'blocked':>8} {'$ spent':>8} {'weeks hit':>9}"
                     f" {'$ over':>7} {'$ switch':>8}  share per rung")
        for name, row in sc["policies"].items():
            lines.append(f"  {name:16} {row['solved_per_week']:9.1f} {row['blocked_per_week']:8.1f}"
                         f" {row['spent_per_week']:8.2f} {row['weeks_hit']:9.0%} {row['over_per_week']:7.2f}"
                         f" {row['switch_usd_per_week']:8.2f}  "
                         + " / ".join(f"{x:.0%}" for x in row["share"]))
        lines.append("  paired by week (95% bootstrap over weeks):")
        for name, d in sc["paired"].items():
            m, lo, hi = d["solved"]
            hm, hlo, hhi = d["weeks_hit"]
            win = "WIN" if lo is not None and lo > 0 else ("loss" if hi is not None and hi < 0 else "no clear difference")
            lines.append(f"    {name:30} solved/wk {m:+.1f} [{lo:+.1f}, {hi:+.1f}]   weeks hit {hm:+.0%}"
                         f" [{hlo:+.0%}, {hhi:+.0%}]  -> {win}")
        lines.append("")
    return "\n".join(lines)


def _pairs(text):
    return {k: float(v) for k, v in (x.split("=") for x in text.split(","))}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results", help="per-task results JSONL (see evals/swebench/analyze.py --export)")
    ap.add_argument("--synthetic", action="store_true", help="made-up task results, to check the mechanics")
    ap.add_argument("--ladder", help="models, best first (default: by pass rate)")
    ap.add_argument("--mix", help="task sets and weights, e.g. mini=0.7,pro=0.3 (default: equal)")
    ap.add_argument("--budgets", default="0.5,0.7,0.9")
    ap.add_argument("--tasks-per-week", type=int, default=100)
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--retry", action="store_true", help="redo a failed task once on the top rung")
    ap.add_argument("--soft", action="store_true", help="budget is a target, not a cap")
    ap.add_argument("--no-ephemeris", action="store_true")
    ap.add_argument("--unit", default="sub_usd", choices=("sub_usd", "api_usd"))
    ap.add_argument("--weekday", type=int, default=0, help="day the week starts, 0 = Monday (local time)")
    ap.add_argument("--every", type=float, default=0, help="pacers re-decide at most every N hours (0: every task)")
    ap.add_argument("--background", type=float, default=0.3, help="share of tasks that are background work")
    ap.add_argument("--switch-cost", type=float, default=0.5,
                    help="cost of changing the interactive model while its cache is warm, in that model's mean task costs")
    ap.add_argument("--cache-ttl-h", type=float, default=1.0)
    ap.add_argument("--unsafe-switch", action="store_true", help="pacers switch even while the cache is warm")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    if not (a.results or a.synthetic):
        ap.error("--results or --synthetic")
    results = synthetic_results() if a.synthetic else load_results(a.results)
    sources = ("baseline",) if a.no_ephemeris else ("baseline", "ephemeris")
    r = run(results, demand_hours(unit=a.unit), ladder=a.ladder.split(",") if a.ladder else None,
            mix=_pairs(a.mix) if a.mix else None, budgets=[float(x) for x in a.budgets.split(",")],
            tasks_per_week=a.tasks_per_week, seeds=a.seeds, sources=sources, retry=a.retry, soft=a.soft,
            weekday=a.weekday, every_h=a.every, background=a.background,
            switch_cost=a.switch_cost, cache_ttl_h=a.cache_ttl_h, safe_switch=not a.unsafe_switch, log=lambda m: print(m, file=sys.stderr))
    print(render(r))
    if a.json:
        Path(a.json).write_text(json.dumps(r, indent=2, default=str))


if __name__ == "__main__":
    main()
