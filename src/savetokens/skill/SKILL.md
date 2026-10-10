---
name: savetokens
description: Check Claude Code, Codex and Hermes usage limits (5-hour and weekly plan limits, or an API budget) with savetokens, suggest how to pace usage, and estimate big jobs before starting them. Use when the user asks about limits, usage, running out, pacing, budget or which session uses the most; when a savetokens alert appears; and before a large job (many subagents, wide refactors, long test or eval loops).
---

# Pacing Claude Code, Codex and Hermes usage with savetokens

savetokens reads each tool's own limit meter (every session, device and account) or counts API spend against a budget. Hermes is always pay as you go, so its limit is its API budget. It forecasts usage with Ephemeris, and works out the options from the user's own data. Use its numbers; don't estimate your own.

## Get the numbers

Use the savetokens MCP tools when they're available. Otherwise run the same thing as a command:

| MCP tool | Command | Gives |
|---|---|---|
| `pacing_brief` | `savetokens advise --json` | each limit: used, likely at reset, chance of running out, room left in points and hours; this session's usage; options ranked by measured effect, each with the exact change to make |
| `estimate_job` | `savetokens estimate --like big --json` | a job's size in points, hours of work at the user's pace, finish time including any wait for a 5-hour reset, and a better start time if one avoids waiting |
| `spend_summary` | `savetokens spend --json` | tokens and $ by provider and model: so far today, this week and this month; likely by each one's end; and the next day, week and 30 days. Subscriptions count as a fixed cost |
| `suggest_setup` | `savetokens suggest --json` | what to set up (a budget, a model's price, the forecaster), each with the exact command and what to ask the user for |

Points are % of a limit: a plan's weekly limit, or an API budget. Times are epoch seconds; say them as local clock times ("Wed 13:20").

In scripts and loops, `savetokens check` answers in its exit code: 0 on track, 3 a limit is at risk or the job must wait for a reset, 4 the job won't fit. It takes the same sizing options as `estimate`, and `--tool` for one tool's limits. Every command takes `--json` and prints one object with `"ok"`. On failure (exit 1) that object is `{"ok": false, "error", "fix"}`, where `fix` is the command that fixes it.

## When the user asks about limits or pacing

1. Lead with the brief's headline.
2. Give the top 2–3 options with what each buys, in the brief's words and numbers. Example: "Run subagents on Sonnet: about 6.5 points a day."
3. Offer to make a change, and make it only after the user agrees: a subagent's `model`, an agent file, the effort level, the model or provider (in Hermes), or pausing a session.

If nothing is at risk, say so in one line and don't invent cuts.

## Before a big job

Size it with `estimate_job` before starting, using one of:
- `like`: `small`, `typical` or `big`, compared with past sessions in this project.
- `hours`: hours of work.
- `usd`.
- `parallel`: how many subagents or sessions will run at once.

Tell the user the size, how long it takes, and whether it fits. If it says a later start avoids waiting, suggest it. If the job won't fit alongside their usual usage, say so before starting and offer a smaller plan, such as Sonnet subagents, fewer parallel runs, or splitting it across the reset.

## While working, when a limit is at risk

If a savetokens note or alert is in your context, pace yourself without being asked:
- Use a cheaper model (Sonnet, or a cheaper one on Hermes's provider) for search and exploration subagents.
- Avoid wide fan-outs.
- Don't re-read large files you've already seen.
- At a task boundary, suggest starting fresh with a short handover note.

Never stop the user's task on your own; tell them and let them decide.

## Rules

- **Never change settings, models or sessions without asking.** Propose; the user decides.
- **Dollar figures on a plan are API-equivalent, not charges.** On an API budget (`kind: api`) they are real spend.
- **If savetokens isn't installed**, say so and point to `savetokens install`.
- **Setup is the user's call.** Show what `suggest_setup` proposes, fill in what they tell you (a budget, a price), and run the command only after they agree. A plan's price detected from their login comes as the default (e.g. `--usd 200`): confirm it with them. For an Ephemeris key, ask them to run `savetokens setup` themselves, so the key never passes through the conversation.
- **To show the user everything**, suggest `savetokens web`: a live dashboard in their browser.
