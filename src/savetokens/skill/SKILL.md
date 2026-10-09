---
name: savetokens
description: Check the Claude usage limits (5-hour and weekly) with savetokens and suggest how to pace usage so the user doesn't run out. Use when the user asks about limits, usage, running out, pacing, budget, which session is using the most, or before a large job (many subagents, wide refactors, long test loops).
---

# Pacing Claude usage with savetokens

savetokens reads Claude's own limit meter (every session, device and account), forecasts usage with Ephemeris, and knows which sessions are using the most.

## Read the numbers

```sh
savetokens dashboard --json
```

The parts that matter:

- `headline`: `{level, text}`. `level` is `ok`, `warn` or `bad`. `text` is the one-line answer. Lead with it.
- `limits[]`: one per limit (`five_hour`, `seven_day`):
  - `used`: % used now.
  - `p50`: the likely % at reset; `p10` to `p90` is the range.
  - `p_hit`: the chance of running out before the reset.
  - `eta`: when you'd run out at this pace (epoch seconds), or null.
  - `resets`: the reset time.
  - `stage`: `heads_up`, `act`, `last_call`, or null.
- `sessions[]`: the last 24 hours, biggest first:
  - `project` and `running`.
  - `pct_week`: % of the weekly limit used today.
  - `pace`: % of the weekly limit used in the last hour.
  - `subagents`: the share of the session's usage that went through subagents.
  - `model`.
  - `if_stopped`: what pausing the session for the next 5 hours would change. `eta` → `eta_if_stopped`, where null means it would reach the reset. When nobody is running out, `adds` is the points of the limit it would use.
- `models[]`: this week's mix, with each model's subagent share.
- `usd_per_pct`: API-equivalent dollars per 1% of the weekly limit, for sizing a job before it starts.

Times are epoch seconds. Say them as local clock times ("Sun 19:59"), not raw numbers.

## Suggest what to do, biggest effect first

Only suggest what the numbers support, and say what each step buys in time or points. Typical moves, roughly by size:

1. **Pause or wind down the session that buys the most.** That's the running session with the latest `if_stopped.eta_if_stopped`, or the largest `adds`. Name it by project.
2. **Move subagents to a cheaper model** when a session's `subagents` share is high. Options:
   - Ask for Sonnet in the Task call.
   - Set `model: sonnet` in the agent's frontmatter.
   - Set `CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-5-5` for new sessions.

   Exploration and search subagents rarely need Opus.
3. **Lower the effort** (`/effort`, or `effortLevel` in settings) for routine work, such as edits, tests and small fixes.
4. **Compact or start fresh at a natural break** in long sessions, since re-reading a large context costs on every turn. Do it only at a break, never mid-task.
5. **Shift background or batch work to after the reset** when the 5-hour window is the problem, because the 5-hour limit resets often.
6. **Size big jobs first.** For a job estimated at about $X of API-equivalent usage, that's X / `usd_per_pct` points of the weekly limit. Compare it with the room left (100 − `p50`) before starting.

If `headline.level` is `ok` and `p_hit` is low, say so briefly and don't invent cuts.

## Rules

- **Never change settings, models or sessions yourself without asking.** Propose; the user decides.
- **Don't print token counts or dollar figures as if they were the user's bill.** On a subscription they're API-equivalent, not charges.
- **If `savetokens` isn't found**, say so and point to `savetokens install`.
