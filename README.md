# savetokens

Know when you'll run out of Claude Code or Codex, before you do.

savetokens watches your Claude Code and Codex limits (each plan's 5-hour and weekly
limits, or a dollar budget when you're on an API key), forecasts your usage, and
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
  savetokens price gpt-6-astra 2 8      # Codex models: $ per million tokens in, out
  ```

  `savetokens api TOOL --off` goes back to the plan. The mode is per machine (a laptop on
  a plan and a CI box on a key both work); the budget is shared through your server.
- **Forecasts your demand.** Each hour's rise in the weekly meter is what you used
  that hour. [Ephemeris](https://ephemeris.cascade.industries) (zero-shot time-series
  foundation models) forecasts the coming hours up to your weekly reset; a local
  baseline is the fallback.
- **Projects each limit to its reset** (or a budget to its period's end): where you'll
  likely be, the range, the chance of running out first, and when. Claude Code, Codex
  and each budget are forecast separately, so you can see which one has room.
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
- **Ask your agent:** "how are my limits, and what should I cut?" The savetokens skill is
  installed for Claude Code, and for Codex when it's on the machine.
- **In a terminal:** `savetokens watch`, the same dashboard full-screen. Put it in a split pane
  or tmux window next to Claude Code.
- **Anywhere else:** `savetokens dashboard --json`. When connected to a server, both show every
  machine and account.

## Install

```sh
uv tool install git+https://github.com/TensorLink-AI/savetokens
savetokens install            # shows every change first; asks for an Ephemeris key
savetokens status
```

`install` adds a statusline (yours is kept, ours goes after it), four light hooks, the
skill, and one crontab line (every 10 minutes; Codex has no hooks, so this is when its
sessions are read). It reads your Claude Code and Codex history. `savetokens uninstall`
removes all of it; your data stays in `~/.savetokens`.

## Run a server (all your machines and accounts in one place)

A small CPU box is plenty. Start the server with your Ephemeris key:

```sh
docker run -d --name savetokens --restart unless-stopped -p 8787:8787 \
  -v savetokens:/data -e EPHEMERIS_API_KEY=... ghcr.io/tensorlink-ai/savetokens
docker logs savetokens          # the first start prints your token
```

Or `EPHEMERIS_API_KEY=... docker compose up -d` from this repo. Then on every machine:

```sh
savetokens install --server http://your-server:8787 --token st_...
```

That's it: each machine sends its usage and Claude's limit readings, the server forecasts
with Ephemeris (through [Gnomon](https://github.com/TensorLink-AI/Gnomon), which keeps every
forecast and scores it against what happened), and every machine alerts from the result.
Containers and cloud sessions can use `SAVETOKENS_SERVER` and `SAVETOKENS_TOKEN` instead of a
config file. More users: `docker exec savetokens savetokens server --data /data --add-user NAME`.

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
| `api TOOL [--budget USD --per day\|week\|month \| --off]` | Claude Code or Codex on an API key, and its budget |
| `price MODEL INPUT OUTPUT [CACHE_READ]` | the price of a model savetokens doesn't know |
| `ephemeris [--key KEY \| --on \| --off]` | the forecaster, and credits left |
| `connect URL --token T` / `connect --off` | sync with a server |
| `server [--add-user NAME]` | run the sync server |
| `watch` | the live dashboard in your terminal |
| `dashboard [--json]` | the dashboard once (JSON for other tools) |
| `maintain` | forecast if due and raise alerts (runs in the background) |
| `backfill` | read Claude Code and Codex sessions again |
