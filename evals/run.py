"""Layer-1 runner: each task under each arm in a fresh git workspace, headless Claude Code.

General purpose: any task source, any arm (evals/arms.json), any model.

Sources (--source)
  builtin          evals/tasks.py: small tasks and waste / legitimate-repeat traps
  mined:<jsonl>    tasks mined from a repo's history (evals/mined.py); tests visible (test-driven)
  mined:<jsonl>:spec   the same tasks from the commit message only (harder, underspecified)

Arms are defined in evals/arms.json: hooks (savetokens guard and steering), block, mode (the quality
knob), env, append_system_prompt.

Isolation: --setting-sources project keeps the user's own settings (and their savetokens hooks)
out; each run gets its own SAVETOKENS_HOME; no MCP servers; sessions are not persisted.
Permissions: only the listed tools are pre-approved (dontAsk), inside a temporary workspace.

  python3 evals/run.py --arms control,guard --tasks off_by_one --seeds 1 --budget 5
  python3 evals/run.py --source mined:evals/mined/gnomon.jsonl --model claude-haiku-4-5 --limit 10
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tasks import TASKS, materialise  # noqa: E402

HERE = Path(__file__).parent
TOOLS = ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "Task"]
ARMS = json.loads((HERE / "arms.json").read_text())


def settings_for(arm: str, exe: str) -> dict:
    spec = ARMS[arm]
    hook = {"type": "command", "command": f"{exe} hook claude-code", "timeout": 3}
    s: dict = {}
    if spec.get("hooks"):
        events = (["SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop"]
                  + (["PreToolUse"] if spec.get("block") else []))
        s["hooks"] = {e: [{"matcher": "*", "hooks": [hook]}] if "Tool" in e else [{"hooks": [hook]}] for e in events}
    if spec.get("env"):
        s["env"] = dict(spec["env"])
    return s


def load_tasks(source: str):
    if source == "builtin":
        return TASKS
    if source.startswith("mined:"):      # mined:<jsonl>[:spec]
        from mined import load
        parts = source.split(":")
        return load(parts[1], parts[2] if len(parts) > 2 else "tdd")
    raise SystemExit(f"unknown source {source}")


def alerts_in(st_home: Path) -> dict:
    db = st_home / "events.db"
    if not db.exists():
        return {}
    con = sqlite3.connect(db)
    try:
        return {f"{r}:{a}": n for r, a, n in con.execute("SELECT rule, action, COUNT(*) FROM alerts GROUP BY 1, 2")}
    finally:
        con.close()


def run_one(task, arm, seed, model, per_run_usd, exe, keep):
    model = ARMS[arm].get("model", model)      # lever arms pin their own model
    root = Path(tempfile.mkdtemp(prefix=f"st-eval-{task.id}-{arm}-"))
    ws, st_home = root / "ws", root / "st"
    ws.mkdir()
    st_home.mkdir()
    materialise(task, ws)
    if ARMS[arm].get("block"):
        (st_home / "config.json").write_text(json.dumps({"block": True}))
    settings = root / "settings.json"
    settings.write_text(json.dumps(settings_for(arm, exe)))
    cmd = ["claude", "-p", task.prompt, "--output-format", "json", "--model", model,
           "--setting-sources", "project", "--settings", str(settings), "--strict-mcp-config",
           "--permission-mode", "dontAsk", "--allowedTools", *TOOLS,
           "--max-budget-usd", str(per_run_usd), "--no-session-persistence"]
    if ARMS[arm].get("append_system_prompt"):
        cmd += ["--append-system-prompt", ARMS[arm]["append_system_prompt"]]
    env = {**__import__("os").environ, "SAVETOKENS_HOME": str(st_home)}
    env.pop("SAVETOKENS_MODE", None)
    if ARMS[arm].get("mode"):
        env["SAVETOKENS_MODE"] = ARMS[arm]["mode"]
    env.update(ARMS[arm].get("env", {}))       # e.g. CLAUDE_CODE_EFFORT_LEVEL, read at startup
    env.pop("CLAUDECODE", None)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, timeout=1800, env=env, stdin=subprocess.DEVNULL)
        out = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {"is_error": True, "result": p.stderr[-500:]}
    except subprocess.TimeoutExpired:
        out = {"is_error": True, "result": "timeout"}
    except ValueError:
        out = {"is_error": True, "result": "bad json"}
    passed, note = task.grade(ws)
    u = out.get("usage") or {}
    row = {"task": task.id, "kind": task.kind, "arm": arm, "seed": seed, "model": model, "passed": bool(passed),
           "note": str(note)[:200], "cost_usd": out.get("total_cost_usd"), "turns": out.get("num_turns"),
           "duration_s": round(time.time() - t0, 1), "is_error": out.get("is_error"),
           "subtype": out.get("subtype"), "input": u.get("input_tokens"), "output": u.get("output_tokens"),
           "cache_read": u.get("cache_read_input_tokens"), "cache_write": u.get("cache_creation_input_tokens"),
           "alerts": alerts_in(st_home), "result": str(out.get("result", ""))[:300]}
    if not keep:
        shutil.rmtree(root, ignore_errors=True)
        # Claude Code keeps large tool outputs under a project folder named after the workspace
        slug = "".join(c if c.isalnum() else "-" for c in str(ws))
        shutil.rmtree(Path.home() / ".claude" / "projects" / slug, ignore_errors=True)
    else:
        row["dir"] = str(root)
    return row


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="builtin")
    ap.add_argument("--arms", default="control,guard,block,fixes")
    ap.add_argument("--tasks", default="all")
    ap.add_argument("--limit", type=int, default=0, help="use the first N tasks")
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--budget", type=float, default=20.0, help="stop starting new runs past this total (API-equivalent $)")
    ap.add_argument("--per-run", type=float, default=2.0)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--out", default=str(HERE / "results" / f"run-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
    ap.add_argument("--keep", action="store_true", help="keep workspaces for inspection")
    a = ap.parse_args(argv)
    exe = shutil.which("savetokens") or f"{sys.executable} -m savetokens"
    pool_tasks = load_tasks(a.source)
    tasks = pool_tasks if a.tasks == "all" else [t for t in pool_tasks if t.id in a.tasks.split(",")]
    if a.limit:
        tasks = tasks[:a.limit]
    jobs = [(t, arm, s) for s in range(a.seeds) for t in tasks for arm in a.arms.split(",")]
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    spent, done = 0.0, 0
    print(f"{len(jobs)} runs on {a.model}, budget ${a.budget:g} -> {out}", flush=True)
    with ThreadPoolExecutor(a.parallel) as pool:
        pending = {}
        it = iter(jobs)
        stop = False

        def submit():
            nonlocal stop
            if stop:
                return
            job = next(it, None)
            if job:
                pending[pool.submit(run_one, *job, a.model, a.per_run, exe, a.keep)] = job

        for _ in range(a.parallel):
            submit()
        while pending:
            fut = next(as_completed(pending))
            pending.pop(fut)
            row = fut.result()
            spent += row["cost_usd"] or 0
            done += 1
            with open(out, "a") as f:
                f.write(json.dumps(row) + "\n")
            print(f"[{done}/{len(jobs)}] {row['task']:14} {row['arm']:8} {'PASS' if row['passed'] else 'fail'}"
                  f"  ${row['cost_usd'] or 0:.3f}  turns={row['turns']}  alerts={row['alerts']}  total=${spent:.2f}",
                  flush=True)
            if spent >= a.budget:
                stop = True
                print("budget reached: not starting more runs", flush=True)
            submit()
    print(f"done: {done} runs, ${spent:.2f} API-equivalent -> {out}")


if __name__ == "__main__":
    main()
