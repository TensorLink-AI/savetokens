# Getting started

Five minutes from nothing to a dashboard that shows every limit and budget you have. It works with Claude Code,
Codex and Hermes, on a plan or an API key, on one machine or many.

Setting it up with an agent? Hand it [agents.md](agents.md). Have a particular setup in mind (several accounts,
several machines, Hermes on a pay-as-you-go API)? See [setups.md](setups.md).

## 1. Install on your main machine

```sh
curl -fsSL https://raw.githubusercontent.com/TensorLink-AI/savetokens/main/install.sh | sh
```

This installs savetokens (and uv first if you don't have it), then runs `savetokens install`. That lists every
change it's about to make and asks before making it:

- a statusline in Claude Code (if you already have one, it stays, and ours goes after it)
- light hooks in Claude Code
- a skill and an MCP server for Claude Code, Codex and Hermes, so your agent can answer "how are my limits?"
- one crontab line that runs every 10 minutes and reads Codex and Hermes sessions, since those tools have no hooks

It also reads your existing history, so you see real numbers straight away.

Nothing changes how your tools run. `savetokens uninstall` removes all of it, and your data stays in
`~/.savetokens`.

## 2. Answer one question (maybe two)

Install takes what it can detect without asking: which Claude or ChatGPT plan you're on, and its list price.

| It asks | What to say |
|---|---|
| Sign in to Ephemeris (the forecaster)? | Press Enter. Your browser opens; sign in or sign up (new accounts get free credits) and approve the code shown in your terminal. Type `s` to skip, and forecasts use a simple local model. Not asked on a machine joined to a hub: the hub forecasts for it. |
| Show project names on your hub's dashboard? | Only asked when joining a hub. Enter says yes. Names go to your own hub only; otherwise its dashboard shows ids. |

Budgets for what you pay per token (Chutes, OpenRouter, an Anthropic API key, ...) come later, once
savetokens has seen what you spend: run `savetokens setup`, which asks for each one, or see step 5.

## 3. Look at it

```sh
savetokens status     # each limit: used now, likely at reset, when you'd run out
savetokens web        # the dashboard in your browser
```

In Claude Code the statusline shows something like `5h 12%→34% · wk 41%`: 12% of the 5-hour limit used now, and
likely 34% by its reset.

If the statusline is empty, send one message in Claude Code. It shows up after the first reading.

## 4. Add your other machines (optional, recommended)

A plan's limits belong to your account, not to one machine, so every machine has to report to one place to get
the full picture. That place is a hub. Run this on the machine that's always on (a desktop, a home server, a
small VPS):

```sh
savetokens server --setup
```

It starts the hub now and at every boot, and prints one line. Paste that line on each other machine:

```sh
curl -fsSL https://your-hub/join | sh -s KXQT-7MBD
```

The code works for 24 hours, on up to 10 machines. To make another one, run
`savetokens pair --machines 5 --hours 24`, or use **Add a machine** in the dashboard.

Tip: install [Tailscale](https://tailscale.com) on every machine first. It's free, and the hub gets a private
HTTPS address with no ports to open.

The dashboard is at the hub's address. To sign in on a phone or a teammate's laptop, use a code from
`savetokens pair`; nothing needs installing there.

## 5. Set the budgets you care about

Plans already have their limits. For anything you pay per token, set a budget:

```sh
savetokens budget --provider openrouter --usd 100 --per month   # one provider's pay-as-you-go spend
savetokens budget --usd 500 --per month                         # all API spend, every tool and machine
savetokens budget --project . --usd 150 --per month             # this folder's project
savetokens budget                                               # every budget, with its forecast
```

A $ budget counts real API spend unless you add `--count all`, which also counts plan usage at API prices.
You can also add, remove and change budgets in the dashboard (Projects tab, BUDGETS).

Each budget can have the budget agent watch over it: `watch` (alerts, the default), `advise` (also tells the agent
at work to pace itself), `guard` (also stops new work once it's used up), or `off`. Set it with `--mode` or in the
dashboard.

## 6. Day to day

- The statusline and alerts warn you in three stages (heads-up, act, last call) when you're likely to run out.
- Ask your agent: "how are my limits?", "will this job fit?", "where are my tokens going?"
- Before a big job: `savetokens check --hours 3 --parallel 4` (exit code 0 means it fits).
- Weekly: `savetokens recap`, how the week went against your plan.

## If something's off

Start with `savetokens setup --json`. It lists what's missing, each with the command that fixes it. The README's
[Troubleshooting](../README.md#troubleshooting) covers join lines that hang, a hub on WSL, and expired codes.
