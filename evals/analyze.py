"""Layer-1 analysis: each arm against control, paired by task and seed.

Ship rule (per arm): pass rate not worse than control by more than MARGIN (lower bound of the
paired bootstrap 95% interval above -MARGIN), and cost per successful task lower.

  python3 evals/analyze.py evals/results/pilot-1.jsonl
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict

MARGIN = 0.02
B = 4000


def load(paths):
    rows = []
    for p in paths:
        rows += [json.loads(l) for l in open(p) if l.strip()]
    return rows


def boot(diffs, seed=0):
    """Paired bootstrap 95% interval of the mean difference."""
    if not diffs:
        return None, None, None
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(diffs, k=len(diffs))) for _ in range(B))
    return statistics.mean(diffs), means[int(0.025 * B)], means[int(0.975 * B) - 1]


def main(paths, control="control"):
    rows = load(paths)
    rows = [{**r, "arm": "control"} if r["arm"] == control else r for r in rows]
    by = {(r["task"], r["seed"], r["arm"]): r for r in rows}
    arms = sorted({r["arm"] for r in rows}, key=lambda a: (a != "control", a))
    kinds = {r["task"]: r["kind"] for r in rows}
    print(f"{len(rows)} runs, {len(kinds)} tasks, model {rows[0]['model']}\n")
    print(f"{'arm':8} {'pass':>6} {'mean $':>8} {'$ / success':>12} {'turns':>6} {'tokens/run':>11}")
    summary = {}
    for arm in arms:
        rs = [r for r in rows if r["arm"] == arm]
        cost = sum(r["cost_usd"] or 0 for r in rs)
        wins = sum(r["passed"] for r in rs)
        toks = statistics.mean((r["input"] or 0) + (r["cache_read"] or 0) + (r["cache_write"] or 0) + (r["output"] or 0)
                               for r in rs)
        summary[arm] = {"pass": wins / len(rs), "cps": cost / wins if wins else float("inf")}
        print(f"{arm:8} {wins / len(rs):6.0%} {cost / len(rs):8.3f} {summary[arm]['cps']:12.3f}"
              f" {statistics.mean(r['turns'] or 0 for r in rs):6.1f} {toks:11,.0f}")

    print("\npaired against control (mean difference, 95% bootstrap interval over task×seed pairs):")
    for arm in arms:
        if arm == "control":
            continue
        pairs = [(by[(t, s, "control")], by[(t, s, arm)]) for (t, s, a) in by if a == arm and (t, s, "control") in by]
        dp = [int(b["passed"]) - int(a["passed"]) for a, b in pairs]
        dc = [(b["cost_usd"] or 0) - (a["cost_usd"] or 0) for a, b in pairs]
        mp, lp, hp = boot(dp)
        mc, lc, hc = boot(dc, 1)
        quality_ok = lp is not None and lp > -MARGIN
        cheaper = summary[arm]["cps"] < summary["control"]["cps"]
        verdict = ("ship" if quality_ok and cheaper else
                   "quality not shown non-inferior" if not quality_ok else "no cost gain")
        print(f"  {arm:8} Δpass {mp:+.0%} [{lp:+.0%}, {hp:+.0%}]   Δ$/run {mc:+.3f} [{lc:+.3f}, {hc:+.3f}]"
              f"   n={len(pairs)}  -> {verdict}")

    print("\nper task (passes out of seeds; mean $):")
    tasks = sorted(kinds, key=lambda t: (kinds[t], t))
    print(f"  {'task':15} {'kind':7} " + " ".join(f"{a:>14}" for a in arms))
    for t in tasks:
        cells = []
        for a in arms:
            rs = [r for r in rows if r["task"] == t and r["arm"] == a]
            if rs:
                cells.append(f"{sum(r['passed'] for r in rs)}/{len(rs)} ${statistics.mean(r['cost_usd'] or 0 for r in rs):.2f}"
                             .rjust(14))
            else:
                cells.append(" " * 14)
        print(f"  {t:15} {kinds[t]:7} " + " ".join(cells))

    print("\nguard alerts (rule:action -> count) by task kind:")
    fired = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for k, n in (r.get("alerts") or {}).items():
            fired[(r["arm"], r["kind"])][k] += n
    for (arm, kind), d in sorted(fired.items()):
        print(f"  {arm:6} {kind:7} {dict(d)}")
    legit_alerts = sum(sum(d.values()) for (arm, kind), d in fired.items() if kind == "legit")
    other_alerts = sum(sum(d.values()) for (arm, kind), d in fired.items() if kind != "legit")
    print(f"  on legitimate-repeat tasks: {legit_alerts}  (these are false positives by construction)")
    print(f"  on other tasks: {other_alerts}")
    errs = [r for r in rows if r.get("is_error")]
    if errs:
        print(f"\n{len(errs)} runs ended in error: " + ", ".join(f"{r['task']}/{r['arm']}: {r['subtype']}" for r in errs[:8]))


if __name__ == "__main__":
    args = sys.argv[1:]
    control = "control"
    if "--control" in args:      # compare against another arm, e.g. the default model for lever evals
        i = args.index("--control")
        control = args[i + 1]
        del args[i:i + 2]
    main(args, control)
