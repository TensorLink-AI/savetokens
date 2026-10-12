# savetokens

Set a token budget and stick to it, for Claude Code, Codex and Hermes.

savetokens tells you when, at this pace, you'd run out, and what to cut before you do. It doesn't
change how your tools run: you decide. Each week it shows how you did against your plan.

savetokens watches your Claude Code and Codex limits (each plan's 5-hour and weekly
limits, or a dollar budget when you're on an API key) and what Hermes Agent spends on
any provider's API (pay as you go, against a dollar budget), forecasts your usage, and
tells you when, at this pace, you'd hit each one. It works across every machine and
every account you use.

```
5h 12%→34% · wk 41% ⚠ out ~Tue 15:00
```

```
Last 7 days: Claude Code $412 (API-equivalent, on your plan), Hermes $38; spend 12% down on the 7 days before.
  Hermes budget: $38 of the $70 planned for these days, $32 under plan.
  Claude Code weekly limit: ended at 81% on Fri 17 Oct, 9 points under the 90% forecast early that week.
  Limit hits: 0 (the 7 days before: 2).
```

## Quick start

One machine, one line (installs uv if needed, then savetokens, then sets it up; shows every change first):

```sh
curl -fsSL https://raw.githubusercontent.com/TensorLink-AI/savetokens/main/install.sh | sh
savetokens status        # each limit, and when you'd run out
```

Or by hand: `uv tool install git+https://github.com/TensorLink-AI/savetokens` (or `pipx install ...`),
then `savetokens install`. Options go after `sh -s --`, e.g. `| sh -s -- --no-ephemeris`.

Several machines (your limits are per account, so add them up in one place):

```sh
savetokens server --setup                              # on the machine that's always on
curl -fsSL http://your-hub:8787/join | sh -s KXQT-7MBD # on each other machine: setup prints this line
```

Then watch it in Claude Code's statusline, `savetokens web`, or ask your agent "how are my limits?".
More in [Install](#install), [Run a hub](#run-a-hub-all-your-machines-in-one-place) and
[Troubleshooting](#troubleshooting).

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
- **Where your tokens go, and what the same work would cost done differently.** `savetokens efficiency`
  works the same for every tool, provider and way of paying. Every number is one of two kinds:
  - **facts**, plain accounting: tokens and $ by provider, model and project; how much of each prompt is
    context re-sent from earlier in the conversation, and how much of that the cache served; how far prompts
    grew; the share of output spent reasoning;
  - **what-ifs**, exact arithmetic under one assumption shown beside the number: caching the re-sent context
    that was billed as fresh input; keeping conversations near their starting size (an upper bound); the same
    tokens at another model's prices (price only: whether it would do the work as well isn't claimed).

  ```
  Facts:
    99% of prompt tokens were re-sent from earlier in the conversation; 94% of that came from the cache.
    Prompts grew 2.73B tokens past where their conversations started ($1,786).
  Same work, done differently (what-ifs):
     $1,786  Keep conversations near their starting size: an upper bound; starting fresh still costs a handover.
        $72  Cache the context that's re-sent: at the provider's cache-read price instead.
  ```

  A third kind comes from your own history, once there's enough of it: **measured** effects. Finished tasks
  compared by how they were worked ("tasks started in a fresh session: median $3.10 against $8.20 for ones that
  continued an existing session; measured across 9 and 11 finished tasks"), and every suggestion followed up a
  week after it was first shown, before against after ("share of subagent $ on Sonnet 0% → 82%; $ per
  finished task $6.40 → $4.90; n 12 and 9. Measured; other things changed too."). Below three tasks on a side,
  it says "not enough data yet" rather than a number.

  There are no rules of thumb inside: prices come from a catalogue of every model OpenRouter lists, from every
  provider (refreshed daily, with your own `savetokens price` on top), so a model released tomorrow is priced
  without an update. `advise`, the agent's tools and the weekly recap use the same numbers.
- **Budgets for anything: overall, per project or task, per provider or tool.** Every machine counts. A dollar
  budget counts **API spend** (what was billed pay-as-you-go: real money) unless you say `--count all`, which
  counts plan usage too, at API prices:

  ```sh
  savetokens budget --usd 500 --per month                       # overall API spend
  savetokens budget --project webapp --usd 150 --per month      # one project's API spend, any provider
  savetokens budget --project webapp --usd 800 --count all      # its plan usage too, at API prices
  savetokens budget --project webapp --provider openai --usd 100   # one provider (or --tool codex)
  savetokens budget --project . --tokens 30M --by 2026-12-15    # this folder's project: a total by a date
  savetokens budget                                             # each with its forecast
  ```
  ```
  Overall budget (API spend): $2,140 of $3,000 a month (71%); likely 96% by the end, 41% chance of going over
  webapp budget (all usage at API prices): $183 of $400 a month (46%); likely 153% by the end, 78% chance of going over
  ```

  Each budget is forecast like a limit, alerts in the same three stages, and has a line in the weekly recap.
  `savetokens projects` shows every project's tokens, its value at API prices and what was paid as API, split
  by tool, provider and plan or API, with its biggest tasks; the browser view's Projects tab shows the same.

  A project is its git repository, the same on every machine however it was cloned. A folder without one is a
  project on that machine only, unless you mark it: `savetokens project link NAME` writes a `.savetokens` file,
  and committing it makes the folder (or several repos under it) one project everywhere. Only an id goes to
  your hub, unless you ask: `savetokens projects --share-names on` sends project and task names to your own
  hub, so its dashboard shows them.
- **Tasks: what each piece of work costs.** A git branch is a task (any but the repo's default one), or an
  issue key in its name (`savetokens task pattern '[A-Z]+-[0-9]+'`), or one you declare:

  ```sh
  savetokens task start "Add OAuth"          # what follows here counts to it
  savetokens task done                       # finished: its cost joins the estimates
  savetokens tasks --project .               # every task: $, tokens, sessions, open or done
  savetokens budget --task "Add OAuth" --tokens 3M
  ```

  Agents can do the same through MCP (`task_start`, `task_status`, `task_done`). Estimates are measured, never
  guessed: "finished tasks in this project: median $4.10, 9 in 10 under $12; from 14 tasks", or "not enough
  data yet" until three tasks are done. Like projects, only an id leaves the machine.
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

Every command except `watch`, `web`, `mcp` and `server` takes `--json` and prints one JSON object with `"ok"`. A failure exits 1 with
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

**Setting it up for the user.** Everything has a non-interactive form, but installing and
connecting change the user's machine, so show the command and run it only once they agree:

```sh
savetokens install --yes --json                       # this machine, nothing asked
savetokens install --yes --server URL --code CODE     # and join a hub (what the join line runs without a terminal)
savetokens setup --json                               # what's still missing, each with its command
savetokens suggest --json                             # the same, as proposals with what to ask the user
```

Leave the Ephemeris key to the user (`savetokens setup` signs them in in the browser), so it never
passes through the conversation. The skill installed for Claude Code, Codex and Hermes says all of
this to the agent; it's in [`src/savetokens/skill/SKILL.md`](https://github.com/TensorLink-AI/savetokens/blob/main/src/savetokens/skill/SKILL.md).

The MCP server never changes anything. Its `suggest_setup` tool hands the agent the exact command
(a budget, a model's price) to show you, and you decide whether it runs.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/TensorLink-AI/savetokens/main/install.sh | sh
# or: uv tool install git+https://github.com/TensorLink-AI/savetokens && savetokens install
savetokens status
```

Setup asks as little as it can. It reads which plan you're on from what Claude Code and Codex keep
locally (only the plan's name, e.g. Claude Max 20x or ChatGPT Pro), and offers its list price as the
default. Then it asks for what it can't know: a budget for tools paying by API, and an Ephemeris key.
For the key, press Enter: it opens Ephemeris in your browser, you sign in (or sign up) and approve
the code shown in your terminal, and the key arrives on its own. You can also paste a key; either way
it's checked on the spot. The key is kept in `~/.config/ephemeris/credentials` (readable by you only), the file the Ephemeris CLI (`ephemeris auth login`) and MCP bridge (`ephemeris-mcp login`) use too, so signing in with any of them covers all three. If your Ephemeris credits run out, `status`, `setup` and the browser view
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

## Run a hub (all your machines in one place)

Your plan's limits are per account, not per machine, so savetokens adds every machine up in one
place: a hub. Pick the machine that's always on (a desktop, a home server, a small VPS) and run:

```sh
savetokens server --setup
```

It signs you in to Ephemeris if you haven't (the hub makes the forecasts for every machine),
starts the hub now and at every boot, connects this machine, and prints one line. Paste that line
on every other machine:

```sh
curl -fsSL http://your-hub:8787/join | sh -s KXQT-7MBD
```

It installs savetokens if it's missing and joins. The code works for 24 hours, for up to 10
machines. More later: `savetokens pair --machines 5 --hours 24`, or **Add a machine** in the
hub's browser view (it also makes a token for CI).

**How machines reach the hub.** Setup picks the hub's address once and saves it, and every join
command (here, `savetokens pair`, and **Add a machine** in the browser) uses it, whatever address your
browser opened the hub at. It prefers HTTPS from Tailscale (`tailscale serve` or `tailscale funnel` in
front of the hub), then the hub's [Tailscale](https://tailscale.com) name (free: private and encrypted,
no domain or certificates), then its local network address, fine for machines on the same network. Your
own domain or a proxy in front: `savetokens server --setup --public-url https://hub.example.com`, or
change it later with `savetokens server --public-url URL`.

**No always-on machine?** Deploy a hub with HTTPS in a couple of minutes:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/TensorLink-AI/savetokens)

Paste your Ephemeris key when Render asks, then open the service's Logs: the first start prints
the join line, with your hub's https address. (A starter instance with a 1 GB disk, about $7 a month.)
Or anywhere with Docker:

```sh
docker compose up -d                                 # EPHEMERIS_API_KEY=... in front, or in .env
DOMAIN=st.example.com docker compose --profile tls up -d   # with HTTPS (Caddy), for the open internet
docker compose logs savetokens                       # the join line
```

**CI, containers, GPU pods:** no install needed, set `SAVETOKENS_SERVER` and `SAVETOKENS_TOKEN`
(**Add a machine** → Make a token). Teammates get their own view:
`savetokens server --add-user NAME`. `savetokens server --stop` stops a hub set up with `--setup`.

**Teams.** Put people on the same hub into a team, and the team sees one set of projects (a repository is the
same project for everyone), its totals, and budgets every member's usage counts toward:

```sh
savetokens server --team acme --member sam --role admin     # on the hub (a new person gets a join line)
savetokens team                                             # on anyone's machine: totals, projects, budgets
savetokens budget --team --project webapp --usd 2000 --per month    # owner or admins
savetokens project share                                    # the team sees this project's name, not an id
savetokens team --people on                                 # owner: members see the per-person view too
```

The owner and admins see usage per person; members see the team's totals and their own, unless the owner turns
the per-person view on for everyone. A team budget's alerts reach every member's next prompt.

**Any other agent or app, through OpenTelemetry.** A hub takes OTLP over HTTP with JSON at its own address,
so anything that exports the GenAI conventions (or Claude Code's own events) is counted with no reader for it.
`savetokens otel` prints the lines to paste where it runs (with a token of its own):

```sh
export OTEL_EXPORTER_OTLP_ENDPOINT='https://your-hub'
export OTEL_EXPORTER_OTLP_PROTOCOL='http/json'
export OTEL_EXPORTER_OTLP_HEADERS='Authorization=Bearer st_...'
export OTEL_LOGS_EXPORTER=otlp OTEL_TRACES_EXPORTER=otlp
```

A tool savetokens also reads on a machine is counted once: the reader's rows replace the telemetry for the
same session.

**API usage no agent reported: the providers' own reports (opt in).** With an admin key on the hub, it reads
Anthropic's and OpenAI's hourly usage by model and adds only what readers and telemetry didn't already send
for that hour and model (your apps, CI, scripts). The key is read from stdin, kept in a file only you can read
beside your data, and never synced:

```sh
echo "$ANTHROPIC_ADMIN_KEY" | savetokens server --provider-key anthropic
echo "$OPENAI_ADMIN_KEY"    | savetokens server --provider-key openai
```

Each machine sends its usage and Claude's limit readings; the hub forecasts with Ephemeris
(through [Gnomon](https://github.com/TensorLink-AI/Gnomon), which keeps every forecast and scores
it against what happened), and every machine alerts from the result. Tokens travel with every
request, so use Tailscale or HTTPS when the hub is reachable from the internet.

Without a hub, everything runs on your machine (`savetokens install` on its own).

## Troubleshooting

Start with `savetokens setup --json` (or the browser view): it lists what's missing, each with its fix.
Every failure with `--json` also says `"fix"`: the command to run.

| | |
|---|---|
| The join line hangs or says it can't connect | The other machine can't reach the hub. Try `curl http://HUB:8787/healthz` from it. Use Tailscale on both, open port 8787 on the hub's firewall, or deploy a hub with HTTPS. |
| The hub is on WSL | Other machines can't reach WSL's address. Install Tailscale inside WSL, or turn on [mirrored networking](https://learn.microsoft.com/windows/wsl/networking#mirrored-mode-networking) (`networkingMode=mirrored` in `.wslconfig`). |
| "unknown or expired code" | Codes expire (24 hours from `--setup`) or run out of machines. Make another on the hub: `savetokens pair --machines 5 --hours 24`, or **Add a machine** in its browser view. |
| `savetokens: command not found` after the join line | The installer put it in `~/.local/bin`. Open a new terminal, or add that to your `PATH`. |
| The hub stopped after a reboot | `systemctl --user status savetokens-server` (Linux) or `~/.savetokens/server.log`. Run `savetokens server --setup` again; it's safe to repeat. |
| Forecasts say "local baseline" | No Ephemeris key on the hub, or its credits ran out: `savetokens ephemeris` says which, with the top-up link. |
| The statusline shows nothing | Run `savetokens status`. If it's empty, Claude Code hasn't sent a reading yet: send one message, then look again. |
| Start over | `savetokens uninstall` removes everything it added; your data stays in `~/.savetokens`. |

## What leaves your machine

- **Nothing**, with `savetokens install --no-ephemeris` and no server.
- **To Ephemeris:** one number per hour for each tool, the % of its weekly limit used
  (or the dollars spent, on an API budget). No tokens, prompts, models, projects or sessions.
- **To your own server, if you connect one:** token counts per request (model, time,
  session id), limit readings and the times you hit a limit, plus ids for projects and tasks
  (keyed hashes, not names). Never prompts, replies, code, file, folder, branch or project names.
- **Telemetry and provider reports, if you turn them on:** OpenTelemetry goes from the agent to your hub;
  a provider admin key stays on the hub machine and is used only to read that provider's usage report.

## Commands

| | |
|---|---|
| `install [--yes] [--server URL --code CODE]` / `uninstall` | set up this machine (and join a hub) / remove everything it added |
| `budget [--project NAME \| --task NAME] [--provider P] [--tool T] [--count api\|all] (--usd X \| --tokens N) (--per day\|week\|month \| --by DATE)` / `budget` / `budget --off NAME` | a budget, overall or for a project or task, API spend or all usage / every budget with its forecast / remove one |
| `projects [--days N] [--share-names on\|off]` / `project link [NAME]` | every project's tokens, API value and API spend by tool, provider and plan or API (every machine, on a hub) / make this folder one project everywhere |
| `task start NAME` / `task done` / `task` / `tasks [--project NAME]` | declare a task, finish it, see the current one / every task's cost and the measured estimate |
| `team [--people on\|off]` / `project share` / `budget --team ...` | your team's totals, projects and budgets / name a project for the team / a team budget |
| `efficiency [--days N] [--tool T] [--provider P] [--project NAME] [--as MODEL]` | where tokens go (facts) and what the same work would cost done differently (what-ifs) |
| `recap [--days N]` | the last week (or N days) against your plan: under or over budget and forecast, limit hits |
| `status [--json]` | each limit: used now, at reset, chance and time of running out; your recent hits |
| `advise [--json]` | where the limits stand and the options, biggest effect first |
| `estimate --like small\|typical\|big \| --hours H \| --points P \| --usd D [--parallel N]` | a job's size, run time and whether it fits |
| `check [--hours H \| --like ... ] [--tool T]` | one verdict in the exit code: 0 on track, 3 at risk, 4 won't fit |
| `spend [--models]` | tokens and $ by provider and model: so far, by each period's end, and coming |
| `plan PROVIDER --usd USD [--per day\|week\|month \| --off]` | a subscription's fixed price |
| `setup [--yes] [--key K\|-]` | plans (prices detected), API budgets, the Ephemeris key: one guided pass |
| `suggest` | what to set up here, each with its command (changes nothing) |
| `web [--port P]` | the dashboard in your browser |
| `mcp` | the MCP server your agent uses (install registers it) |
| `api TOOL [--budget USD --per day\|week\|month \| --off]` | Claude Code or Codex on an API key, or Hermes: its budget |
| `price MODEL INPUT OUTPUT [CACHE_READ]` | the price of a model savetokens doesn't know |
| `ephemeris [--key K\|- \| --model M\|ensemble \| --on \| --off]` | the forecaster: key, model (toto2-313m by default), credits left |
| `server --setup [--public-url URL]` / `server --public-url URL` / `server --stop` | make this machine the hub, starting on boot / the address every join command uses / stop it (the data stays) |
| `server [--host H --port P --add-user NAME]` | run the hub yourself, with the browser view at its address |
| `pair [--machines N --hours H]` | a join code and the one line to paste on more machines |
| `join URL CODE` | join a hub with a code |
| `connect URL --token T` / `connect --off` | sync with a server |
| `watch` | the live dashboard in your terminal |
| `dashboard [--json]` | the dashboard once (JSON for other tools) |
| `maintain` | forecast if due and raise alerts (runs in the background) |
| `backfill` | read Claude Code, Codex and Hermes sessions again |

## Releasing

Bump `version` in `pyproject.toml` and `src/savetokens/__init__.py`, merge, then tag:
`git tag v0.3.0 && git push origin v0.3.0`. The `publish` workflow tests, builds and uploads to PyPI
through Trusted Publishing (set up once at pypi.org: project `savetokens`, workflow `publish.yml`,
environment `pypi`).
