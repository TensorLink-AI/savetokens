# Setting up savetokens: a playbook for agents

You're an agent (Claude Code, Codex, Hermes or any other) setting up savetokens for a person. Follow these steps
in order. Every command takes `--json` and prints one object with `"ok"`. When a command fails it exits 1 with
`{"ok": false, "error": ..., "fix": ...}`, and `fix` is the command that fixes it.

## Ground rules

- **Ask before changing anything.** Installing, joining a hub, and setting a budget or price all change the
  person's machine or numbers. Show them the command, say what it does in a line, and run it once they agree.
  Reading (`status`, `setup --json`, `projects`, `spend`) needs no permission.
- **Never handle keys.** For the Ephemeris key, ask the person to run `savetokens setup` themselves; it signs
  them in through the browser. Don't ask them to paste API keys or tokens into the conversation.
- **Use savetokens' numbers.** Don't estimate limits, prices or forecasts yourself.
- **Ask the person what only they know:** a budget's amount, which machine should be the hub, and whether
  more machines are coming. Everything else is detected.

## 1. Is it installed?

```sh
command -v savetokens && savetokens setup --json
```

If it's installed, go to step 3.

## 2. Install (ask first)

Say: "This installs savetokens and adds a statusline, hooks, an MCP server, a skill and one crontab line.
It doesn't change how your tools run, and `savetokens uninstall` removes all of it. OK?"

```sh
uv tool install git+https://github.com/TensorLink-AI/savetokens   # or: pipx install git+https://...
savetokens install --yes --json
```

If the person has a hub and a join code, add them:
`savetokens install --yes --json --server https://HUB --code XXXX-XXXX`.

If uv isn't installed, use the one-line installer instead. Without a terminal attached it runs non-interactively:
`curl -fsSL https://raw.githubusercontent.com/TensorLink-AI/savetokens/main/install.sh | sh`.

## 3. Work through the checklist

```sh
savetokens setup --json
```

`steps` is a list. Each step has these fields:

| field | meaning |
|---|---|
| `id` | e.g. `plan:anthropic`, `budget:provider:openrouter`, `forecaster` |
| `done` | already set up |
| `title` | what to tell the person |
| `command` | what to run; `USD` is a placeholder for an amount |
| `ask` | the question to put to the person, when the step needs an answer |
| `default` | a detected answer (for example a plan's list price) to offer |
| `optional` | skip unless the person wants it |

For each step that isn't done and isn't optional:

1. Has an `ask`: ask it, offering `default` if there is one. Put the answer into `command` in place of `USD`.
   Show the person the command and run it once they agree.
2. Is `forecaster`: ask the person to run `savetokens setup` themselves (it opens the browser), or to skip
   it. Without it, forecasts use a local baseline.
3. No `ask` and no `command`: just tell them its `title`.

Run `savetokens setup --json` again until `left` (the steps still to do) is empty, or holds only optional steps.
`savetokens suggest --json` gives the same items as proposals, if that's handier.

## 4. More than one machine?

Ask: "Do you use Claude Code, Codex or Hermes on other machines too (a laptop, a server, CI)?" If yes, the
machines should share a hub, because limits belong to the account.

- **Which machine?** Ask which one is always on. On that machine, with the person's OK, run
  `savetokens server --setup`. It prints the join line.
- **Already have a hub?** On any joined machine, run `savetokens pair --machines N --hours 24 --json`. Give the
  person `command` (the `curl … | sh -s CODE` line) to paste on each other machine, or run `join` there yourself
  if savetokens is already installed.
- **Can't reach the hub?** Check with `curl https://HUB/healthz`. The usual fix is Tailscale on both machines.

## 5. Check it works

```sh
savetokens status --json      # each limit: used now, likely at reset, when it would run out
savetokens budget --json      # each budget with its forecast
savetokens check; echo $?     # 0 on track, 3 at risk, 4 a job won't fit
```

Tell the person in two or three lines where they stand. Then point them to the dashboard: `savetokens web`, or
the hub's address. In Claude Code the statusline fills in after the next message.

## 6. Leave them set up for later

- Projects and tasks are automatic: each git repo is a project, and each non-default branch is a task. Offer a
  budget for a project only if they ask for one: `savetokens budget --project . --usd N --per month`.
- From now on, the installed skill and MCP tools (`pacing_brief`, `estimate_job`, `spend_summary`,
  `suggest_setup`) are how you answer "how are my limits?" and "will this fit?". See the
  [skill](../src/savetokens/skill/SKILL.md).

For a particular setup (several accounts, Hermes providers, CI, teams), see [setups.md](setups.md).
