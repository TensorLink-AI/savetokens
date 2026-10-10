# savetokens

Know when you'll run out of Claude Code, Codex or your Hermes budget, before you do.

savetokens watches your Claude Code and Codex limits (each plan's 5-hour and weekly
limits, or a dollar budget when you're on an API key) and what Hermes Agent spends on
any provider's API (pay as you go, against a dollar budget), forecasts your usage, and
tells you when, at this pace, you'd hit each one. It works across every machine and
every account you use.

```
5h 12%→34% · wk 41% ⚠ out ~Tue 15:00
```

## What it does

- **Reads each tool's own meter.** Claude Code's statusline and Codex's session logs
  give the % used and reset time for each limit. That counts everything: other
  machines, the web apps, every session. savetokens records it, along with token
  counts and the limit errors in your transcripts (the times you actually ran out).
- **Or counts against your API budget.** On an API key there's no plan meter, so you
  set a budget and savetokens prices each request:

  ```sh
  savetokens api claude-code --budget 200 --per month   # or --per day / week
  savetokens api codex --budget 50 --per week
  savetokens api hermes --budget 100 --per month
  savetokens price gpt-6-astra 2 8      # Codex models: $ per million tokens in, out
  ```

  Hermes is always pay as you go, on whichever provider you point it at (OpenRouter,
  Anthropic, OpenAI, Nous, a local server). savetokens reads the running token and cost
  totals Hermes keeps per session in `~/.hermes/state.db` (every profile too) and uses
  Hermes's own cost figure, or the price list when Hermes has none. Usage Hermes marks as
  included in a plan (a ChatGPT login) counts against no budget.

  `savetokens api TOOL --off` goes back to the plan. The mode is per machine (a laptop on
  a plan and a CI box on a key both work); the budget is shared through your server.
  You rarely need to switch it yourself: Claude Code on an API key is detected (its
  statusline sends no limit meter), as is Codex logged in with a key, and savetokens asks
  once for a budget.
- **Spend by provider and model.** `savetokens spend` (and the browser view's Spend tab) shows tokens and
  dollars today, this week and this month, for each provider and each model within it. That covers Anthropic,
  OpenAI, and whatever Hermes uses: OpenRouter, Nous, Chutes, Synthetic, or any API host. It also shows the
  likely total by each period's end and over the next day, week and 30 days. Each provider's tokens are
  forecast with Ephemeris, and dollars follow from its recent price per token. A subscription counts as a fixed
  cost, spread evenly over time:

  ```sh
  savetokens plan anthropic --usd 200 --per month      # Claude Max
  savetokens plan openai --usd 200 --per month         # ChatGPT Pro (Codex)
  savetokens plan synthetic.new --usd 20 --per month   # a flat-rate provider Hermes uses
  savetokens spend --models
  ```

- **Forecasts your demand.** Each hour's rise in the weekly meter is what you used
  that hour. [Ephemeris](https://ephemeris.cascade.industries) (zero-shot time-series
  foundation models) forecasts the coming hours up to your weekly reset; a local
  baseline is the fallback.
- **Projects each limit to its reset** (or a budget to its period's end): where you'll
  likely be, the range, the chance of running out first, and when. Claude Code, Codex,
  Hermes and each budget are forecast separately, so you can see which one has room.
- **Alerts you in three stages**, once each per window:

  | stage | when |
  |---|---|
  | heads-up | more likely than not to run out before the reset |
  | act | 80% likely, or under an hour to go |
  | last call | under 10 minutes to go |

  It also flags a sudden jump in the weekly projection (an intense session starting)
  before any stage is reached. Alerts show in the statusline, as a message on your
  next prompt (to you, not the agent) and as a desktop notification where available.

## See it live

- **In Claude Code:** the statusline, plus a live pane. Install the pane with
  `/plugin install savetokens --marketplace TensorLink-AI/savetokens`, then type `/savetokens`.
  It shows each limit's bar (now, likely at reset, high end), when you'd run out, usage per
  hour with the forecast, and your model mix, refreshing every 30 seconds.
- **Ask your agent:** "how are my limits, and what should I cut?" or "how big is this job, and
  will it fit?" Install registers a savetokens MCP server with Claude Code, Codex and
  Hermes (`pacing_brief`, `estimate_job`) plus a skill that says when to use it. The answers are worked
  out from your own usage, for example:

  > Run subagents on Sonnet: saves about 6.5 points of the weekly limit a day.
  > Start synth fresh at its next task: it re-reads ~144k tokens a request; saves ~4.9 points in 5 h.

  > About 6 points and 3 h of work. The 5-hour limit fills at 21:40; it waits until 23:50 and
  > finishes around 01:30. Starting after 23:50 runs straight through.

  The same from a terminal: `savetokens advise`, `savetokens estimate --like big`.
- **Tell the agent before it asks (opt-in):** with `install --tell-agent`, when a limit is at
  risk, a short pacing note goes into the agent's context at session start and with each alert,
  so it can use cheaper subagents and avoid wide fan-outs on its own. Nothing is added while
  you're on track.
- **In your browser:** `savetokens web` opens a live dashboard on this machine, at 127.0.0.1 behind a key made
  for that run. It shows each limit with its forecast range, usage per hour against the forecast, every session
  and what stopping it would save, models, machines, alerts and limit hits. When the machine is connected to a
  server, it shows every machine. A server also serves the page at its own address: sign in with a code from
  `savetokens pair`, so a teammate or your phone needs no install.
- **In a terminal:** `savetokens watch`, the same dashboard full-screen. Put it in a split pane
  or tmux window next to Claude Code.
- **Anywhere else:** `savetokens dashboard --json`. When connected to a server, both show every
  machine and account.

## For agents and scripts

Every command takes `--json` and prints one JSON object with `"ok"`. A failure exits 1 with
`{"ok": false, "error": ..., "fix": "<the command that fixes it>"}`. `savetokens check` gives its
verdict as the exit code, so a script or an agent loop can gate a big job without parsing anything:

```sh
savetokens check --hours 3 --parallel 4 || echo "not now"
#  0  on track, and the job fits
#  3  a limit is at risk, or the job has to wait for a 5-hour reset
#  4  the job doesn't fit before the limit resets
savetokens check --tool hermes --json      # one tool's limits only
savetokens setup --json                    # the setup checklist: done, and what's left with its command
```

The MCP server never changes anything. Its `suggest_setup` tool hands the agent the exact command
(a budget, a model's price) to show you, and you decide whether it runs.

## Install

```sh
uv tool install git+https://github.com/TensorLink-AI/savetokens
savetokens install            # shows every change first, then sets up what it can't detect
savetokens status
```

Setup asks as little as it can. It reads which plan you're on from what Claude Code and Codex keep
locally (only the plan's name, e.g. Claude Max 20x or ChatGPT Pro), and offers its list price as the
default. Then it asks for what it can't know: a budget for tools paying by API, and an Ephemeris key.
For the key, press Enter: it opens Ephemeris in your browser, you sign in (or sign up) and approve
the code shown in your terminal, and the key arrives on its own. You can also paste a key; either way
it's checked on the spot. If your Ephemeris credits run out, `status`, `setup` and the browser view
say so, with the top-up link, and forecasts fall back to the local baseline. Run it again any time. The browser view shows what's left, each step with a command
to copy:

```sh
savetokens setup              # one question at a time; Enter takes the detected answer
savetokens setup --yes        # take the detected plan prices, ask nothing
savetokens setup --json       # the checklist, for an agent (changes nothing)
echo "$KEY" | savetokens setup --key -   # a key from a secret store, kept out of shell history
```

`install` adds a statusline (yours is kept, ours goes after it), four light hooks, the
skill, and one crontab line (every 10 minutes; Codex and Hermes have no hooks, so this is
when their sessions are read). It reads your Claude Code, Codex and Hermes history. `savetokens uninstall`
removes all of it; your data stays in `~/.savetokens`.

## Run a server (all your machines and accounts in one place)

A small CPU box is plenty. Start the server with your Ephemeris key:

```sh
docker run -d --name savetokens --restart unless-stopped -p 8787:8787 \
  -v savetokens:/data -e EPHEMERIS_API_KEY=... ghcr.io/tensorlink-ai/savetokens
docker logs savetokens          # the first start prints your token
```

Or `EPHEMERIS_API_KEY=... docker compose up -d` from this repo. The first start prints a join
code; on each machine:

```sh
savetokens install --server http://your-server:8787 --code ABCD-2345
```

Codes are short, single-use and last 10 minutes. For the next machine, run `savetokens pair` on
one that's already joined (or `savetokens join URL CODE` to join without reinstalling).

That's it: each machine sends its usage and Claude's limit readings, the server forecasts
with Ephemeris (through [Gnomon](https://github.com/TensorLink-AI/Gnomon), which keeps every
forecast and scores it against what happened), and every machine alerts from the result.
Containers and cloud sessions can use `SAVETOKENS_SERVER` and `SAVETOKENS_TOKEN` (the token is in
`first-token.txt` in the data volume) instead of a config file. More users:
`docker exec savetokens savetokens server --data /data --add-user NAME`.

The token travels with every request, so use HTTPS when the server is reachable from the
internet: `DOMAIN=st.example.com docker compose --profile tls up -d` puts Caddy in front.

Without a server, everything runs on your machine (`savetokens install` on its own).

## What leaves your machine

- **Nothing**, with `savetokens install --no-ephemeris` and no server.
- **To Ephemeris:** one number per hour for each tool, the % of its weekly limit used
  (or the dollars spent, on an API budget). No tokens, prompts, models, projects or sessions.
- **To your own server, if you connect one:** token counts per request (model, time,
  session id), limit readings and the times you hit a limit. Never prompts, replies,
  code or file names.

## Commands

| | |
|---|---|
| `status [--json]` | each limit: used now, at reset, chance and time of running out; your recent hits |
| `advise [--json]` | where the limits stand and the options, biggest effect first |
| `estimate --like small\|typical\|big \| --hours H \| --points P \| --usd D [--parallel N]` | a job's size, run time and whether it fits |
| `check [--hours H \| --like ... ] [--tool T]` | one verdict in the exit code: 0 on track, 3 at risk, 4 won't fit |
| `spend [--models]` | tokens and $ by provider and model: so far, by each period's end, and coming |
| `plan PROVIDER --usd USD [--per day\|week\|month \| --off]` | a subscription's fixed price |
| `ephemeris [--key K\|-] [--model M\|ensemble]` | the forecaster: key, model (toto2-313m by default), credits |
| `setup [--yes] [--key K\|-]` | plans (prices detected), API budgets, the Ephemeris key: one guided pass |
| `suggest` | what to set up here, each with its command (changes nothing) |
| `web [--port P]` | the dashboard in your browser |
| `mcp` | the MCP server your agent uses (install registers it) |
| `join URL CODE` / `pair` | join a server with a short code / make a code for another machine |
| `api TOOL [--budget USD --per day\|week\|month \| --off]` | Claude Code or Codex on an API key, or Hermes: its budget |
| `price MODEL INPUT OUTPUT [CACHE_READ]` | the price of a model savetokens doesn't know |
| `ephemeris [--key KEY \| --on \| --off]` | the forecaster, and credits left |
| `connect URL --token T` / `connect --off` | sync with a server |
| `server [--add-user NAME]` | run the sync server, with the browser view at its address |
| `watch` | the live dashboard in your terminal |
| `dashboard [--json]` | the dashboard once (JSON for other tools) |
| `maintain` | forecast if due and raise alerts (runs in the background) |
| `backfill` | read Claude Code, Codex and Hermes sessions again |
