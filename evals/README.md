# savetokens evals

Does an intervention save tokens without costing quality? Each task runs under each
**arm** (a configuration of savetokens) in a fresh workspace, headless, with a hidden
grader. The measure is **cost per successful task**, and an arm ships only if its pass
rate is not worse than control by more than 2 points (paired bootstrap, 95%).

## Pieces

| File | What it does |
| --- | --- |
| `tasks.py` | Built-in tasks: small coding tasks, waste traps, and legitimate-repeat traps (polling, flaky retries) that catch guard false positives |
| `mined.py` | Mines tasks from any repo's history: commits whose new tests fail on the parent and pass on the commit. Exports with `git archive`, never touches the repo |
| `arms.json` | Arms: `hooks` (savetokens guard and steering), `block`, `mode` (the quality knob: `quality`, `balanced`, `lean`), `env`, `append_system_prompt` |
| `run.py` | Runs task × arm × seed with headless Claude Code, any model, a total budget cap and a per-run cap |
| `analyze.py` | Pass rate, $/run, $/success, paired differences against control, guard alerts by task kind |
| `check_tasks.py` | Checks every built-in grader fails as-is and passes with a reference fix |

## Typical use

```sh
# mine 40 tasks from a repo (once; cached in evals/mined/)
python3 evals/mined.py /path/to/repo --python /path/to/repo/.venv/bin/python --limit 40

# run them under each arm on any model, capped at $25 API-equivalent
python3 evals/run.py --source mined:evals/mined/repo.jsonl --arms control,guard,block,fixes \
    --model claude-sonnet-5-5 --budget 25
python3 evals/analyze.py evals/results/run-*.jsonl
```

Levers are measured the same way against the user's default: `--arms opus,opus-low,sonnet,sonnet-low`
and `analyze.py --control opus` give each lever's saving and its cost in passes.

To trace the quality knob's cost and quality curve, run the mode arms against control:
`--arms control,quality,balanced,lean`. Each run's `alerts` field counts briefings
(`briefing:brief`) and guard warnings, so you can confirm the steering was delivered.

Sources: `builtin`, `mined:<jsonl>` (tests visible, test-driven; well specified) and
`mined:<jsonl>:spec` (commit message only; harder, and underspecified when the tests
check exact names).

## Isolation

`--setting-sources project` keeps the user's own settings and hooks out; each run gets
its own `SAVETOKENS_HOME`; no MCP servers; sessions are not persisted. Only the listed
tools are pre-approved (`--permission-mode dontAsk`) inside a temporary workspace.
Graders overwrite the test files before running, so editing tests can't pass a task.

## Known limits

- Single-session tasks: long multi-task sessions (where context carry happens) are not
  modelled yet.
- Graders run the commit's own tests, not the whole suite, so some regressions elsewhere
  go unnoticed.
- One harness (Claude Code). Hermes and Codex need their own runners.
