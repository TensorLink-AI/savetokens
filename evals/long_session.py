"""Long-session eval: several tasks in one session, so context carries over as it does in real use.

Each chain puts K mined tasks in separate folders of one workspace (task1/, task2/, ...) and runs
them in order in a single headless Claude Code session (`--resume`). Each task is graded right
after its turn. Arms differ only in how context is managed:

  carry        one session, the harness's default auto-compaction (near the full window)
  compact-Nk   one session, autoCompactWindow = N thousand tokens
  clear        a fresh session per task (the same as /clear between tasks): the reference for
               how much carried context costs, and whether carrying it helps quality

The measure is the same as layer 1: cost per successful task, and the pass rate must not drop
by more than 2 points. Sessions are persisted (needed for --resume) under workspaces named
st-eval-*, which savetokens' own backfill ignores; they are deleted after reading.

  python3 evals/long_session.py --chains 2 --length 3 --arms carry,clear --budget 5
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mined import load  # noqa: E402

HERE = Path(__file__).parent
TOOLS = ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "Task"]
ARMS = {"carry": {}, "clear": {"fresh": True},
        "compact-150k": {"settings": {"autoCompactWindow": 150_000}},
        "compact-100k": {"settings": {"autoCompactWindow": 100_000}}}
SKIP = {"gnomon-32edc922"}   # fails under every arm in layer 1: a task problem, not a signal


def claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def transcript(session_id):
    hits = list((claude_home() / "projects").glob(f"*st-eval-*/{session_id}.jsonl"))
    return hits[0] if hits else None


def session_stats(paths):
    """Compactions and peak context from the session transcripts."""
    compactions, peak = 0, 0
    for p in paths:
        if not p or not p.exists():
            continue
        for line in open(p, "rb"):
            if b"compact_boundary" in line:
                compactions += 1
            elif b'"usage"' in line:
                try:
                    u = json.loads(line)["message"]["usage"]
                    peak = max(peak, sum(int(u.get(k) or 0) for k in
                                         ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
                except (KeyError, ValueError, TypeError):
                    pass
    return compactions, peak


def run_chain(chain_id, tasks, arm, model, per_task_usd, keep):
    spec = ARMS[arm]
    root = Path(tempfile.mkdtemp(prefix=f"st-eval-chain{chain_id}-{arm}-"))
    ws = root / "ws"
    ws.mkdir()
    for i, t in enumerate(tasks):
        t.setup(ws / f"task{i + 1}")
    settings = root / "settings.json"
    settings.write_text(json.dumps(spec.get("settings", {})))
    env = {**os.environ}
    env.pop("CLAUDECODE", None)
    sid, sessions, rows = None, [], []
    for i, t in enumerate(tasks):
        d = f"task{i + 1}"
        prompt = (f"Next task. It is in the directory {d}/ (its own checkout of the repository); run commands"
                  f" from there, e.g. `cd {d} && ...`, and don't change the other task directories.\n\n{t.prompt}")
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model, "--setting-sources", "project",
               "--settings", str(settings), "--strict-mcp-config", "--permission-mode", "dontAsk",
               "--allowedTools", *TOOLS, "--max-budget-usd", str(per_task_usd)]
        if sid and not spec.get("fresh"):
            cmd += ["--resume", sid]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=1800, env=env,
                               stdin=subprocess.DEVNULL)
            out = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {"is_error": True,
                                                                                  "result": p.stderr[-300:]}
        except (subprocess.TimeoutExpired, ValueError):
            out = {"is_error": True, "result": "timeout or bad json"}
        sid = out.get("session_id") or sid
        if out.get("session_id") and out["session_id"] not in sessions:
            sessions.append(out["session_id"])
        passed, note = t.grade(ws / d)
        u = out.get("usage") or {}
        rows.append({"chain": chain_id, "step": i + 1, "task": t.id, "arm": arm, "model": model,
                     "passed": bool(passed), "note": str(note)[:200], "cost_usd": out.get("total_cost_usd"),
                     "turns": out.get("num_turns"), "duration_s": round(time.time() - t0, 1),
                     "is_error": out.get("is_error"), "cache_read": u.get("cache_read_input_tokens"),
                     "cache_write": u.get("cache_creation_input_tokens"), "output": u.get("output_tokens")})
    paths = [transcript(s) for s in sessions]
    compactions, peak = session_stats(paths)
    for r in rows:
        r.update(chain_compactions=compactions, chain_peak_context=peak)
    if not keep:
        for p in paths:
            if p:
                shutil.rmtree(p.parent, ignore_errors=True)   # the st-eval project folder of this workspace
        shutil.rmtree(root, ignore_errors=True)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(HERE / "mined" / "gnomon.jsonl"))
    ap.add_argument("--chains", type=int, default=6)
    ap.add_argument("--length", type=int, default=6, help="tasks per session")
    ap.add_argument("--arms", default="carry,compact-150k,compact-100k,clear")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--budget", type=float, default=40.0)
    ap.add_argument("--per-task", type=float, default=3.0)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--out", default=str(HERE / "results" / f"long-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    tasks = [t for t in load(a.source) if t.id not in SKIP]
    chains = [tasks[i * a.length:(i + 1) * a.length] for i in range(a.chains)]
    chains = [c for c in chains if len(c) == a.length]
    jobs = [(j, c, arm) for j, c in enumerate(chains) for arm in a.arms.split(",")]
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"{len(jobs)} chains of {a.length} tasks on {a.model}, budget ${a.budget:g} -> {out}", flush=True)
    spent = 0.0
    with ThreadPoolExecutor(a.parallel) as pool:
        pending, it, stop = {}, iter(jobs), False

        def submit():
            job = None if stop else next(it, None)
            if job:
                pending[pool.submit(run_chain, *job, a.model, a.per_task, a.keep)] = job

        for _ in range(a.parallel):
            submit()
        while pending:
            fut = next(as_completed(pending))
            j, _, arm = pending.pop(fut)
            rows = fut.result()
            cost = sum(r["cost_usd"] or 0 for r in rows)
            spent += cost
            with open(out, "a") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
            print(f"chain {j} {arm:13} {sum(r['passed'] for r in rows)}/{len(rows)} passed  ${cost:.2f}"
                  f"  compactions={rows[0]['chain_compactions']}  peak={rows[0]['chain_peak_context'] // 1000}k"
                  f"  total=${spent:.2f}", flush=True)
            if spent >= a.budget:
                stop = True
                print("budget reached: not starting more chains", flush=True)
            submit()
    print(f"done: ${spent:.2f} -> {out}")


def analyze(paths, control="carry"):
    """Per arm: pass rate, $ per chain, $ per success; paired by chain against the control arm."""
    sys.path.insert(0, str(HERE))
    from analyze import MARGIN, boot
    rows = [json.loads(line) for p in paths for line in open(p) if line.strip()]
    by = {}
    for r in rows:
        by.setdefault((r["arm"], r["chain"]), []).append(r)
    arms = sorted({a for a, _ in by}, key=lambda a: (a != control, a))
    chains = sorted({c for _, c in by})
    print(f"{len(chains)} chains, arms: {', '.join(arms)}\n")
    print(f"{'arm':14} {'pass':>6} {'$/chain':>9} {'$/success':>10} {'compactions':>12} {'peak ctx':>9}")
    for a in arms:
        rs = [r for c in chains for r in by.get((a, c), [])]
        if not rs:
            continue
        ok = sum(r["passed"] for r in rs)
        cost = sum(r["cost_usd"] or 0 for r in rs)
        n = len({r["chain"] for r in rs})
        comp = sum(by[(a, c)][0]["chain_compactions"] for c in chains if (a, c) in by)
        peak = max(by[(a, c)][0]["chain_peak_context"] for c in chains if (a, c) in by)
        print(f"{a:14} {ok / len(rs):6.0%} {cost / n:9.2f} {cost / max(ok, 1):10.2f} {comp:12} {peak // 1000:8}k")
    print(f"\npaired by chain against {control} (95% bootstrap):")
    for a in arms:
        if a == control:
            continue
        common = [c for c in chains if (a, c) in by and (control, c) in by]
        dp = [sum(r["passed"] for r in by[(a, c)]) / len(by[(a, c)])
              - sum(r["passed"] for r in by[(control, c)]) / len(by[(control, c)]) for c in common]
        dc = [sum(r["cost_usd"] or 0 for r in by[(a, c)]) - sum(r["cost_usd"] or 0 for r in by[(control, c)])
              for c in common]
        mp, lp, hp = boot(dp)
        mc, lc, hc = boot(dc)
        verdict = "no quality loss" if lp is not None and lp > -MARGIN else "possible quality loss"
        print(f"  {a:14} Δpass {mp:+.0%} [{lp:+.0%}, {hp:+.0%}]   Δ$/chain {mc:+.2f} [{lc:+.2f}, {hc:+.2f}]"
              f"   n={len(common)} -> {verdict}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["analyze"]:
        args = sys.argv[2:]
        control = "carry"
        if "--control" in args:
            i = args.index("--control")
            control = args[i + 1]
            del args[i:i + 2]
        analyze(args, control)
    else:
        main()
