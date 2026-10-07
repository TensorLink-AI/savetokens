# Benchmark: savetokens on Hermes, SWE-bench Verified Mini

You are running a benchmark on this machine: does savetokens cut the tokens Hermes spends on real
coding tasks without lowering the share of tasks it solves? Work through the steps in order, and
reply with the report at the bottom.

- **Tasks:** SWE-bench Verified Mini, a published subset of 50 tasks from SWE-bench Verified
  that keeps the full set's pass rates and difficulty. Each task is a real GitHub issue in a real
  repo, graded by its own tests.
- **Runner:** Harbor, the official harness for Terminal-Bench, which also runs SWE-bench. It
  includes a Hermes agent. Each task runs in its own Docker container with a fresh Hermes inside.
- **Model:** `glm-5.3-flash` on Engy (`https://api.engy.ai/v1`), with `deepseek-v4-flash`, also on
  Engy, as the fallback.
- **Arms:** `off` (Hermes alone) and `on` (Hermes plus savetokens in mode lean, its Hermes levers
  allowed). Both arms run the same tasks with the same settings.

Rules:
- Never print or copy API keys or the contents of `.env` files. Pass keys only through
  environment variables.
- Don't change this machine's own Hermes config. The benchmark runs Hermes inside containers,
  with its own config there.
- If you are running inside the Hermes gateway, don't restart it. Start long runs detached
  (`setsid nohup ... &`), so they survive your session ending, and check back on them.
- Budget: stop and report if the projected model spend for the whole run is over **$25**, or
  if the actual spend passes it.
- Start each run once. Don't re-run a step just to get a better result. Report failures as
  they are.

## 0. Prerequisites

```sh
uname -m                         # x86_64 expected (arm64 works under emulation, much slower)
docker info --format '{{.ServerVersion}}'   # Docker must be running and usable without sudo
df -h ~ | tail -1                # 15 GB or more free
free -g | head -2                # 8 GB or more of RAM (about 2-4 GB per concurrent task)
uv --version
```

If Docker isn't available, stop here and report that.

## 1. Get the tools

```sh
uv tool install harbor          # or: uv tool upgrade harbor
harbor --version                # 0.24 or newer
git clone -b hermes-test https://github.com/TensorLink-AI/savetokens ~/savetokens-bench
mkdir -p ~/st-bench
```

## 2. Keys and models

Harbor passes two variables into each container: `OPENAI_API_KEY` and `OPENAI_BASE_URL`. Set them
in the shell that will run Harbor, without echoing the key. Load it from wherever this machine
keeps the Engy key:

```sh
export OPENAI_BASE_URL=https://api.engy.ai/v1
export OPENAI_API_KEY="$(...)"   # load the Engy key here, without printing it
test -n "$OPENAI_API_KEY" && echo KEY-SET
curl -s "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY" \
  | python3 -c "import json,sys; ids=[m['id'] for m in json.load(sys.stdin)['data']]; print([i for i in ids if 'glm-5.3' in i or 'deepseek-v4' in i])"
```

Check 2: both `glm-5.3-flash` and `deepseek-v4-flash` are listed. If Engy uses slightly different
ids, use the exact ids it lists from here on, and report them.

Note each model's price per million input and output tokens. Take them from Engy's model list or
pricing page, or from savetokens' snapshot on this machine:
`python3 -c "import sqlite3,os;print(sqlite3.connect(os.path.expanduser('~/.savetokens/events.db')).execute(\"SELECT value FROM meta WHERE key='hermes_prices'\").fetchone())"`

## 3. The fallback model

Write `~/st-bench/fallback.yaml`: a fragment of Hermes config that makes `deepseek-v4-flash` on the
same Engy endpoint the fallback. Check the right format against this machine's Hermes version:
its docs, `hermes config --help`, or how fallbacks are written in your own config. The fragment:

- is appended to a config whose main model runs on Hermes's `openai-api` provider, with
  `OPENAI_API_KEY` and `OPENAI_BASE_URL` set;
- must reach `deepseek-v4-flash` using only those two variables. **Don't put a key in the file.**
- must contain no top-level keys other than the fallback's own (the main config already has
  `model`, `provider`, `toolsets`, `memory`, `terminal`, `delegation` and `checkpoints`).

Test that the fallback works, in a throwaway Hermes home, so your own config isn't touched. Use a
model id that doesn't exist, so the main model fails:

```sh
export T=$(mktemp -d)
printf 'model: no-such-model-xyz\nprovider: openai-api\n' > $T/config.yaml
cat ~/st-bench/fallback.yaml >> $T/config.yaml
HERMES_HOME=$T hermes -z "Reply with the single word ok." -m no-such-model-xyz --provider openai-api
rm -rf $T
```

Check 3: the reply comes back from `deepseek-v4-flash`. Report the fragment, which has no keys in it.
If you can't get a fallback to work after a few honest tries, write an empty
`~/st-bench/fallback.yaml`, carry on without a fallback, and say so in the report.

## 4. Check Harbor and Docker (free: no model calls)

Harbor's `oracle` agent applies each task's known fix, which checks Docker, the task images and
the grader:

```sh
cd ~/st-bench
harbor run -d swebench-verified@1.0 -a oracle -i django__django-15098 -n 1 -o jobs --job-name oracle -y
cat jobs/oracle/*/result.json | python3 -c "import json,sys; r=json.load(sys.stdin); print(r['task_name'], r['verifier_result'])"
```

Check 4: the reward is 1.

If Harbor fails with `Error getting dataset swebench-verified@1.0`, it can't reach its own hub.
Use the registry file from GitHub instead: download it, add `--registry-path ~/st-bench/registry.json`
to the oracle command above, and add the same flag to every `make_jobs.py` command below.

```sh
curl -fsSL https://raw.githubusercontent.com/laude-institute/harbor/main/registry.json -o ~/st-bench/registry.json
```

## 5. Smoke test: 2 tasks per arm (a few cents)

```sh
cd ~/st-bench
python3 ~/savetokens-bench/evals/swebench/make_jobs.py --model glm-5.3-flash \
  --fallback-config ~/st-bench/fallback.yaml --tasks 2 --tag=-smoke --concurrent 2
export PYTHONPATH=~/savetokens-bench/evals/swebench
harbor run -c job-off-smoke.json -y
harbor run -c job-on-smoke.json -y
python3 ~/savetokens-bench/evals/swebench/analyze.py off=jobs/st-off-smoke on=jobs/st-on-smoke \
  --price glm-5.3-flash=IN,OUT          # replace IN,OUT with the per-million prices from step 2
```

Each container installs Hermes first, which takes several minutes.

Then check the `on` arm actually ran savetokens:

```sh
for f in jobs/st-on-smoke/*/agent/savetokens-setup.txt; do echo "== $f"; tail -15 "$f"; done
for f in jobs/st-on-smoke/*/agent/savetokens-levers.txt; do echo "== $f"; cat "$f"; done
```

Check 5a: every trial has a result, either solved or not, with no setup or infrastructure errors.
Token counts are above zero.
Check 5b: `savetokens-setup.txt` ends with `exit=0` and shows the plugin installed. The levers list
shows which levers were on. Copy that list into the report.
Check 5c: from the smoke run's cost per task, project the full run: 50 tasks × 2 arms × cost per
task. If the projection is over $25, stop and report it.

## 6. First pass: 50 tasks per arm, 1 attempt each

Run it detached. Expect several hours, mostly spent installing Hermes in each container. Use
`--concurrent 4` with 16 GB of RAM or more, `2` otherwise.

```sh
cd ~/st-bench
python3 ~/savetokens-bench/evals/swebench/make_jobs.py --model glm-5.3-flash \
  --fallback-config ~/st-bench/fallback.yaml --attempts 1 --concurrent 4
setsid nohup bash -c 'export PYTHONPATH=~/savetokens-bench/evals/swebench; \
  harbor run -c job-off.json -y > off.log 2>&1; harbor run -c job-on.json -y > on.log 2>&1' >/dev/null 2>&1 &
```

The two arms run one after the other, so they don't compete for the machine. Check progress every
30 minutes or so: `ls jobs/st-off jobs/st-on | wc -l` and `tail -3 off.log on.log`. Don't
watch it continuously. Keep an eye on the spend against the $25 budget. If it gets close, stop
with `pkill -f "harbor run"` and report what finished.

## 7. Results

```sh
python3 ~/savetokens-bench/evals/swebench/analyze.py off=jobs/st-off on=jobs/st-on \
  --price glm-5.3-flash=IN,OUT --json results.json
grep -l "exit=0" jobs/st-on/*/agent/savetokens-setup.txt | wc -l      # trials where savetokens set up fine
cat jobs/st-on/*/agent/savetokens-levers.txt | grep -i " on" | sort | uniq -c   # which levers were on
```

## Report

Reply with this, filled in:

```
machine: arch / RAM / free disk / docker version
harbor version:            hermes version (inside the containers, from any trial's logs):
2  models listed:                       PASS | FAIL  (exact ids, prices per M in/out)
3  fallback works:                      PASS | FAIL | SKIPPED  (paste the fragment; no keys)
4  oracle reward 1:                     PASS | FAIL
5a smoke trials completed:              PASS | FAIL  (paste analyze output)
5b savetokens ran in the on arm:        PASS | FAIL  (setup tail, levers list)
5c projected full-run cost:             $
6  first pass completed:                YES | PARTIAL (how many trials per arm) | NO
7  analyze output:                      (paste it in full)
   savetokens set up fine in N of 50 on-arm trials; levers on: ...
   trials that used the fallback model (if visible in the logs):
   total spend (estimate from tokens, and Engy's own figure if you can see it):
errors or tracebacks:
anything surprising:
```
