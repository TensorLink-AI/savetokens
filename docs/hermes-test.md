# Test savetokens' Hermes support

You are testing an alpha build of savetokens, a local token-usage monitor, against this Hermes
install. Work through the steps in order, and record what each check prints. At the end, reply
with the report at the bottom.

Rules:
- Never print or copy API keys or the contents of `.env` files.
- Back up the Hermes config before step 6, and leave it as you found it.
- If you are running inside the Hermes gateway, do not run `hermes gateway restart` yourself:
  it would end your own session. Use `--no-restart`, and run test prompts with `hermes -z`
  (a fresh process loads the plugin).
- Spend at most about $0.50 of model usage in total, and use your cheapest configured model.

## 0. Prerequisites

```sh
hermes --version && which hermes
python3 --version            # 3.11 or newer
uv --version || pipx --version
```

## 1. Install the test build

It is the `hermes-test` branch of the public repo https://github.com/TensorLink-AI/savetokens
(no GitHub login needed).

```sh
uv tool install --reinstall "git+https://github.com/TensorLink-AI/savetokens@hermes-test"
# no uv: pipx install --force "git+https://github.com/TensorLink-AI/savetokens@hermes-test"
# or: git clone -b hermes-test https://github.com/TensorLink-AI/savetokens && uv tool install --reinstall ./savetokens
savetokens --version                                                  # expect 0.1.0a1
savetokens levers --help | grep -q "hermes:compaction" && echo NEW-BUILD   # confirms this build
```

## 2. Install into Hermes

```sh
hermes config path                                    # note the config file path
cp "$(hermes config path)" /tmp/hermes-config.before-savetokens.yaml
savetokens install hermes --no-restart --no-ephemeris
hermes plugins list | grep -i savetokens              # expect: enabled
hermes cron list | grep -i savetokens                 # expect: savetokens-alerts and savetokens-daily
```

Check 2: the plugin is enabled and both cron jobs exist.

## 3. Generate a little real traffic

Run three short one-shot prompts on your cheapest model (replace MODEL and PROVIDER):

```sh
for i in 1 2 3; do hermes -z "Reply with the single word ok." -m MODEL --provider PROVIDER; done
```

## 4. Capture and pricing

```sh
python3 - <<'EOF'
import sqlite3, os
db = os.path.expanduser("~/.savetokens/events.db")
c = sqlite3.connect(db)
for r in c.execute("SELECT ts, model, provider, input, output, cost_usd, cost_source FROM usage "
                   "WHERE harness='hermes' ORDER BY ts DESC LIMIT 5"):
    print(r)
print("prices:", c.execute("SELECT value FROM meta WHERE key='hermes_prices'").fetchone())
EOF
```

Check 4a: there are 3 or more new rows, with `provider` set (a provider name, or the API host for a
custom endpoint) and `cost_source` starting with `hermes:`. If the model is free or included
in a subscription, `cost_usd` may be 0 or NULL; say which.
Check 4b: `prices` lists the models you configured (main, fallbacks and side-task models), each
with per-million `in` and `out` prices. Note any configured model that is missing.
Check 4c: `savetokens report --harness hermes --days 1` shows the spend.

## 5. Budgets and pacing

```sh
savetokens budgets add test --usd 0.01 --period day --harness hermes
savetokens budgets
savetokens budget --json | python3 -m json.tool | head -40
```

Check 5: `test` shows the spend from step 3, and a chance of going over close to 100%. If
the tiny budget is already overspent, the forecast shows that too.

## 6. Levers (changes Hermes settings, then undoes them)

```sh
hermes config get auxiliary --json > /tmp/aux.before.json
hermes config get compression --json > /tmp/compression.before.json
savetokens levers on --allow hermes:side-tasks,hermes:quality-score,hermes:compaction   # Hermes only
savetokens mode lean          # lean applies levers without waiting for risk
savetokens maintain
savetokens levers             # expect ON next to the levers that applied
hermes config get compression.threshold_tokens --json    # expect 150000 as a number, not "150000"
hermes config get auxiliary --json                       # side tasks with an explicit model may now point at
                                                         # your cheapest configured model
hermes config get openrouter.min_coding_score --json     # lowered only if model is openrouter/pareto-code
```

Check 6a: each value the levers set reads back with the right type (a number stays a number).
Check 6b: `hermes -z "Reply with ok." -m MODEL --provider PROVIDER` still works with these settings.

Now undo them:

```sh
savetokens levers revert
hermes config get auxiliary --json > /tmp/aux.after.json
hermes config get compression --json > /tmp/compression.after.json
diff /tmp/aux.before.json /tmp/aux.after.json && echo AUX-RESTORED
diff /tmp/compression.before.json /tmp/compression.after.json && echo COMPRESSION-RESTORED
diff /tmp/hermes-config.before-savetokens.yaml "$(hermes config path)" || true
```

Check 6c: AUX-RESTORED and COMPRESSION-RESTORED. Report the full-file diff: it may show only
formatting, or an empty section left behind; say exactly what differs. If any setting is not
restored, copy the backup back: `cp /tmp/hermes-config.before-savetokens.yaml "$(hermes config path)"`.

## 7. Steering text (optional, about one cent)

With the tiny budget still in place and the mode set to lean:

```sh
hermes -z "Reply with ok." -m MODEL --provider PROVIDER
python3 -c "
import sqlite3,os; c=sqlite3.connect(os.path.expanduser('~/.savetokens/events.db'))
print(c.execute(\"SELECT rule, action, substr(message,1,160) FROM alerts WHERE harness='hermes' ORDER BY ts DESC LIMIT 5\").fetchall())"
```

Check 7: a `briefing` row exists (the context injected on the session's first turn), and possibly
a `budget` row.

## 8. Clean up

```sh
savetokens budgets remove test
savetokens levers off
savetokens mode auto
diff /tmp/hermes-config.before-savetokens.yaml "$(hermes config path)" || echo "config differs: see above"
```

Leave savetokens installed unless told otherwise. To remove it completely, run `savetokens uninstall hermes`.

## Report

Reply with this, filled in:

```
hermes version:
savetokens version:
2  plugin enabled / cron jobs:            PASS | FAIL  (output)
4a usage rows with provider + cost:       PASS | FAIL  (paste the rows; no keys)
4b prices for configured models:          PASS | FAIL  (which models, any missing)
4c report shows spend:                    PASS | FAIL
5  budget pacing:                         PASS | FAIL  (paste the `budgets` line)
6a lever values read back correctly:      PASS | FAIL  (paste the get outputs)
6b Hermes still works with levers on:     PASS | FAIL
6c settings restored:                     PASS | FAIL  (paste the diffs)
7  steering rows:                         PASS | FAIL | SKIPPED
8  cleaned up, config same as before:     YES | NO (what differs)
errors or tracebacks (from any step, incl. `hermes logs` lines mentioning savetokens):
anything surprising:
```
