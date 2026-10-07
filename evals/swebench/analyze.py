"""Compare Harbor jobs (one per arm) on the same tasks: pass rate, tokens and $ per solved task.

  python3 evals/swebench/analyze.py off=jobs/st-off on=jobs/st-on [--control off]
      [--price glm-5.3-flash=IN,OUT] [--margin 0.05] [--json out.json]

Each trial's reward comes from SWE-bench's own tests (Harbor's verifier); tokens from Harbor's
record of the agent run. Arms are paired by task: each task's mean over attempts, then the
difference per task, with a 95% paired bootstrap interval. "No quality loss" means the lower
bound of the pass-rate difference is above -margin, fixed before the run (default 5 points).
Dollars use --price (per million input/output tokens), applied to all of an arm's tokens, so
calls served by a fallback model are priced at the main model's rate: the share of trials that
used another model is printed so you can judge that.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analyze import boot  # noqa: E402


def trials(job_dir: Path):
    for p in sorted(Path(job_dir).glob("*/result.json")):
        r = json.loads(p.read_text())
        if "task_name" not in r:
            continue
        a = r.get("agent_result") or {}
        rewards = (r.get("verifier_result") or {}).get("rewards") or {}
        exc = r.get("exception_info")
        yield {"task": r["task_name"], "trial": r.get("trial_name"),
               "reward": float(rewards.get("reward", 0) or 0) if rewards else 0.0,
               "graded": bool(rewards), "error": (exc or {}).get("exception_type") if exc else None,
               "input": a.get("n_input_tokens") or 0, "cache": a.get("n_cache_tokens") or 0,
               "output": a.get("n_output_tokens") or 0, "cost": a.get("cost_usd"),
               "models": sorted((a.get("model_usage") or {}).keys())}


def per_task(rows):
    by = defaultdict(list)
    for r in rows:
        by[r["task"]].append(r)
    return by


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("arms", nargs="+", help="name=job_dir")
    ap.add_argument("--control")
    ap.add_argument("--price", help="model=IN,OUT dollars per million tokens")
    ap.add_argument("--margin", type=float, default=0.05)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    arms = dict(x.split("=", 1) for x in a.arms)
    control = a.control or next(iter(arms))
    price = None
    if a.price:
        _, _, v = a.price.partition("=")
        pin, pout = (float(x) for x in v.split(","))
        price = lambda r: (r["input"] * pin + r["output"] * pout) / 1e6  # noqa: E731
    data = {name: per_task(list(trials(Path(d)))) for name, d in arms.items()}
    common = sorted(set.intersection(*(set(v) for v in data.values())))
    print(f"{len(common)} tasks in every arm; control: {control}\n")
    if not common:
        raise SystemExit("no task has results in every arm: check the job directories")
    print(f"{'arm':12} {'trials':>6} {'pass':>6} {'errors':>6} {'tok/task':>10} {'tok/solved':>11}"
          + (f" {'$/task':>8} {'$/solved':>9}" if price else "") + "  models seen")
    summary = {}
    for name, by in data.items():
        rows = [r for t in common for r in by[t]]
        if not rows:
            continue
        solved = sum(r["reward"] for r in rows)
        tok = sum(r["input"] + r["output"] for r in rows)
        line = (f"{name:12} {len(rows):6} {solved / len(rows):6.0%} {sum(1 for r in rows if r['error']):6}"
                f" {tok / len(rows):10,.0f} {tok / max(solved, 1):11,.0f}")
        usd = sum(price(r) for r in rows) if price else None
        if price:
            line += f" {usd / len(rows):8.3f} {usd / max(solved, 1):9.3f}"
        models = defaultdict(int)
        for r in rows:
            for m in r["models"]:
                models[m] += 1
        print(line + "  " + ", ".join(f"{m} ({n})" for m, n in models.items()))
        summary[name] = {"trials": len(rows), "pass": solved / len(rows), "tokens_per_task": tok / len(rows),
                         "tokens_per_solved": tok / max(solved, 1), "usd": usd, "models": dict(models)}
    print(f"\npaired by task against {control} (95% bootstrap), no-loss margin {a.margin:.0%}:")
    for name, by in data.items():
        if name == control:
            continue
        mean = lambda rs, f: statistics.mean(f(r) for r in rs)  # noqa: E731
        dp = [mean(by[t], lambda r: r["reward"]) - mean(data[control][t], lambda r: r["reward"]) for t in common]
        tok = lambda r: r["input"] + r["output"]  # noqa: E731
        dt = [mean(by[t], tok) - mean(data[control][t], tok) for t in common]
        base = statistics.mean(mean(data[control][t], tok) for t in common) or 1
        mp, lp, hp = boot(dp)
        mt, lt, ht = boot(dt)
        if mp is None:
            continue
        verdict = "no quality loss" if lp > -a.margin else "can't rule out a quality loss"
        print(f"  {name:12} Δpass {mp:+.1%} [{lp:+.1%}, {hp:+.1%}]   Δtokens/task {mt / base:+.1%}"
              f" [{lt / base:+.1%}, {ht / base:+.1%}]   n={len(common)} -> {verdict}")
        summary[name].update(d_pass=[mp, lp, hp], d_tokens_rel=[mt / base, lt / base, ht / base], verdict=verdict)
    if a.json:
        Path(a.json).write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
