---
name: savetokens
description: Check Claude Code and Codex usage limits (5-hour and weekly plan limits, or an API budget) with savetokens and suggest how to pace usage so the user doesn't run out. Use when the user asks about limits, usage, running out, pacing, budget, which session is using the most, or before a large job (many subagents, wide refactors, long test loops).
---

# Pacing Claude Code and Codex usage with savetokens

savetokens reads Claude Code's and Codex's own limit meters (every session, device and account), or counts API-key usage against a dollar budget. It forecasts usage with Ephemeris and knows which sessions are using the most.

## Read the numbers

```sh
savetokens dashboard --json
```

The parts that matter:

- `headline`: `{level, text}`. `level` is `ok`, `warn` or `bad`. `text` is the one-line answer. Lead with it.
- `limits[]`: one per limit:
  - `label`: what to call it ("weekly limit", "Codex weekly limit", "Claude Code API budget").
  - `pool`: `claude-code`, `codex`, `claude-code:api` or `codex:api`. `name` is `five_hour`, `seven_day` or `budget`.
  - For a budget (`kind` `api`): `spent_usd` of `budget_usd` per `period`. Its `used` and `p50` are % of the budget, and `resets` is when the period ends.
  - `used`: % used now.
  - `p50`: the likely % at reset; `p10` to `p90` is the range.
  - `p_hit`: the chance of running out before the reset.
  - `eta`: when you'd run out at this pace (epoch seconds), or null.
  - `resets`: the reset time.
  - `stage`: `heads_up`, `act`, `last_call`, or null.
- `sessions[]`: the last 24 hours, biggest first:
  - `project`, `harness` (`claude-code` or `codex`), `pool` and `running`.
  - `pct_week`: % of its pool's limit used today (the weekly limit, or the budget).
  - `pace`: the same for the last hour.
  - `subagents`: the share of the session's usage that went through subagents.
  - `model`.
  - `if_stopped`: what pausing the session for the next 5 hours would change. `eta` → `eta_if_stopped`, where null means it would reach the reset. When nobody is running out, `adds` is the points of the limit it would use.
- `models[]`: this week's mix per `harness`, with each model's subagent share.
- `usd_per_pct`: API-equivalent dollars per 1% of Claude's weekly limit, for sizing a job before it starts.
- `unpriced`: Codex models used on an API key that have no price yet, so they aren't counted. Suggest `savetokens price MODEL INPUT OUTPUT`.

Times are epoch seconds. Say them as local clock times ("Sun 19:59"), not raw numbers.

## Suggest what to do, biggest effect first

Only suggest what the numbers support, and say what each step buys in time or points. Typical moves, roughly by size:

1. **Pause or wind down the session that buys the most.** That's the running session with the latest `if_stopped.eta_if_stopped`, or the largest `adds`. Name it by project.
2. **Move subagents to a cheaper model** when a session's `subagents` share is high. Options:
   - Ask for Sonnet in the Task call.
   - Set `model: sonnet` in the agent's frontmatter.
   - Set `CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-5-5` for new sessions.

   Exploration and search subagents rarely need Opus.
3. **Lower the effort** for routine work, such as edits, tests and small fixes: `/effort` or `effortLevel` in Claude Code, `model_reasoning_effort` in Codex's `config.toml`.
4. **Compact or start fresh at a natural break** in long sessions, since re-reading a large context costs on every turn. Do it only at a break, never mid-task.
5. **Shift background or batch work to after the reset** when the 5-hour window is the problem, because the 5-hour limit resets often.
6. **Size big jobs first.** For a job estimated at about $X of API-equivalent usage, that's X / `usd_per_pct` points of Claude's weekly limit. Compare it with the room left (100 − `p50`) before starting.
7. **Move work between tools** when one pool is tight and another has room, e.g. Codex's weekly limit at 30% while Claude's is heading past 100%.
8. **On an API budget**, the money is real: keep prompt caching on, use a cheaper model for bulk work, and run batch jobs through the API's batch endpoint where the work allows.

If `headline.level` is `ok` and `p_hit` is low, say so briefly and don't invent cuts.

## Rules

- **Never change settings, models or sessions yourself without asking.** Propose; the user decides.
- **Don't print token counts or dollar figures as if they were the user's bill on a subscription.** There they're API-equivalent, not charges. On an API budget (`kind` `api`), they are the spend.
- **If `savetokens` isn't found**, say so and point to `savetokens install`.
