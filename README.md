# savetokens

Save tokens across agent harnesses (Hermes, Claude Code, Codex) and apps that call
LLM APIs directly.

savetokens is a meta-harness: a layer beside each agent harness that watches how it
spends tokens and guides its behaviour. It forecasts when you will hit your rate limit
or budget with [Ephemeris](https://ephemeris.cascade.industries), steers the agent to
economise only when that's at risk, stops runaway sessions as they happen, shows where
tokens went, and offers fixes with a before/after on your own sessions.

It is built on [Gnomon](https://github.com/TensorLink-AI/Gnomon) (ledger, routing,
decision memory) and [Ephemeris](https://ephemeris.cascade.industries) (hosted time-series
forecasts).

**Status:** early development. Claude Code and Hermes work today; Codex usage and limits
are read from its own logs (forecasts and levers; no Codex hooks yet).

## Quick start

```sh
uv tool install savetokens                                  # or: pipx install savetokens
savetokens install claude-code --ephemeris-key <your key>   # or: savetokens install hermes
savetokens status
```

Get an Ephemeris key at [ephemeris.cascade.industries](https://ephemeris.cascade.industries).
Without one, `install` asks for it, and forecasts fall back to a local baseline until you
add it (`savetokens ephemeris connect --key <key>`).

`install` shows every change before making it, then it is live at once: no restart, no
flags. It adds the harness hooks (or the Hermes plugin), composes with your existing
statusline, backfills history, and adds an hourly crontab line so forecasts and scoring
stay current while Claude Code is closed (`--no-schedule` to skip). Your data stays on
your machine in `~/.savetokens`: counts and metadata only, never prompts, replies or file
contents. The one thing that leaves it is for forecasting: every 6 hours, hourly
API-equivalent dollar totals go to Ephemeris (no tokens, prompts, project or session
names). `--no-ephemeris` keeps everything local. Limit percentages are rough for the
first day while savetokens learns how your usage maps to your limits; the statusline
says `learning` until then.

**Why Ephemeris.** Replaying 8 weeks of real usage through 5-hour limit windows
(`savetokens backtest`), Ephemeris-driven steering avoided significantly more limit hits
than the local baseline, forecast error was about half, its ranges held for new users
from their first 12 hours (the baseline's mostly didn't), and it caught injected
runaways far more often at the same alarm rate. Run the backtest on your own history to
check.

**Hermes:** `savetokens install hermes [--notify telegram]` copies the plugin, enables
it, restarts the gateway, and adds two Hermes cron jobs (alerts every 15 minutes, silent
unless something is new, and a 09:00 summary). In Hermes Desktop, turn on the savetokens
status-bar item under Capabilities → Plugins.

## What it does

- **Steering, with a quality knob.** savetokens tells the agent what it needs to spend
  well, and only as much as you allow. `savetokens mode` sets how much:

  | Mode | What the agent gets |
  | --- | --- |
  | `quality` | Nothing beyond late guard warnings: monitoring only |
  | `balanced` | Lessons from this repo's history at session start (large outputs, files that get re-read); the budget from a 20% chance of hitting a limit, a nudge from 50% |
  | `lean` | Also asks for economical work every session; nudges from a 20% chance; the guard fires earlier |
  | `auto` (default) | `lean` from a 30% chance of hitting a limit before it resets, `quality` when there's plenty of headroom (unused subscription quota is wasted, so there's no point economising), `balanced` otherwise |

  The chance of a hit comes from the Ephemeris forecast's sample paths, so a better forecast means
  economising only when it's needed. `savetokens backtest` replays your own usage
  through 5-hour limit windows to show what each forecaster would have done: limit hits,
  dollars past the limit, and hours spent economising for nothing, with Ephemeris
  compared against the local baseline, a plain "warn at 80%" rule and perfect foresight.

  `SAVETOKENS_MODE` overrides it for one session. `savetokens briefing` shows exactly what
  the agent is told; it's usually nothing, and never more than a few lines. The agent can
  check the budget itself before a big job (`savetokens budget --json`, via the installed
  skill). For API billing, set a daily budget with `savetokens budget --daily-usd 20`.
- **Levers, when a limit is at risk.** In `auto` mode, once the forecast puts the chance of
  hitting a limit before it resets at 30% or more, savetokens switches the harness to
  cheaper options for new sessions and subagents, and puts them back afterwards:

  | Harness | Levers (least risky first) | Where |
  | --- | --- | --- |
  | Claude Code | subagents on Sonnet; effort `low`; (opt-in) auto-compact at 300k; the main model only if you allow `main` | `~/.claude/settings.json`; in a running session, the agent is asked to pass `model: "sonnet"` to routine subagents |
  | Codex | effort `low`; (opt-in) auto-compact at 150k | `~/.codex/config.toml` |
  | Hermes | side tasks on the cheapest model you already configured; (opt-in) compression at 150k; OpenRouter `pareto-code` score 0.15 lower | through `hermes config`, when a budget is at risk |

  It never changes anything mid-request. It undoes the levers when the risk drops
  below 15%, when the window resets, or as soon as tests start failing or a loop starts
  (then waits 30 minutes before trying again), restoring your exact previous values. A
  value you changed yourself in the meantime is left alone. `savetokens levers` shows
  what's allowed and applied; `--allow`, `off` and `revert` control it; install asks
  first (`--no-levers` to skip).
- **Pay-as-you-go budgets.** For API-billed spend (Hermes, OpenRouter, Engy or any
  provider Hermes supports), set dollars per day, week or month, for one provider, one
  harness or everything: `savetokens budgets add or --usd 200 --period month --provider
  openrouter`. Each call is priced by Hermes's own estimator, which uses the provider's
  reported cost where there is one and its price catalogues otherwise, so any model you pick
  is covered. savetokens paces each budget with the forecast: spend so far, the expected total
  at the period's end, the chance of going over, and when it would run out at this pace.
  `savetokens budgets connect openrouter` uses OpenRouter's own figures for the key's spend
  and remaining limit. Levers step down only to models you have already configured (main,
  fallbacks, side tasks), never to models you haven't chosen.
- **Heavy follow-ups.** A short prompt on a huge context (500k+ in `balanced`) re-reads all
  of it, and after a break of over an hour also rebuilds the expired cache: often several
  dollars for a one-line question. savetokens says what it will cost when you send it, once
  per session per 100k; set `"heavy_followup": "block"` in `~/.savetokens/config.json` to
  have it stop the prompt instead, so you can `/clear` or `/compact` first (sending it again
  goes ahead).
- **Runaway guard.** Warns the agent and you, inside the session, when the same tool
  call repeats, an unchanged file is re-read, tests keep failing with no code change,
  spend runs well above the session's own pace, or context grows past 200k tokens. A
  machine-wide check flags any hour whose spend is above the forecast's 99th percentile
  for that time of day, so a runaway in another session or a scheduled job is caught
  too (delivered through Hermes cron alerts when no session is open).
  Each warning is one line and fires once. Blocking is opt-in (`--block`). Hooks fail
  open: a savetokens error never breaks a session.
- **Actuals and forecasts, kept apart.** `savetokens status` shows this hour, the 5-hour
  window, today and this week: what is used so far for this session, this machine and
  the whole account (Claude Code's own limit reading, which includes other devices),
  and a forecast for the end of each window with a p10–p90 range. The statusline shows
  the short form, e.g. `5h 2%→6% · wk 11%→19% · session 0.2% wk`. Forecasts come
  from the Ephemeris ensemble (or, without a key or with `--no-ephemeris`, a local
  baseline that replays your past days); both are made and every window forecast is
  scored when the window ends, so `savetokens forecast` shows the track record.
- **Report.** `savetokens report [--days N]`: spend by harness, model, project and
  session, and the biggest waste in dollars: long contexts re-read every turn, prompt
  cache rebuilt after idle gaps, large tool outputs carried in context, re-read files,
  subagents on top-tier models.
- **Compaction.** Claude Code auto-compacts only near the full window on 1M-context
  models. The report shows how often compaction fired, at what context size and what it
  cost; `savetokens fixes plan autocompact-window` proposes an earlier window based on
  where you compact by hand. The context warning stays quiet when auto-compact is about
  to fire, and after a long break suggests `/clear` (the cache is cold anyway).
- **Fixes.** `savetokens fixes list|plan|apply|revert|impact`: route subagents to
  Sonnet, cap shell or MCP output, auto-compact earlier, add output-hygiene guidance. Each applies only with
  your consent, reverts exactly, and `impact` shows an observational before/after with
  an interval.

**Subscription or API.** Each session is tagged by how it is billed: Claude Code sends
limit figures only to subscribers, and older sessions take your account's plan. For
subscription usage, savetokens reports and forecasts **share of your 5-hour and weekly
limits**; for API usage, **dollars**. Anthropic doesn't publish how limits are counted,
so savetokens learns it per model from your statusline readings (`savetokens learn`),
logs every fit, and sharpens as readings accumulate. Usage on other devices also counts
towards your limits but isn't visible locally, so learned rates can read high.

## Commands

| Command | What it does |
| --- | --- |
| `savetokens install <harness> [--yes] [--block] [--ephemeris-key K \| --no-ephemeris]` | Add hooks or plugin, connect Ephemeris and backfill history |
| `savetokens mode [quality\|balanced\|lean\|auto]` | Show or set the quality knob |
| `savetokens budget [--json] [--daily-usd N]` | Limits used and forecast at reset, with advice; set an API daily budget |
| `savetokens budgets [add NAME --usd N --period day\|week\|month [--provider P] [--harness H] \| remove NAME \| connect openrouter]` | Pay-as-you-go budgets, paced with the forecast |
| `savetokens levers [status\|on\|off\|revert] [--allow ...]` | What savetokens may change when a limit is at risk, and what it has changed |
| `savetokens study [--dry-run]` | Experimental: a cheap model reads skeletons of your costliest sessions (structure only, paths hashed) and proposes changes; nothing is applied |
| `savetokens backtest [--lean 0.2] [--no-ephemeris] [--show]` | Replay your usage: limit hits avoided by each forecaster, forecast accuracy, new-user accuracy, runaway detection |
| `savetokens briefing [--cwd DIR]` | What the agent is told at session start in that project |
| `savetokens status [--session ID]` | Actuals and forecasts per window, for session, machine and account |
| `savetokens report [--days N] [--json]` | Where tokens went and the biggest waste |
| `savetokens fixes [list\|plan\|apply\|revert\|impact] <id>` | Canned fixes |
| `savetokens learn [--refit]` | What it has learned about your limits, per model |
| `savetokens ephemeris connect [--key K \| --env-file F]` | Connect Ephemeris, the default forecaster (sends hourly dollar totals only); `disconnect` keeps forecasts local |
| `savetokens forecast [--refresh]` | Per-day outlook, Ephemeris vs baseline, and the scored track record |
| `savetokens guard [--block on\|off]` | Show or change guard settings |
| `savetokens capabilities --json` | Machine-readable summary for agents |
| `savetokens notify [--daily]` | New alerts for cron delivery; silent when nothing is new |
| `savetokens feedback "..."` | Leave feedback (stored locally) |
| `savetokens uninstall <harness>` | Remove hooks or plugin; keeps your data |

## Development

```sh
python3 -m pytest
```

Standard library only, Python 3.10+.
