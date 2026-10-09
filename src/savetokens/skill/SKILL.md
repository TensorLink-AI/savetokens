---
name: savetokens
description: Check Claude Code and Codex usage limits (5-hour and weekly plan limits, or an API budget) with savetokens, suggest how to pace usage, and estimate big jobs before starting them. Use when the user asks about limits, usage, running out, pacing, budget or which session uses the most; when a savetokens alert appears; and before a large job (many subagents, wide refactors, long test or eval loops).
---

# Pacing Claude Code and Codex usage with savetokens

savetokens reads each tool's own limit meter (every session, device and account) or counts API-key spend against a budget. It forecasts usage with Ephemeris, and works out the options from the user's own data. Use its numbers; don't estimate your own.

## Get the numbers

Use the savetokens MCP tools when they're available. Otherwise run the same thing as a command:

| MCP tool | Command | Gives |
|---|---|---|
| `pacing_brief` | `savetokens advise --json` | each limit: used, likely at reset, chance of running out, room left in points and hours; this session's usage; options ranked by measured effect, each with the exact change to make |
| `estimate_job` | `savetokens estimate --like big --json` | a job's size in points, hours of work at the user's pace, finish time including any wait for a 5-hour reset, and a better start time if one avoids waiting |

Points are % of a limit: a plan's weekly limit, or an API budget. Times are epoch seconds; say them as local clock times ("Wed 13:20").

## When the user asks about limits or pacing

1. Lead with the brief's headline.
2. Give the top 2–3 options with what each buys, in the brief's words and numbers. Example: "Run subagents on Sonnet: about 6.5 points a day."
3. Offer to make a change, and make it only after the user agrees: a subagent's `model`, an agent file, the effort level, or pausing a session.

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
- Use Sonnet for search and exploration subagents.
- Avoid wide fan-outs.
- Don't re-read large files you've already seen.
- At a task boundary, suggest starting fresh with a short handover note.

Never stop the user's task on your own; tell them and let them decide.

## Rules

- **Never change settings, models or sessions without asking.** Propose; the user decides.
- **Dollar figures on a plan are API-equivalent, not charges.** On an API budget (`kind: api`) they are real spend.
- **If savetokens isn't installed**, say so and point to `savetokens install`.
